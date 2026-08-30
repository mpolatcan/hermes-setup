#!/usr/bin/env python3
"""Resolve one 1Password secret reference through the official SDK.

The secret is written only to stdout for capture by the launcher. Errors are
intentionally generic so tokens, references, and resolved values cannot leak.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys
from typing import Any

INTEGRATION_NAME = "Hermes Honcho Codex Adapter"
INTEGRATION_VERSION = "v0.1.0"
TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
DEFAULT_TIMEOUT_SECONDS = 30.0


class SecretResolutionError(RuntimeError):
    """Safe, non-sensitive secret-resolution failure."""


def validate_reference(reference: str) -> None:
    parts = reference.split("/")
    if len(parts) < 5 or parts[0] != "op:" or parts[1] != "" or not all(parts[2:5]):
        raise SecretResolutionError("invalid 1Password secret reference")


async def resolve_secret(
    reference: str,
    token: str,
    *,
    client_type: Any | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    validate_reference(reference)
    if not token:
        raise SecretResolutionError("missing 1Password service-account token")
    if timeout_seconds <= 0:
        raise SecretResolutionError("1Password SDK timeout must be positive")

    async def resolve() -> Any:
        client_cls = client_type
        if client_cls is None:
            client_module = importlib.import_module("onepassword.client")
            client_cls = client_module.Client
        client = await client_cls.authenticate(
            auth=token,
            integration_name=INTEGRATION_NAME,
            integration_version=INTEGRATION_VERSION,
        )
        return await client.secrets.resolve(reference)

    try:
        value = await asyncio.wait_for(resolve(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise SecretResolutionError("1Password SDK resolution timed out") from exc
    except Exception as exc:
        raise SecretResolutionError("1Password SDK resolution failed") from exc

    if not isinstance(value, str) or not value:
        raise SecretResolutionError("1Password SDK returned an empty secret")
    return value


def run(reference: str, *, client_type: Any | None = None) -> str:
    token = os.environ.pop(TOKEN_ENV, "")
    try:
        return asyncio.run(resolve_secret(reference, token, client_type=client_type))
    finally:
        token = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", help="1Password op:// secret reference")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        value = run(args.reference)
    except SecretResolutionError as exc:
        print(f"secret resolution failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
