#!/usr/bin/env python3
"""Fail-closed 1Password SDK bootstrap for Hermes gateways.

Loads only the configured ENV_VAR -> op:// reference map, resolves every value
through the official 1Password SDK, removes the bootstrap token, and execs the
supported Hermes runtime. Secret values and references are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, TextIO

import yaml

INTEGRATION_NAME = "Hermes Gateway SDK Bootstrap"
INTEGRATION_VERSION = "v0.1.0"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
HONCHO_ROOT_ENV = "HONCHO_JWT_ROOT"
DESKTOP_SESSION_TOKEN_ENV = "HERMES_DASHBOARD_SESSION_TOKEN"
DESKTOP_REMOTE_TOKEN_ENV = "HERMES_DESKTOP_REMOTE_TOKEN"
DESKTOP_REMOTE_URL_ENV = "HERMES_DESKTOP_REMOTE_URL"
DESKTOP_REMOTE_URL = "http://127.0.0.1:9120"
HONCHO_SERVER_DOMAIN = b"honcho-auth-jwt-secret-v1"
ALLOWED_HONCHO_WORKSPACES = frozenset(
    {"polatcan-gaming", "polatcan-finance", "polatcan-health"}
)
SEND_ENV_ALLOWLIST = {
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TMPDIR",
    "TZ",
    "USER",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
}
ALLOWED_PROFILES = frozenset(
    {
        "general",
        "assistant",
        "researcher",
        "coder",
        "writer",
        "producer",
        "marketing",
        "health",
        "finance",
    }
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BootstrapError(RuntimeError):
    """Safe, non-sensitive bootstrap failure."""


def validate_reference(reference: object) -> str:
    if not isinstance(reference, str):
        raise BootstrapError("invalid 1Password reference map")
    parts = reference.strip().split("/")
    if len(parts) < 5 or parts[0] != "op:" or parts[1] != "" or not all(parts[2:5]):
        raise BootstrapError("invalid 1Password reference map")
    return reference.strip()


def load_reference_map(
    config_path: Path, *, allow_enabled: bool = False
) -> tuple[dict[str, str], str, bool]:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BootstrapError("unable to load Hermes profile config") from exc
    if not isinstance(raw, dict):
        raise BootstrapError("invalid Hermes profile config")

    secrets = raw.get("secrets")
    onepassword = secrets.get("onepassword") if isinstance(secrets, dict) else None
    if not isinstance(onepassword, dict):
        raise BootstrapError("missing 1Password bootstrap config")
    provider_enabled = onepassword.get("enabled")
    if provider_enabled is not True and provider_enabled is not False:
        raise BootstrapError("invalid built-in 1Password provider state")
    if provider_enabled is True and not allow_enabled:
        raise BootstrapError("built-in 1Password provider must be disabled")

    token_env = onepassword.get("service_account_token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not ENV_NAME_RE.fullmatch(token_env):
        raise BootstrapError("invalid 1Password token environment name")

    env_map = onepassword.get("env")
    if not isinstance(env_map, dict) or not env_map:
        raise BootstrapError("empty 1Password reference map")

    references: dict[str, str] = {}
    for name, reference in env_map.items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            raise BootstrapError("invalid 1Password environment mapping")
        if name == token_env:
            raise BootstrapError("token environment cannot be a resolved target")
        references[name] = validate_reference(reference)
    return references, token_env, provider_enabled


async def resolve_reference_map(
    references: Mapping[str, str],
    token: str,
    *,
    client_type: Any | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    if not token:
        raise BootstrapError("missing 1Password service-account token")
    if timeout_seconds <= 0:
        raise BootstrapError("1Password SDK timeout must be positive")

    async def resolve() -> dict[str, str]:
        client_cls = client_type
        if client_cls is None:
            client_module = importlib.import_module("onepassword.client")
            client_cls = client_module.Client
        client = await client_cls.authenticate(
            auth=token,
            integration_name=INTEGRATION_NAME,
            integration_version=INTEGRATION_VERSION,
        )
        ordered = sorted(references.items())
        response = await client.secrets.resolve_all(
            [reference for _, reference in ordered]
        )
        individual = getattr(response, "individual_responses", None)
        if not isinstance(individual, Mapping):
            raise BootstrapError("1Password SDK could not resolve all references")
        values: dict[str, str] = {}
        for name, reference in ordered:
            item = individual.get(reference)
            if (
                item is None
                or getattr(item, "error", None) is not None
                or getattr(item, "content", None) is None
            ):
                raise BootstrapError("1Password SDK could not resolve all references")
            value = getattr(item.content, "secret", None)
            if not isinstance(value, str) or not value:
                raise BootstrapError("1Password SDK returned an empty secret")
            values[name] = value
        return values

    try:
        return await asyncio.wait_for(resolve(), timeout=timeout_seconds)
    except BootstrapError:
        raise
    except TimeoutError as exc:
        raise BootstrapError("1Password SDK resolution timed out") from exc
    except Exception as exc:
        raise BootstrapError("1Password SDK resolution failed") from exc


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def load_honcho_workspace(profile_home: Path) -> str:
    try:
        raw = json.loads((profile_home / "honcho.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise BootstrapError("unable to load Honcho profile config") from exc
    workspace = raw.get("workspace") if isinstance(raw, dict) else None
    expected = (
        "polatcan-finance"
        if profile_home.name == "finance"
        else "polatcan-health"
        if profile_home.name == "health"
        else "polatcan-gaming"
    )
    if workspace != expected or workspace not in ALLOWED_HONCHO_WORKSPACES:
        raise BootstrapError("invalid Honcho workspace for profile")
    return workspace


def derive_honcho_environment(
    resolved: Mapping[str, str],
    *,
    workspace: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    values = dict(resolved)
    root = values.pop(HONCHO_ROOT_ENV, None)
    if root is None:
        return values
    if not root or workspace not in ALLOWED_HONCHO_WORKSPACES:
        raise BootstrapError("invalid Honcho workspace or root secret")
    if "HONCHO_API_KEY" in values:
        raise BootstrapError("ambiguous Honcho credential mapping")
    server_secret = hmac.new(root.encode(), HONCHO_SERVER_DOMAIN, hashlib.sha256).hexdigest()
    header = _b64url(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode()
    )
    payload = _b64url(
        json.dumps(
            {
                "t": timestamp or dt.datetime.now(dt.timezone.utc).isoformat(),
                "w": workspace,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    signature = _b64url(
        hmac.new(server_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    )
    values["HONCHO_API_KEY"] = f"{signing_input}.{signature}"
    return values


def build_child_environment(
    base_environment: Mapping[str, str],
    resolved: Mapping[str, str],
    *,
    token_env: str,
    profile_home: Path,
) -> dict[str, str]:
    child = dict(base_environment)
    child.pop(token_env, None)
    child.pop(DEFAULT_TOKEN_ENV, None)
    child.pop("VIRTUAL_ENV", None)
    child.update(resolved)
    child["HERMES_HOME"] = str(profile_home)
    return child


def parse_maintenance_send_args(command_args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
    )
    parser.add_argument("-t", "--to", action="append")
    parser.add_argument("-f", "--file")
    parser.add_argument("-s", "--subject")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-l", "--list", dest="list_targets", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("message", nargs="?")
    try:
        parsed, unknown = parser.parse_known_args(command_args)
    except (argparse.ArgumentError, SystemExit) as exc:
        raise BootstrapError("unsupported maintenance send arguments") from exc
    if unknown:
        raise BootstrapError("unsupported maintenance send arguments")
    if parsed.list_targets:
        raise BootstrapError("send command is restricted to message delivery")
    targets = parsed.to or []
    if len(targets) != 1:
        raise BootstrapError("send command requires exactly one target")
    target = targets[0]
    if target != "telegram":
        raise BootstrapError("send command is restricted to the Telegram home target")
    if parsed.file not in {None, "-"}:
        raise BootstrapError("send command can read only piped stdin")
    if parsed.message is None and parsed.file != "-":
        raise BootstrapError("send command requires literal text or explicit piped stdin")
    if parsed.message == "-":
        raise BootstrapError("send command requires --file - for piped stdin")
    if parsed.message and "MEDIA:" in parsed.message:
        raise BootstrapError("send command does not permit media attachments")
    if parsed.subject and "MEDIA:" in parsed.subject:
        raise BootstrapError("send command does not permit media attachments")
    return parsed


def select_references_for_command(
    command: str,
    command_args: list[str],
    references: Mapping[str, str],
) -> dict[str, str]:
    if command in {"gateway", "serve"}:
        if command_args:
            raise BootstrapError(f"{command} command does not accept passthrough arguments")
        return dict(references)

    if command == "desktop":
        if command_args:
            raise BootstrapError("desktop command does not accept passthrough arguments")
        reference = references.get(DESKTOP_SESSION_TOKEN_ENV)
        if reference is None:
            raise BootstrapError("missing Desktop session token mapping")
        selected = {DESKTOP_SESSION_TOKEN_ENV: reference}
        honcho_reference = references.get(HONCHO_ROOT_ENV)
        if honcho_reference is not None:
            selected[HONCHO_ROOT_ENV] = honcho_reference
        return selected

    parse_maintenance_send_args(command_args)

    selected = {
        name: reference
        for name, reference in references.items()
        if name.startswith("TELEGRAM_")
    }
    if "TELEGRAM_BOT_TOKEN" not in selected:
        raise BootstrapError("missing Telegram secret mapping")
    return selected


def build_send_base_environment(base: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in base.items()
        if name in SEND_ENV_ALLOWLIST
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    command_args: list[str] = []
    if "--" in raw:
        separator = raw.index("--")
        command_args = raw[separator + 1 :]
        raw = raw[:separator]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=sorted(ALLOWED_PROFILES))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--hermes-executable", type=Path)
    parser.add_argument("--legacy-hermes", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--command", choices=("desktop", "gateway", "send", "serve"), default="gateway"
    )
    args = parser.parse_args(raw)
    args.command_args = command_args
    return args


def run(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None = None,
    client_type: Any | None = None,
    execve: Any = os.execve,
    stdin_stream: TextIO | None = None,
) -> int:
    base = dict(os.environ if environment is None else environment)
    profile_home = Path(f"/Users/mutlupolatcan/.hermes/profiles/{args.profile}")
    config_path = args.config or profile_home / "config.yaml"
    hermes_executable = args.hermes_executable or Path(
        "/Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/hermes"
    )
    desktop_executable = getattr(args, "desktop_executable", None) or Path(
        "/Applications/Hermes.app/Contents/MacOS/Hermes"
    )
    command = getattr(args, "command", "gateway")
    command_args = list(getattr(args, "command_args", []))
    if command_args[:1] == ["--"]:
        command_args = command_args[1:]
    if command in {"desktop", "serve"} and args.profile != "general":
        raise BootstrapError(f"{command} command is restricted to the general profile")

    references, token_env, provider_enabled = load_reference_map(
        config_path, allow_enabled=args.legacy_hermes is not None
    )
    if provider_enabled:
        if command != "gateway" or command_args:
            raise BootstrapError(
                "legacy transition supports only gateway execution"
            )
        legacy_hermes = args.legacy_hermes
        if (
            legacy_hermes is None
            or not legacy_hermes.is_absolute()
            or not legacy_hermes.is_file()
            or not os.access(legacy_hermes, os.X_OK)
        ):
            raise BootstrapError("legacy Hermes transition executable is unavailable")
        legacy_argv = [
            str(legacy_hermes),
            "--profile",
            args.profile,
            "gateway",
            "run",
            "--replace",
        ]
        try:
            execve(str(legacy_hermes), legacy_argv, base)
        except OSError as exc:
            raise BootstrapError("legacy Hermes transition exec failed") from exc
        raise BootstrapError("legacy Hermes transition exec unexpectedly returned")

    references = select_references_for_command(command, command_args, references)

    stdin_message: str | None = None
    if command == "send":
        parsed_send = parse_maintenance_send_args(command_args)
        if parsed_send.file == "-":
            source = stdin_stream if stdin_stream is not None else sys.stdin
            stdin_message = source.read()
            if "MEDIA:" in stdin_message:
                raise BootstrapError("send command does not permit media attachments")

    token = base.pop(token_env, "")
    try:
        resolved = asyncio.run(
            resolve_reference_map(
                references,
                token,
                client_type=client_type,
                timeout_seconds=args.timeout_seconds,
            )
        )
    finally:
        token = ""

    if HONCHO_ROOT_ENV in resolved:
        resolved = derive_honcho_environment(
            resolved,
            workspace=load_honcho_workspace(profile_home),
        )

    if command in {"desktop", "send"}:
        base = build_send_base_environment(base)
    if command == "desktop":
        session_token = resolved.pop(DESKTOP_SESSION_TOKEN_ENV, None)
        if not session_token:
            raise BootstrapError("missing resolved Desktop session token")
        resolved[DESKTOP_REMOTE_TOKEN_ENV] = session_token
        resolved[DESKTOP_REMOTE_URL_ENV] = DESKTOP_REMOTE_URL
    child = build_child_environment(
        base,
        resolved,
        token_env=token_env,
        profile_home=profile_home,
    )
    if args.check_only:
        print(
            json.dumps(
                {
                    "ok": True,
                    "profile": args.profile,
                    "provider_enabled": False,
                    "resolved_count": len(resolved),
                    "resolved_env_names": sorted(resolved),
                    "token_removed": token_env not in child,
                },
                separators=(",", ":"),
            )
        )
        return 0

    executable = desktop_executable if command == "desktop" else hermes_executable
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise BootstrapError("Hermes executable is unavailable")
    if command == "desktop":
        argv = [str(desktop_executable)]
    elif command == "send":
        argv = [
            str(hermes_executable),
            "--profile",
            args.profile,
            "send",
            *command_args,
        ]
    elif command == "serve":
        argv = [
            str(hermes_executable),
            "--profile",
            args.profile,
            "serve",
            "--isolated",
            "--host",
            "127.0.0.1",
            "--port",
            "9120",
        ]
    else:
        argv = [
            str(hermes_executable),
            "--profile",
            args.profile,
            "gateway",
            "run",
            "--replace",
        ]
    try:
        if stdin_message is None:
            execve(str(executable), argv, child)
        else:
            with tempfile.TemporaryFile(mode="w+b") as validated_stdin:
                validated_stdin.write(stdin_message.encode("utf-8"))
                validated_stdin.seek(0)
                os.dup2(validated_stdin.fileno(), 0)
                execve(str(executable), argv, child)
    except OSError as exc:
        raise BootstrapError("Hermes exec failed") from exc
    raise BootstrapError("Hermes exec unexpectedly returned")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except BootstrapError as exc:
        print(f"Hermes SDK bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
