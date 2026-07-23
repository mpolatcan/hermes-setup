from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hermes_gateway_sdk_bootstrap.py"
)
SPEC = importlib.util.spec_from_file_location("hermes_gateway_sdk_bootstrap", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)

TOKEN = "service-account-token-sentinel"
SECRET_A = "resolved-secret-a-sentinel"
SECRET_B = "resolved-secret-b-sentinel"
REF_A = "op://vault/item/field-a"
REF_B = "op://vault/item/field-b"


class FakeSecrets:
    values = {REF_A: SECRET_A, REF_B: SECRET_B}
    references: list[str] = []
    error: Exception | None = None

    async def resolve(self, reference: str) -> str:
        type(self).references.append(reference)
        error = type(self).error
        if error is not None:
            raise error
        return type(self).values[reference]


class FakeClient:
    secrets = FakeSecrets()
    auth_calls: list[dict[str, str]] = []

    @classmethod
    def reset(cls) -> None:
        FakeSecrets.references = []
        FakeSecrets.error = None
        FakeSecrets.values = {REF_A: SECRET_A, REF_B: SECRET_B}
        cls.auth_calls = []

    @classmethod
    async def authenticate(cls, **kwargs: str) -> "FakeClient":
        cls.auth_calls.append(kwargs)
        return cls()


class SlowSecrets:
    async def resolve(self, _: str) -> str:
        await asyncio.sleep(60)
        return SECRET_A


class SlowClient:
    secrets = SlowSecrets()

    @classmethod
    async def authenticate(cls, **_: str) -> "SlowClient":
        return cls()


def write_config(root: Path, *, enabled: bool = False) -> Path:
    path = root / "config.yaml"
    path.write_text(
        "secrets:\n"
        "  onepassword:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN\n"
        "    env:\n"
        f"      BETA_KEY: {REF_B}\n"
        f"      ALPHA_KEY: {REF_A}\n",
        encoding="utf-8",
    )
    return path


class GatewaySdkBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.reset()

    def test_loads_disabled_provider_reference_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            references, token_env, enabled = bootstrap.load_reference_map(
                write_config(Path(tmp))
            )
        self.assertEqual(references, {"BETA_KEY": REF_B, "ALPHA_KEY": REF_A})
        self.assertEqual(token_env, bootstrap.DEFAULT_TOKEN_ENV)
        self.assertFalse(enabled)

    def test_rejects_enabled_builtin_provider_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(Path(tmp), enabled=True)
            with self.assertRaisesRegex(bootstrap.BootstrapError, "must be disabled"):
                bootstrap.load_reference_map(path)
        self.assertEqual(FakeClient.auth_calls, [])

    def test_authenticates_once_and_resolves_in_sorted_order(self) -> None:
        values = asyncio.run(
            bootstrap.resolve_reference_map(
                {"BETA_KEY": REF_B, "ALPHA_KEY": REF_A},
                TOKEN,
                client_type=FakeClient,
            )
        )
        self.assertEqual(values, {"ALPHA_KEY": SECRET_A, "BETA_KEY": SECRET_B})
        self.assertEqual(FakeSecrets.references, [REF_A, REF_B])
        self.assertEqual(len(FakeClient.auth_calls), 1)
        self.assertEqual(FakeClient.auth_calls[0]["auth"], TOKEN)

    def test_missing_token_fails_closed(self) -> None:
        with self.assertRaisesRegex(bootstrap.BootstrapError, "missing"):
            asyncio.run(
                bootstrap.resolve_reference_map(
                    {"ALPHA_KEY": REF_A}, "", client_type=FakeClient
                )
            )
        self.assertEqual(FakeClient.auth_calls, [])

    def test_sdk_error_is_sanitized(self) -> None:
        FakeSecrets.error = RuntimeError(f"leak {TOKEN} {REF_A} {SECRET_A}")
        with self.assertRaises(bootstrap.BootstrapError) as caught:
            asyncio.run(
                bootstrap.resolve_reference_map(
                    {"ALPHA_KEY": REF_A}, TOKEN, client_type=FakeClient
                )
            )
        message = str(caught.exception)
        for sensitive in (TOKEN, REF_A, SECRET_A):
            self.assertNotIn(sensitive, message)

    def test_timeout_is_bounded_and_sanitized(self) -> None:
        with self.assertRaisesRegex(bootstrap.BootstrapError, "timed out"):
            asyncio.run(
                bootstrap.resolve_reference_map(
                    {"ALPHA_KEY": REF_A},
                    TOKEN,
                    client_type=SlowClient,
                    timeout_seconds=0.001,
                )
            )

    def test_child_environment_removes_token_and_virtualenv(self) -> None:
        child = bootstrap.build_child_environment(
            {
                bootstrap.DEFAULT_TOKEN_ENV: TOKEN,
                "VIRTUAL_ENV": "/tmp/venv",
                "PATH": "/usr/bin:/bin",
            },
            {"ALPHA_KEY": SECRET_A},
            token_env=bootstrap.DEFAULT_TOKEN_ENV,
            profile_home=Path("/profile/home"),
        )
        self.assertNotIn(bootstrap.DEFAULT_TOKEN_ENV, child)
        self.assertNotIn("VIRTUAL_ENV", child)
        self.assertEqual(child["ALPHA_KEY"], SECRET_A)
        self.assertEqual(child["HERMES_HOME"], "/profile/home")

    def test_check_only_reports_names_not_values_or_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            args = argparse.Namespace(
                profile="assistant",
                config=config,
                hermes_python=None,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=True,
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = bootstrap.run(
                    args,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["resolved_env_names"], ["ALPHA_KEY", "BETA_KEY"])
        self.assertTrue(report["token_removed"])
        rendered = stdout.getvalue()
        for sensitive in (TOKEN, SECRET_A, SECRET_B, REF_A, REF_B):
            self.assertNotIn(sensitive, rendered)

    def test_exec_uses_supported_boundary_without_token(self) -> None:
        class ExecCaptured(Exception):
            pass

        captured: dict[str, object] = {}

        def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, env=env)
            raise ExecCaptured

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root)
            hermes_python = root / "python"
            hermes_python.write_text("", encoding="utf-8")
            hermes_python.chmod(0o700)
            args = argparse.Namespace(
                profile="assistant",
                config=config,
                hermes_python=hermes_python,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=False,
            )
            with self.assertRaises(ExecCaptured):
                bootstrap.run(
                    args,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                    execve=fake_execve,
                )
        self.assertEqual(captured["path"], str(hermes_python))
        self.assertEqual(
            captured["argv"],
            [
                str(hermes_python),
                "-m",
                "hermes_cli.main",
                "--profile",
                "assistant",
                "gateway",
                "run",
                "--replace",
            ],
        )
        child = captured["env"]
        assert isinstance(child, dict)
        self.assertNotIn(bootstrap.DEFAULT_TOKEN_ENV, child)
        self.assertEqual(child["ALPHA_KEY"], SECRET_A)

    def test_transition_dispatches_enabled_profile_to_legacy_hermes(self) -> None:
        class ExecCaptured(Exception):
            pass

        captured: dict[str, object] = {}

        def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, env=env)
            raise ExecCaptured

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root, enabled=True)
            legacy = root / "hermes"
            legacy.write_text("#!/bin/sh\n", encoding="utf-8")
            legacy.chmod(0o700)
            args = argparse.Namespace(
                profile="assistant",
                config=config,
                hermes_python=None,
                legacy_hermes=legacy,
                timeout_seconds=30.0,
                check_only=False,
            )
            with self.assertRaises(ExecCaptured):
                bootstrap.run(
                    args,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                    execve=fake_execve,
                )
        self.assertEqual(captured["path"], str(legacy))
        self.assertEqual(
            captured["argv"],
            [str(legacy), "--profile", "assistant", "gateway", "run", "--replace"],
        )
        legacy_env = captured["env"]
        assert isinstance(legacy_env, dict)
        self.assertEqual(legacy_env[bootstrap.DEFAULT_TOKEN_ENV], TOKEN)
        self.assertEqual(FakeClient.auth_calls, [])

    def test_cli_error_contains_no_sensitive_data(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            with patch.dict(os.environ, {}, clear=True):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = bootstrap.main(
                        ["assistant", "--config", str(config), "--check-only"]
                    )
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("missing 1Password service-account token", stderr.getvalue())
        for sensitive in (REF_A, REF_B, SECRET_A, SECRET_B):
            self.assertNotIn(sensitive, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
