#!/usr/bin/env python3
"""Fail-closed 1Password SDK bootstrap for Hermes gateways.

Loads only the configured ENV_VAR -> op:// reference map, resolves every value
through the official 1Password SDK, removes the bootstrap token, and execs the
supported Hermes runtime. Secret values and references are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

INTEGRATION_NAME = "Hermes Gateway SDK Bootstrap"
INTEGRATION_VERSION = "v0.1.0"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
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
        values: dict[str, str] = {}
        for name in sorted(references):
            value = await client.secrets.resolve(references[name])
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=sorted(ALLOWED_PROFILES))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--hermes-executable", type=Path)
    parser.add_argument("--legacy-hermes", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args(argv)


def run(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None = None,
    client_type: Any | None = None,
    execve: Any = os.execve,
) -> int:
    base = dict(os.environ if environment is None else environment)
    profile_home = Path(f"/Users/mutlupolatcan/.hermes/profiles/{args.profile}")
    config_path = args.config or profile_home / "config.yaml"
    hermes_executable = args.hermes_executable or Path(
        "/Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/hermes"
    )

    references, token_env, provider_enabled = load_reference_map(
        config_path, allow_enabled=args.legacy_hermes is not None
    )
    if provider_enabled:
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

    if (
        not hermes_executable.is_absolute()
        or not hermes_executable.is_file()
        or not os.access(hermes_executable, os.X_OK)
    ):
        raise BootstrapError("Hermes console executable is unavailable")
    argv = [
        str(hermes_executable),
        "--profile",
        args.profile,
        "gateway",
        "run",
        "--replace",
    ]
    try:
        execve(str(hermes_executable), argv, child)
    except OSError as exc:
        raise BootstrapError("Hermes exec failed") from exc
    raise BootstrapError("Hermes exec unexpectedly returned")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except BootstrapError as exc:
        print(f"gateway bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
