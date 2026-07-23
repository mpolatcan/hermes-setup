from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resolve_onepassword_secret.py"
SPEC = importlib.util.spec_from_file_location("resolve_onepassword_secret", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)

VALID_REFERENCE = "op://vault/item/field"
TOKEN = "service-account-token-sentinel"
SECRET = "resolved-secret-sentinel"


class FakeSecrets:
    def __init__(self, value: str = SECRET, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.references: list[str] = []

    async def resolve(self, reference: str) -> str:
        self.references.append(reference)
        if self.error is not None:
            raise self.error
        return self.value


class FakeClient:
    secrets = FakeSecrets()
    auth_calls: list[dict[str, str]] = []
    auth_error: Exception | None = None

    @classmethod
    def reset(cls, *, value: str = SECRET, error: Exception | None = None) -> None:
        cls.secrets = FakeSecrets(value=value, error=error)
        cls.auth_calls = []
        cls.auth_error = None

    @classmethod
    async def authenticate(cls, **kwargs: str) -> "FakeClient":
        cls.auth_calls.append(kwargs)
        if cls.auth_error is not None:
            raise cls.auth_error
        return cls()


class SlowSecrets:
    async def resolve(self, _: str) -> str:
        await asyncio.sleep(60)
        return SECRET


class SlowClient:
    secrets = SlowSecrets()

    @classmethod
    async def authenticate(cls, **_: str) -> "SlowClient":
        return cls()


class OnePasswordSecretResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.reset()

    def test_resolves_with_service_account_and_expected_identity(self) -> None:
        value = asyncio.run(
            resolver.resolve_secret(VALID_REFERENCE, TOKEN, client_type=FakeClient)
        )

        self.assertEqual(value, SECRET)
        self.assertEqual(FakeClient.secrets.references, [VALID_REFERENCE])
        self.assertEqual(
            FakeClient.auth_calls,
            [
                {
                    "auth": TOKEN,
                    "integration_name": resolver.INTEGRATION_NAME,
                    "integration_version": resolver.INTEGRATION_VERSION,
                }
            ],
        )

    def test_invalid_reference_fails_before_authentication(self) -> None:
        with self.assertRaisesRegex(resolver.SecretResolutionError, "invalid"):
            asyncio.run(resolver.resolve_secret("not-a-reference", TOKEN, client_type=FakeClient))
        self.assertEqual(FakeClient.auth_calls, [])

    def test_missing_token_fails_closed(self) -> None:
        with self.assertRaisesRegex(resolver.SecretResolutionError, "missing"):
            asyncio.run(resolver.resolve_secret(VALID_REFERENCE, "", client_type=FakeClient))
        self.assertEqual(FakeClient.auth_calls, [])

    def test_sdk_error_is_sanitized(self) -> None:
        FakeClient.auth_error = RuntimeError(f"upstream leaked {TOKEN} {VALID_REFERENCE}")
        with self.assertRaises(resolver.SecretResolutionError) as caught:
            asyncio.run(resolver.resolve_secret(VALID_REFERENCE, TOKEN, client_type=FakeClient))
        message = str(caught.exception)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(VALID_REFERENCE, message)

    def test_sdk_timeout_is_bounded_and_sanitized(self) -> None:
        with self.assertRaisesRegex(resolver.SecretResolutionError, "timed out"):
            asyncio.run(
                resolver.resolve_secret(
                    VALID_REFERENCE,
                    TOKEN,
                    client_type=SlowClient,
                    timeout_seconds=0.001,
                )
            )

    def test_empty_secret_fails_closed(self) -> None:
        FakeClient.reset(value="")
        with self.assertRaisesRegex(resolver.SecretResolutionError, "empty"):
            asyncio.run(resolver.resolve_secret(VALID_REFERENCE, TOKEN, client_type=FakeClient))

    def test_run_removes_token_from_environment(self) -> None:
        with patch.dict(os.environ, {resolver.TOKEN_ENV: TOKEN}, clear=False):
            self.assertEqual(
                resolver.run(VALID_REFERENCE, client_type=FakeClient),
                SECRET,
            )
            self.assertNotIn(resolver.TOKEN_ENV, os.environ)

    def test_cli_missing_token_emits_only_safe_error(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = resolver.main([VALID_REFERENCE])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("missing 1Password service-account token", stderr.getvalue())
        self.assertNotIn(VALID_REFERENCE, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
