from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import os
import subprocess
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


def write_send_config(root: Path) -> Path:
    path = root / "config.yaml"
    path.write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: false\n"
        "    service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN\n"
        "    env:\n"
        f"      TELEGRAM_BOT_TOKEN: {REF_A}\n"
        f"      LINEAR_CLIENT_SECRET: {REF_B}\n",
        encoding="utf-8",
    )
    return path


def write_desktop_config(root: Path) -> Path:
    path = root / "config.yaml"
    path.write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: false\n"
        "    service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN\n"
        "    env:\n"
        f"      HERMES_DASHBOARD_SESSION_TOKEN: {REF_A}\n"
        f"      HONCHO_JWT_ROOT: {REF_B}\n",
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

    def test_derives_workspace_scoped_honcho_jwt_and_removes_root(self) -> None:
        resolved = bootstrap.derive_honcho_environment(
            {bootstrap.HONCHO_ROOT_ENV: "root-secret", "OTHER": "keep"},
            workspace="polatcan-gaming",
            timestamp="2026-07-26T00:00:00+00:00",
        )
        self.assertNotIn(bootstrap.HONCHO_ROOT_ENV, resolved)
        self.assertEqual(resolved["OTHER"], "keep")
        token = resolved["HONCHO_API_KEY"]
        self.assertEqual(len(token.split(".")), 3)
        encoded_payload = token.split(".")[1]
        encoded_payload += "=" * (-len(encoded_payload) % 4)
        self.assertEqual(
            json.loads(base64.urlsafe_b64decode(encoded_payload)),
            {"t": "2026-07-26T00:00:00+00:00", "w": "polatcan-gaming"},
        )

    def test_rejects_unapproved_honcho_workspace(self) -> None:
        with self.assertRaisesRegex(bootstrap.BootstrapError, "workspace"):
            bootstrap.derive_honcho_environment(
                {bootstrap.HONCHO_ROOT_ENV: "root-secret"},
                workspace="hermes_general",
            )

    def test_check_only_reports_names_not_values_or_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            args = argparse.Namespace(
                profile="assistant",
                config=config,
                hermes_executable=None,
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
            hermes_executable = root / "hermes"
            hermes_executable.write_text("", encoding="utf-8")
            hermes_executable.chmod(0o700)
            args = argparse.Namespace(
                profile="assistant",
                config=config,
                hermes_executable=hermes_executable,
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
        self.assertEqual(captured["path"], str(hermes_executable))
        self.assertEqual(
            captured["argv"],
            [
                str(hermes_executable),
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

    def test_serve_mode_execs_general_loopback_backend_with_full_environment(self) -> None:
        class ExecCaptured(Exception):
            pass

        captured: dict[str, object] = {}

        def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, env=env)
            raise ExecCaptured

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root)
            hermes_executable = root / "hermes"
            hermes_executable.write_text("", encoding="utf-8")
            hermes_executable.chmod(0o700)
            args = argparse.Namespace(
                profile="general",
                config=config,
                hermes_executable=hermes_executable,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=False,
                command="serve",
                command_args=[],
            )
            with self.assertRaises(ExecCaptured):
                bootstrap.run(
                    args,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                    execve=fake_execve,
                )
        self.assertEqual(
            captured["argv"],
            [
                str(hermes_executable),
                "--profile",
                "general",
                "serve",
                "--isolated",
                "--host",
                "127.0.0.1",
                "--port",
                "9120",
            ],
        )
        child = captured["env"]
        assert isinstance(child, dict)
        self.assertNotIn(bootstrap.DEFAULT_TOKEN_ENV, child)
        self.assertEqual(child["ALPHA_KEY"], SECRET_A)
        self.assertEqual(child["BETA_KEY"], SECRET_B)

    def test_desktop_mode_execs_general_app_with_only_loopback_session_environment(self) -> None:
        class ExecCaptured(Exception):
            pass

        captured: dict[str, object] = {}

        def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, env=env)
            raise ExecCaptured

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_desktop_config(root)
            desktop_executable = root / "Hermes"
            desktop_executable.write_text("", encoding="utf-8")
            desktop_executable.chmod(0o700)
            args = argparse.Namespace(
                profile="general",
                config=config,
                hermes_executable=None,
                desktop_executable=desktop_executable,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=False,
                command="desktop",
                command_args=[],
            )
            with self.assertRaises(ExecCaptured):
                bootstrap.run(
                    args,
                    environment={
                        bootstrap.DEFAULT_TOKEN_ENV: TOKEN,
                        "LINEAR_CLIENT_SECRET": "inherited-secret",
                        "PATH": "/usr/bin:/bin",
                    },
                    client_type=FakeClient,
                    execve=fake_execve,
                )
        self.assertEqual(captured["path"], str(desktop_executable))
        self.assertEqual(captured["argv"], [str(desktop_executable)])
        child = captured["env"]
        assert isinstance(child, dict)
        self.assertNotIn(bootstrap.DEFAULT_TOKEN_ENV, child)
        self.assertNotIn("LINEAR_CLIENT_SECRET", child)
        self.assertNotIn("HERMES_DASHBOARD_SESSION_TOKEN", child)
        self.assertNotIn("HONCHO_JWT_ROOT", child)
        self.assertEqual(child["HERMES_DESKTOP_REMOTE_TOKEN"], SECRET_A)
        self.assertEqual(child["HERMES_DESKTOP_REMOTE_URL"], "http://127.0.0.1:9120")

    def test_send_reference_scope_keeps_only_telegram_secrets(self) -> None:
        selected = bootstrap.select_references_for_command(
            "send",
            ["--to", "telegram", "ready"],
            {
                "TELEGRAM_BOT_TOKEN": REF_A,
                "TELEGRAM_HOME_CHANNEL": REF_B,
                "LINEAR_CLIENT_SECRET": "op://vault/item/linear",
            },
        )
        self.assertEqual(
            selected,
            {"TELEGRAM_BOT_TOKEN": REF_A, "TELEGRAM_HOME_CHANNEL": REF_B},
        )

    def test_command_scope_rejects_nontelegram_send_and_gateway_passthrough(self) -> None:
        references = {"TELEGRAM_BOT_TOKEN": REF_A}
        with self.assertRaisesRegex(bootstrap.BootstrapError, "Telegram home"):
            bootstrap.select_references_for_command(
                "send", ["--to", "discord", "ready"], references
            )
        invalid_send_args = [
            ["--to", "telegram", "ready", "-tdiscord"],
            ["--to", "telegram", "ready", "--t=discord"],
            ["--to", "telegram", "-l", "ready"],
            ["--to", "telegram:123", "ready"],
            ["--to", "telegram", "--file", "/etc/passwd"],
            ["--to", "telegram", "MEDIA:/etc/passwd"],
            ["--to", "telegram", "--subject", "MEDIA:/etc/passwd", "ready"],
            ["--to", "telegram"],
            ["--to", "telegram", "-"],
        ]
        for send_args in invalid_send_args:
            with self.subTest(send_args=send_args):
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.select_references_for_command(
                        "send", send_args, references
                    )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "passthrough"):
            bootstrap.select_references_for_command(
                "gateway", ["unexpected"], references
            )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "exactly one target"):
            bootstrap.select_references_for_command(
                "send",
                ["--to", "telegram", "ready", "--to", "discord"],
                references,
            )

    def test_send_rejects_media_from_stdin_before_secret_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(
                profile="assistant",
                config=write_send_config(root),
                hermes_executable=None,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=True,
                command="send",
                command_args=["--to", "telegram", "--file", "-"],
            )
            with self.assertRaisesRegex(bootstrap.BootstrapError, "media"):
                bootstrap.run(
                    args,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                    stdin_stream=StringIO("MEDIA:/etc/passwd"),
                )
        self.assertEqual(FakeClient.auth_calls, [])
        self.assertEqual(FakeSecrets.references, [])

    def test_send_cli_parses_command_before_passthrough_arguments(self) -> None:
        args = bootstrap.parse_args(
            [
                "assistant",
                "--command",
                "send",
                "--",
                "--to",
                "telegram",
                "ready",
            ]
        )
        self.assertEqual(args.command, "send")
        self.assertEqual(args.command_args, ["--to", "telegram", "ready"])

    def test_serve_cli_is_general_only_and_rejects_passthrough(self) -> None:
        args = bootstrap.parse_args(["general", "--command", "serve"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.command_args, [])

        references = {"ALPHA_KEY": REF_A}
        with self.assertRaisesRegex(bootstrap.BootstrapError, "passthrough"):
            bootstrap.select_references_for_command("serve", ["--port", "9120"], references)

        with tempfile.TemporaryDirectory() as tmp:
            invalid = argparse.Namespace(
                profile="assistant",
                config=write_config(Path(tmp)),
                hermes_executable=None,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=True,
                command="serve",
                command_args=[],
            )
            with self.assertRaisesRegex(bootstrap.BootstrapError, "general profile"):
                bootstrap.run(
                    invalid,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                )
        self.assertEqual(FakeClient.auth_calls, [])

    def test_desktop_cli_is_general_only_and_rejects_passthrough(self) -> None:
        args = bootstrap.parse_args(["general", "--command", "desktop"])
        self.assertEqual(args.command, "desktop")
        self.assertEqual(args.command_args, [])

        references = {
            "HERMES_DASHBOARD_SESSION_TOKEN": REF_A,
            "HONCHO_JWT_ROOT": REF_B,
        }
        with self.assertRaisesRegex(bootstrap.BootstrapError, "passthrough"):
            bootstrap.select_references_for_command("desktop", ["--unsafe"], references)
        self.assertEqual(
            bootstrap.select_references_for_command("desktop", [], references),
            {"HERMES_DASHBOARD_SESSION_TOKEN": REF_A},
        )

        with tempfile.TemporaryDirectory() as tmp:
            invalid = argparse.Namespace(
                profile="assistant",
                config=write_desktop_config(Path(tmp)),
                hermes_executable=None,
                desktop_executable=None,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=True,
                command="desktop",
                command_args=[],
            )
            with self.assertRaisesRegex(bootstrap.BootstrapError, "general profile"):
                bootstrap.run(
                    invalid,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                )
        self.assertEqual(FakeClient.auth_calls, [])

    def test_send_mode_execs_only_hermes_send_with_resolved_environment(self) -> None:
        class ExecCaptured(Exception):
            pass

        captured: dict[str, object] = {}

        def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, env=env)
            raise ExecCaptured

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_send_config(root)
            hermes_executable = root / "hermes"
            hermes_executable.write_text("", encoding="utf-8")
            hermes_executable.chmod(0o700)
            args = argparse.Namespace(
                profile="assistant",
                config=config,
                hermes_executable=hermes_executable,
                legacy_hermes=None,
                timeout_seconds=30.0,
                check_only=False,
                command="send",
                command_args=["--to", "telegram", "ready"],
            )
            with self.assertRaises(ExecCaptured):
                bootstrap.run(
                    args,
                    environment={
                        bootstrap.DEFAULT_TOKEN_ENV: TOKEN,
                        "HOME": "/Users/test",
                        "PATH": "/usr/bin:/bin",
                        "LINEAR_CLIENT_SECRET": "inherited-linear-secret",
                        "LC_SECRET": "inherited-locale-secret",
                    },
                    client_type=FakeClient,
                    execve=fake_execve,
                )
        self.assertEqual(
            captured["argv"],
            [
                str(hermes_executable),
                "--profile",
                "assistant",
                "send",
                "--to",
                "telegram",
                "ready",
            ],
        )
        child = captured["env"]
        assert isinstance(child, dict)
        self.assertNotIn(bootstrap.DEFAULT_TOKEN_ENV, child)
        self.assertEqual(child["TELEGRAM_BOT_TOKEN"], SECRET_A)
        self.assertNotIn("LINEAR_CLIENT_SECRET", child)
        self.assertNotIn("LC_SECRET", child)
        self.assertEqual(child["PATH"], "/usr/bin:/bin")
        self.assertEqual(FakeSecrets.references, [REF_A])

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
                hermes_executable=None,
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

    def test_transition_rejects_send_instead_of_dispatching_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root, enabled=True)
            legacy = root / "hermes"
            legacy.write_text("#!/bin/sh\n", encoding="utf-8")
            legacy.chmod(0o700)
            args = argparse.Namespace(
                profile="assistant",
                config=config,
                hermes_executable=None,
                legacy_hermes=legacy,
                timeout_seconds=30.0,
                check_only=False,
                command="send",
                command_args=["--to", "telegram", "ready"],
            )
            with self.assertRaisesRegex(
                bootstrap.BootstrapError, "only gateway"
            ):
                bootstrap.run(
                    args,
                    environment={bootstrap.DEFAULT_TOKEN_ENV: TOKEN},
                    client_type=FakeClient,
                )
        self.assertEqual(FakeClient.auth_calls, [])

    def test_watchdog_pipes_stdin_as_a_file_and_hashes_stable_state(self) -> None:
        watchdog = (
            Path(__file__).parents[3] / "scripts" / "watchdog.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("--to telegram --file -", watchdog)
        self.assertIn(
            'printf "%s" "$REPORT|$ISSUES|$ALL_OK|$DOWN_COUNT|$RESTART_COUNT" | md5',
            watchdog,
        )
        self.assertNotIn('echo "$MESSAGE" | md5', watchdog)

    def test_send_wrapper_sanitizes_environment_before_keychain_lookup(self) -> None:
        wrapper = (
            Path(__file__).parents[1] / "scripts" / "hermes_send_keychain.sh"
        ).read_text(encoding="utf-8")
        self.assertLess(wrapper.index("/usr/bin/env -i"), wrapper.index("/usr/bin/security"))
        self.assertNotIn("HERMES_SEND_SANITIZED", wrapper)
        self.assertIn("HOME=\"/Users/mutlupolatcan\"", wrapper)
        self.assertIn("/bin/bash --noprofile --norc -c", wrapper)
        for unsafe in ("DYLD_", "PYTHONPATH", "HTTPS_PROXY", "SSL_CERT_FILE"):
            self.assertNotIn(f'{unsafe}="${{{unsafe}', wrapper)

    def test_desktop_wrapper_sanitizes_environment_before_keychain_lookup(self) -> None:
        wrapper = (
            Path(__file__).parents[1] / "scripts" / "hermes_desktop_keychain.sh"
        ).read_text(encoding="utf-8")
        self.assertLess(wrapper.index("/usr/bin/env -i"), wrapper.index("/usr/bin/security"))
        self.assertIn("HOME=\"/Users/mutlupolatcan\"", wrapper)
        self.assertIn("/bin/bash --noprofile --norc -c", wrapper)
        self.assertLess(wrapper.index("/usr/bin/pgrep -x Hermes"), wrapper.index("/usr/bin/security"))
        self.assertIn('"$profile" --command desktop', wrapper)
        for unsafe in ("DYLD_", "PYTHONPATH", "HTTPS_PROXY", "SSL_CERT_FILE"):
            self.assertNotIn(f'{unsafe}="${{{unsafe}', wrapper)

    def test_desktop_launchagent_runs_only_the_secure_wrapper(self) -> None:
        plist = (
            Path(__file__).parents[1] / "launchd" / "ai.hermes.desktop-general.plist"
        ).read_text(encoding="utf-8")
        self.assertIn("ai.hermes.desktop-general", plist)
        self.assertIn("/Users/mutlupolatcan/.hermes/scripts/hermes-desktop-keychain.sh", plist)
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", plist)
        self.assertNotIn("HERMES_DESKTOP_REMOTE_TOKEN", plist)

    def test_watchdog_retries_failed_delivery_and_counts_a_missing_label_once(self) -> None:
        watchdog = Path(__file__).parents[3] / "scripts" / "watchdog.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()

            stubs = {
                "launchctl": """#!/bin/bash
if [[ "${MISSING_GENERAL:-0}" == "1" && "$2" == "ai.hermes.gateway-general" ]]; then exit 1; fi
printf '\"PID\" = %s;\n\"LastExitStatus\" = 0;\n' "${PID_VALUE:-123}"
""",
                "ps": "#!/bin/bash\nprintf '%s\\n' \"$2\"\n",
                "docker": """#!/bin/bash
if [[ "$*" == *".State.Running"* ]]; then printf 'true\n'; else printf 'healthy\n'; fi
""",
                "queue-probe": "#!/bin/bash\nprintf '{\"ok\":true,\"pending\":0,\"in_progress\":0}\\n'\n",
                "send": """#!/bin/bash
cat >> "$SEND_LOG"
printf '\n---delivery---\n' >> "$SEND_LOG"
exit "${SEND_EXIT:-1}"
""",
            }
            for name, content in stubs.items():
                path = bin_dir / name
                path.write_text(content, encoding="utf-8")
                path.chmod(0o700)

            send_log = root / "send.log"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "HERMES_WATCHDOG_STATE_DIR": str(root / "state"),
                "HERMES_WATCHDOG_SEND": str(bin_dir / "send"),
                "HERMES_WATCHDOG_DOCKER": str(bin_dir / "docker"),
                "HERMES_WATCHDOG_QUEUE_PROBE": str(bin_dir / "queue-probe"),
                "SEND_LOG": str(send_log),
                "SEND_EXIT": "1",
                "MISSING_GENERAL": "1",
                "PID_VALUE": "123",
            }
            first = subprocess.run(
                ["/bin/bash", str(watchdog)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 1)
            status_file = root / "state" / "hermes-watchdog-last"
            self.assertFalse(status_file.exists())
            self.assertFalse((root / "state" / "hermes-watchdog-prev").exists())
            first_message = send_log.read_text(encoding="utf-8")
            self.assertIn("general not running", first_message)
            self.assertIn("1 component failure(s)", first_message)

            environment["SEND_EXIT"] = "0"
            second = subprocess.run(
                ["/bin/bash", str(watchdog)], env=environment, check=False
            )
            self.assertEqual(second.returncode, 0)
            self.assertTrue(status_file.exists())
            deliveries = send_log.read_text(encoding="utf-8").count("---delivery---")
            self.assertEqual(deliveries, 2)

            third = subprocess.run(
                ["/bin/bash", str(watchdog)], env=environment, check=False
            )
            self.assertEqual(third.returncode, 0)
            self.assertEqual(
                send_log.read_text(encoding="utf-8").count("---delivery---"), 2
            )

            restart_state = root / "restart-state"
            restart_state.mkdir()
            profiles = "general researcher assistant marketing coder writer producer finance health"
            (restart_state / "hermes-watchdog-prev").write_text(
                "".join(f"{profile}:111:0\n" for profile in profiles.split()),
                encoding="utf-8",
            )
            environment.update(
                HERMES_WATCHDOG_STATE_DIR=str(restart_state),
                MISSING_GENERAL="0",
                PID_VALUE="222",
                SEND_EXIT="1",
            )
            failed_restart = subprocess.run(
                ["/bin/bash", str(watchdog)], env=environment, check=False
            )
            self.assertEqual(failed_restart.returncode, 1)
            self.assertIn(
                "general:111:0",
                (restart_state / "hermes-watchdog-prev").read_text(encoding="utf-8"),
            )

            environment["SEND_EXIT"] = "0"
            successful_retry = subprocess.run(
                ["/bin/bash", str(watchdog)], env=environment, check=False
            )
            self.assertEqual(successful_retry.returncode, 0)
            self.assertIn(
                "general:222:0",
                (restart_state / "hermes-watchdog-prev").read_text(encoding="utf-8"),
            )

    def test_watchdog_alerts_on_queue_threshold_and_sends_one_recovery(self) -> None:
        watchdog = Path(__file__).parents[3] / "scripts" / "watchdog.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stubs = {
                "launchctl": "#!/bin/bash\nprintf '\"PID\" = 123;\\n\"LastExitStatus\" = 0;\\n'\n",
                "ps": "#!/bin/bash\nprintf '123\\n'\n",
                "docker": """#!/bin/bash
if [[ "$*" == *".State.Running"* ]]; then printf 'true\n'; else printf 'healthy\n'; fi
""",
                "queue-probe": """#!/bin/bash
if [[ "${QUEUE_FAIL:-0}" == "1" ]]; then
  printf '{"ok":false,"pending":26,"in_progress":0,"threshold":"pending>25"}\n'
  exit 2
fi
printf '{"ok":true,"pending":0,"in_progress":0}\n'
""",
                "send": """#!/bin/bash
cat >> "$SEND_LOG"
printf '\n---delivery---\n' >> "$SEND_LOG"
""",
            }
            for name, content in stubs.items():
                path = bin_dir / name
                path.write_text(content, encoding="utf-8")
                path.chmod(0o700)
            send_log = root / "send.log"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "HERMES_WATCHDOG_STATE_DIR": str(root / "state"),
                "HERMES_WATCHDOG_SEND": str(bin_dir / "send"),
                "HERMES_WATCHDOG_DOCKER": str(bin_dir / "docker"),
                "HERMES_WATCHDOG_QUEUE_PROBE": str(bin_dir / "queue-probe"),
                "SEND_LOG": str(send_log),
                "QUEUE_FAIL": "1",
            }
            alarm = subprocess.run(
                ["/bin/bash", str(watchdog)], env=environment, check=False
            )
            self.assertEqual(alarm.returncode, 0)
            self.assertTrue(send_log.exists(), "queue threshold must emit an alert")
            first = send_log.read_text(encoding="utf-8")
            self.assertIn("Honcho queue threshold exceeded", first)
            self.assertIn("pending=26", first)

            environment["QUEUE_FAIL"] = "0"
            recovered = subprocess.run(
                ["/bin/bash", str(watchdog)], env=environment, check=False
            )
            self.assertEqual(recovered.returncode, 0)
            second = send_log.read_text(encoding="utf-8")
            self.assertIn("Recovered", second)
            self.assertEqual(second.count("---delivery---"), 2)

            steady = subprocess.run(
                ["/bin/bash", str(watchdog)], env=environment, check=False
            )
            self.assertEqual(steady.returncode, 0)
            self.assertEqual(
                send_log.read_text(encoding="utf-8").count("---delivery---"), 2
            )

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
