#!/usr/bin/env python3
"""Mint a short-lived Derya GitHub App token and exec the pinned gh binary.

The 1Password bootstrap token and GitHub private key stay in memory. The minted
installation token is passed only to the gh child process and is never printed.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

INTEGRATION_NAME = "Derya GitHub App Token Broker"
INTEGRATION_VERSION = "v0.1.0"
TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
GH_BINARY = "/opt/homebrew/bin/gh"
ALLOWED_REPOSITORY = "mpolatcan/hermes-setup"
EXPECTED_APP_ID = 4550664
EXPECTED_INSTALLATION_ID = 152740425
EXPECTED_OWNER = "mpolatcan"
EXPECTED_PERMISSIONS = {
    "actions": "read",
    "contents": "write",
    "metadata": "read",
    "pull_requests": "write",
}
REQUEST_TIMEOUT_SECONDS = 30
VAULT_ID = "7ubnofdpw4kdjj43vjvyknjuva"
ITEM_ID = "utureginrsgfwswikgqseyqcve"
PRIVATE_KEY_FILE_ID = "y7c72m26tfupeojbxze2iogbnu"
PRIVATE_KEY_FILE_NAME = "Private Key"
PRIVATE_KEY_FILE_SIZE = 1679
SECRET_REFERENCES = {
    "app_id": "op://7ubnofdpw4kdjj43vjvyknjuva/utureginrsgfwswikgqseyqcve/qzktbavhmz4zqgz5tf2w4kbgf4",
    "installation_id": "op://7ubnofdpw4kdjj43vjvyknjuva/utureginrsgfwswikgqseyqcve/iijdqpvj5oros2buzxzuzpjzsa",
    "repository": "op://7ubnofdpw4kdjj43vjvyknjuva/utureginrsgfwswikgqseyqcve/ijt3fsi65t5lrzeeuko7wlzhla",
}
SAFE_ENV = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
    }
)


class BrokerError(RuntimeError):
    """A safe, non-sensitive broker failure."""


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def normalize_resolved(values: Mapping[str, str]) -> dict[str, Any]:
    try:
        app_id = int(values["app_id"])
        installation_id = int(values["installation_id"])
        repository = values["repository"].strip()
        private_key = values["private_key"]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise BrokerError("invalid GitHub App credential metadata") from exc
    if app_id != EXPECTED_APP_ID or installation_id != EXPECTED_INSTALLATION_ID:
        raise BrokerError("GitHub App identity mismatch")
    if repository != ALLOWED_REPOSITORY:
        raise BrokerError("GitHub App repository scope mismatch")
    first_line = private_key.splitlines()[0] if private_key.splitlines() else ""
    footer = private_key.rstrip().splitlines()[-1] if private_key.rstrip().splitlines() else ""
    if not (
        first_line.startswith("-----BEGIN ")
        and "PRIVATE KEY-----" in first_line
        and footer in {"-----END RSA PRIVATE KEY-----", "-----END PRIVATE KEY-----"}
    ):
        raise BrokerError("invalid GitHub App private key")
    return {
        "app_id": app_id,
        "installation_id": installation_id,
        "repository": repository,
        "private_key": private_key,
    }


async def resolve_references(
    token: str,
    *,
    client_type: Any | None = None,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not token:
        raise BrokerError("missing 1Password bootstrap token")

    async def resolve() -> dict[str, Any]:
        client_cls = client_type
        if client_cls is None:
            client_cls = importlib.import_module("onepassword.client").Client
        file_attributes_type = importlib.import_module("onepassword.types").FileAttributes
        client = await client_cls.authenticate(
            auth=token,
            integration_name=INTEGRATION_NAME,
            integration_version=INTEGRATION_VERSION,
        )
        ordered = sorted(SECRET_REFERENCES.items())
        response = await client.secrets.resolve_all([reference for _, reference in ordered])
        individual = getattr(response, "individual_responses", None)
        if not isinstance(individual, Mapping):
            raise BrokerError("1Password SDK could not resolve GitHub App credentials")
        resolved: dict[str, str] = {}
        for name, reference in ordered:
            item = individual.get(reference)
            value = getattr(getattr(item, "content", None), "secret", None)
            if item is None or getattr(item, "error", None) is not None or not isinstance(value, str) or not value:
                raise BrokerError("1Password SDK could not resolve GitHub App credentials")
            resolved[name] = value
        key_bytes = await client.items.files.read(
            VAULT_ID,
            ITEM_ID,
            file_attributes_type(
                name=PRIVATE_KEY_FILE_NAME,
                id=PRIVATE_KEY_FILE_ID,
                size=PRIVATE_KEY_FILE_SIZE,
            ),
        )
        if not isinstance(key_bytes, bytes) or len(key_bytes) != PRIVATE_KEY_FILE_SIZE:
            raise BrokerError("1Password SDK could not resolve GitHub App credentials")
        try:
            resolved["private_key"] = key_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BrokerError("invalid GitHub App private key") from exc
        return normalize_resolved(resolved)

    try:
        return await asyncio.wait_for(resolve(), timeout=timeout_seconds)
    except BrokerError:
        raise
    except TimeoutError as exc:
        raise BrokerError("1Password SDK resolution timed out") from exc
    except Exception as exc:
        raise BrokerError("1Password SDK resolution failed") from exc


def sign_rs256(payload: bytes, private_key: str) -> bytes:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, private_key.encode("utf-8"))
    finally:
        os.close(write_fd)
    try:
        result = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_fd}"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            pass_fds=(read_fd,),
        )
    finally:
        os.close(read_fd)
    if result.returncode != 0 or not result.stdout:
        raise BrokerError("GitHub App JWT signing failed")
    return result.stdout


def build_app_jwt(
    app_id: int,
    private_key: str,
    *,
    now: int | None = None,
    signer: Callable[[bytes, str], bytes] = sign_rs256,
) -> str:
    timestamp = int(time.time()) if now is None else int(now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    claims = _b64url(
        json.dumps(
            {"exp": timestamp + 540, "iat": timestamp - 60, "iss": str(app_id)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    signing_input = f"{header}.{claims}"
    signature = _b64url(signer(signing_input.encode("ascii"), private_key))
    return f"{signing_input}.{signature}"


def mint_installation_token(
    *,
    jwt: str,
    installation_id: int,
    repository: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    owner, repo = repository.split("/", 1)
    if owner != "mpolatcan" or repo != "hermes-setup":
        raise BrokerError("GitHub App repository scope mismatch")
    payload = json.dumps(
        {
            "permissions": {
                "actions": "read",
                "contents": "write",
                "pull_requests": "write",
            },
            "repositories": [repo],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    request = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
            "User-Agent": "derya-github-app-token-broker/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read())
    except Exception as exc:
        raise BrokerError("GitHub installation token request failed") from exc
    token = data.get("token") if isinstance(data, dict) else None
    expires_at = data.get("expires_at") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or not isinstance(expires_at, str) or not expires_at:
        raise BrokerError("GitHub installation token response invalid")
    return token


def verify_installation(
    *,
    jwt: str,
    installation_id: int,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    if installation_id != EXPECTED_INSTALLATION_ID:
        raise BrokerError("GitHub App identity mismatch")
    request = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}",
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "User-Agent": "derya-github-app-token-broker/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read())
    except Exception as exc:
        raise BrokerError("GitHub installation verification failed") from exc
    if not isinstance(data, dict):
        raise BrokerError("GitHub installation verification failed")
    account = data.get("account")
    if (
        not isinstance(account, dict)
        or account.get("login") != EXPECTED_OWNER
        or data.get("target_type") != "User"
        or data.get("repository_selection") != "selected"
        or data.get("permissions") != EXPECTED_PERMISSIONS
    ):
        raise BrokerError("GitHub installation scope mismatch")


def build_child_environment(
    base: Mapping[str, str], gh_token: str, *, gh_config_dir: str
) -> dict[str, str]:
    if not gh_token:
        raise BrokerError("empty GitHub installation token")
    child = {name: value for name, value in base.items() if name in SAFE_ENV}
    child["HOME"] = "/Users/mutlupolatcan"
    child["USER"] = "mutlupolatcan"
    child["LOGNAME"] = "mutlupolatcan"
    child["PATH"] = "/Users/mutlupolatcan/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    child["GH_CONFIG_DIR"] = gh_config_dir
    child["GH_TOKEN"] = gh_token
    return child


def validate_command(command: Sequence[str]) -> list[str]:
    args = list(command)
    if not args or args[0] != GH_BINARY:
        raise BrokerError("only the pinned gh binary is allowed")
    if args[1:] == ["auth", "status"]:
        return args
    if len(args) < 3 or args[1] != "api":
        raise BrokerError("only gh auth status and pinned gh api routes are allowed")
    endpoint = args[2]
    repo_root = "repos/mpolatcan/hermes-setup"
    if (
        not re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@/-]+", endpoint)
        or any(segment in {"", ".", ".."} for segment in endpoint.split("/"))
        or "%" in endpoint
        or "\\" in endpoint
    ):
        raise BrokerError("GitHub API route is not canonical")
    if endpoint != "installation/repositories" and not (
        endpoint == repo_root or endpoint.startswith(f"{repo_root}/")
    ):
        raise BrokerError("GitHub API route is outside the pinned repository")
    method = "GET"
    method_explicit = False
    raw_fields: dict[str, str] = {}
    tail = args[3:]
    index = 0
    while index < len(tail):
        argument = tail[index]
        if argument in {"--include", "--silent"}:
            index += 1
            continue
        if argument in {"-X", "--method"}:
            if index + 1 >= len(tail):
                raise BrokerError("missing gh api method")
            method = tail[index + 1].upper()
            method_explicit = True
            index += 2
            continue
        if argument.startswith("--method="):
            method = argument.split("=", 1)[1].upper()
            method_explicit = True
            index += 1
            continue
        if argument in {"-f", "--raw-field"}:
            if index + 1 >= len(tail):
                raise BrokerError("missing gh api raw field")
            field = tail[index + 1]
            index += 2
        elif argument.startswith("--raw-field="):
            field = argument.split("=", 1)[1]
            index += 1
        else:
            raise BrokerError("gh api flag is not allowed")
        if "=" not in field:
            raise BrokerError("invalid gh api raw field")
        name, value = field.split("=", 1)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name in raw_fields:
            raise BrokerError("invalid gh api raw field")
        raw_fields[name] = value
    if raw_fields and not method_explicit:
        method = "POST"
    allowed_methods = {"GET", "POST", "PATCH", "PUT"}
    if method not in allowed_methods:
        raise BrokerError("GitHub API method is not allowed")
    if endpoint == "installation/repositories":
        if method != "GET" or raw_fields:
            raise BrokerError("installation scope route is read-only")
        return args
    if method == "GET":
        if raw_fields:
            raise BrokerError("GET routes cannot carry request fields")
        return args
    relative = endpoint.removeprefix(f"{repo_root}/")
    allowed_fields: set[str]
    if method == "POST" and relative == "pulls":
        allowed_fields = {"base", "body", "draft", "head", "title"}
        if not {"base", "head", "title"}.issubset(raw_fields):
            raise BrokerError("pull request creation fields are incomplete")
    elif method == "PUT" and re.fullmatch(r"pulls/[1-9][0-9]*/merge", relative):
        allowed_fields = {"commit_message", "commit_title", "merge_method", "sha"}
    elif method == "POST" and re.fullmatch(r"issues/[1-9][0-9]*/comments", relative):
        allowed_fields = {"body"}
        if set(raw_fields) != {"body"}:
            raise BrokerError("comment creation requires only body")
    else:
        raise BrokerError("GitHub API mutation route is not allowed")
    if not set(raw_fields).issubset(allowed_fields):
        raise BrokerError("GitHub API mutation field is not allowed")
    return args


def sanitize_gh_auth_status_output(output: str) -> str:
    sanitized = re.sub(
        r"(?m)^(\s*-\s*Token:)\s*.*$",
        r"\1 [REDACTED]",
        output,
    )
    return re.sub(r"ghs_[A-Za-z0-9_]+", "[REDACTED]", sanitized)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--":
        raise BrokerError("usage: github_app_token_broker.py -- /opt/homebrew/bin/gh ...")
    command = validate_command(args[1:])
    bootstrap_token = os.environ.get(TOKEN_ENV, "")
    resolved = asyncio.run(resolve_references(bootstrap_token))
    os.environ.pop(TOKEN_ENV, None)
    jwt = build_app_jwt(resolved["app_id"], resolved["private_key"])
    verify_installation(jwt=jwt, installation_id=resolved["installation_id"])
    installation_token = mint_installation_token(
        jwt=jwt,
        installation_id=resolved["installation_id"],
        repository=resolved["repository"],
    )
    with tempfile.TemporaryDirectory(
        prefix="derya-gh-config-", dir="/private/tmp"
    ) as gh_config_dir:
        os.chmod(gh_config_dir, 0o700)
        child = build_child_environment(
            os.environ, installation_token, gh_config_dir=gh_config_dir
        )
        if command[1:] == ["auth", "status"]:
            result = subprocess.run(
                command,
                env=child,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.stdout:
                sys.stdout.write(sanitize_gh_auth_status_output(result.stdout))
            if result.stderr:
                sys.stderr.write(sanitize_gh_auth_status_output(result.stderr))
        else:
            result = subprocess.run(command, env=child, check=False)
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokerError as exc:
        print(f"derya-gh: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
