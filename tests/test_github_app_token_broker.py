import base64
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "github_app_token_broker.py"
SPEC = importlib.util.spec_from_file_location("github_app_token_broker", MODULE_PATH)
assert SPEC and SPEC.loader
BROKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BROKER)


def decode_segment(value: str) -> dict:
    value += "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value))


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class BrokerTests(unittest.TestCase):
    def test_normalize_resolved_accepts_only_pinned_installation(self):
        values = BROKER.normalize_resolved(
            {
                "app_id": "4550664",
                "installation_id": "152740425",
                "repository": "mpolatcan/hermes-setup",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nkey\n-----END RSA PRIVATE KEY-----",
            }
        )
        self.assertEqual(values["app_id"], 4550664)
        self.assertEqual(values["installation_id"], 152740425)

        for field, invalid in (
            ("app_id", "4550665"),
            ("installation_id", "152740426"),
            ("repository", "mpolatcan/other"),
            ("private_key", "not-a-key"),
        ):
            broken = {
                "app_id": "4550664",
                "installation_id": "152740425",
                "repository": "mpolatcan/hermes-setup",
                "private_key": "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----",
            }
            broken[field] = invalid
            with self.subTest(field=field), self.assertRaises(BROKER.BrokerError):
                BROKER.normalize_resolved(broken)

    def test_build_app_jwt_uses_short_lived_rs256_claims(self):
        captured = {}

        def signer(payload: bytes, private_key: str) -> bytes:
            captured["payload"] = payload
            captured["private_key"] = private_key
            return b"signature"

        token = BROKER.build_app_jwt(4550664, "private", now=1_800_000_000, signer=signer)
        header, claims, signature = token.split(".")
        self.assertEqual(decode_segment(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            decode_segment(claims),
            {"exp": 1_800_000_540, "iat": 1_799_999_940, "iss": "4550664"},
        )
        self.assertEqual(signature, "c2lnbmF0dXJl")
        self.assertEqual(captured["private_key"], "private")

    def test_mint_installation_token_pins_repository_and_permissions(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse({"token": "installation-token", "expires_at": "2030-01-01T00:00:00Z"})

        result = BROKER.mint_installation_token(
            jwt="app-jwt",
            installation_id=152740425,
            repository="mpolatcan/hermes-setup",
            opener=opener,
        )
        self.assertEqual(result, "installation-token")
        self.assertEqual(
            captured["url"],
            "https://api.github.com/app/installations/152740425/access_tokens",
        )
        self.assertEqual(captured["headers"]["Authorization"], "Bearer app-jwt")
        self.assertEqual(
            captured["body"],
            {
                "permissions": {
                    "actions": "read",
                    "contents": "write",
                    "pull_requests": "write",
                },
                "repositories": ["hermes-setup"],
            },
        )
        self.assertEqual(captured["timeout"], 30)

    def test_verify_installation_pins_owner_selection_and_permissions(self):
        def valid_opener(_request, timeout):
            self.assertEqual(timeout, 30)
            return FakeResponse(
                {
                    "account": {"login": "mpolatcan"},
                    "target_type": "User",
                    "repository_selection": "selected",
                    "permissions": {
                        "actions": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                    },
                }
            )

        BROKER.verify_installation(
            jwt="app-jwt",
            installation_id=152740425,
            opener=valid_opener,
        )

        def wrong_owner(_request, _timeout):
            return FakeResponse(
                {
                    "account": {"login": "other"},
                    "target_type": "User",
                    "repository_selection": "selected",
                    "permissions": {
                        "actions": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                    },
                }
            )

        with self.assertRaises(BROKER.BrokerError):
            BROKER.verify_installation(
                jwt="app-jwt",
                installation_id=152740425,
                opener=wrong_owner,
            )

    def test_build_child_environment_strips_credentials_and_sets_only_gh_token(self):
        child = BROKER.build_child_environment(
            {
                "HOME": "/Users/test",
                "PATH": "/usr/bin:/bin",
                "OP_SERVICE_ACCOUNT_TOKEN": "bootstrap",
                "GH_TOKEN": "stale",
                "GITHUB_TOKEN": "stale-two",
                "UNRELATED_API_KEY": "must-not-propagate",
            },
            "fresh-installation-token",
            gh_config_dir="/private/tmp/isolated-gh-config",
        )
        self.assertEqual(child["GH_TOKEN"], "fresh-installation-token")
        self.assertEqual(child["HOME"], "/Users/mutlupolatcan")
        self.assertEqual(child["GH_CONFIG_DIR"], "/private/tmp/isolated-gh-config")
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", child)
        self.assertNotIn("GITHUB_TOKEN", child)
        self.assertNotIn("UNRELATED_API_KEY", child)

    def test_validate_command_allows_only_pinned_gh_binary(self):
        self.assertEqual(
            BROKER.validate_command(
                ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup"]
            ),
            ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup"],
        )
        self.assertEqual(
            BROKER.validate_command(["/opt/homebrew/bin/gh", "auth", "status"]),
            ["/opt/homebrew/bin/gh", "auth", "status"],
        )
        create_pr = [
            "/opt/homebrew/bin/gh",
            "api",
            "repos/mpolatcan/hermes-setup/pulls",
            "-X",
            "POST",
            "-f",
            "title=OPS-69",
            "-f",
            "head=fix/ops69-plan-nonsparse-source",
            "-f",
            "base=main",
        ]
        self.assertEqual(BROKER.validate_command(create_pr), create_pr)
        for command in (
            [],
            ["gh", "api", "user"],
            ["/bin/sh", "-c", "env"],
            ["/opt/homebrew/bin/gh", "auth", "token"],
            ["/opt/homebrew/bin/gh", "alias", "list"],
            ["/opt/homebrew/bin/gh", "extension", "list"],
            ["/opt/homebrew/bin/gh", "pr", "list"],
            ["/opt/homebrew/bin/gh", "api", "https://example.com/leak"],
            ["/opt/homebrew/bin/gh", "api", "user", "--hostname", "example.com"],
            ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup/../other"],
            ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup/%2e%2e/other"],
            ["/opt/homebrew/bin/gh", "api", "installation/repositories", "-XPOST"],
            ["/opt/homebrew/bin/gh", "api", "installation/repositories", "-f", "x=y"],
            ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup", "--input", "/etc/passwd"],
            ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup", "-F", "body=@/etc/passwd"],
            ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup", "--jq", "env.GH_TOKEN"],
            ["/opt/homebrew/bin/gh", "api", "repos/mpolatcan/hermes-setup", "--template", "{{env \"GH_TOKEN\"}}"],
        ):
            with self.subTest(command=command), self.assertRaises(BROKER.BrokerError):
                BROKER.validate_command(command)

    def test_auth_status_output_redacts_partial_and_complete_tokens(self):
        output = """github.com
  ✓ Logged in to account derya-hermes[bot]
  - Token: ghs_4550664_eyJhbGciOi...suffix
  diagnostic ghs_completeTokenValue
"""
        sanitized = BROKER.sanitize_gh_auth_status_output(output)
        self.assertNotIn("ghs_", sanitized)
        self.assertNotIn("suffix", sanitized)
        self.assertEqual(sanitized.count("[REDACTED]"), 2)

    @mock.patch.object(BROKER, "mint_installation_token", return_value="fresh-token")
    @mock.patch.object(BROKER, "verify_installation")
    @mock.patch.object(BROKER, "build_app_jwt", return_value="app-jwt")
    @mock.patch.object(BROKER, "resolve_references", new_callable=mock.AsyncMock)
    @mock.patch.object(BROKER.subprocess, "run")
    def test_main_sanitizes_auth_status_streams_and_preserves_exit_code(
        self,
        run_command,
        resolve_references,
        _build_jwt,
        _verify_installation,
        _mint_token,
    ):
        resolve_references.return_value = {
            "app_id": 4550664,
            "installation_id": 152740425,
            "repository": "mpolatcan/hermes-setup",
            "private_key": "private",
        }
        run_command.return_value = SimpleNamespace(
            stdout="  - Token: ghs_stdoutSecret\n",
            stderr="diagnostic ghs_stderrSecret\n",
            returncode=7,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {BROKER.TOKEN_ENV: "bootstrap"}, clear=False),
            mock.patch.object(BROKER.sys, "stdout", stdout),
            mock.patch.object(BROKER.sys, "stderr", stderr),
        ):
            result = BROKER.main(["--", BROKER.GH_BINARY, "auth", "status"])
        self.assertEqual(result, 7)
        self.assertNotIn("ghs_", stdout.getvalue() + stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "  - Token: [REDACTED]\n")
        self.assertEqual(stderr.getvalue(), "diagnostic [REDACTED]\n")
        _command, kwargs = run_command.call_args
        self.assertTrue(kwargs["text"])
        self.assertIs(kwargs["stdout"], BROKER.subprocess.PIPE)
        self.assertIs(kwargs["stderr"], BROKER.subprocess.PIPE)

    def test_parse_credential_request_accepts_scoped_https_github_request(self):
        request = (
            "protocol=https\n"
            "host=github.com\n"
            "path=mpolatcan/hermes-setup\n"
            "\n"
        )
        self.assertEqual(
            BROKER.parse_credential_request(request),
            {
                "protocol": "https",
                "host": "github.com",
                "path": "mpolatcan/hermes-setup",
            },
        )

    def test_parse_credential_request_rejects_malformed_or_duplicate_keys(self):
        for request in (
            "protocol=https\nhost=github.com\nnot-a-valid-line\n",
            "protocol=https\nprotocol=https\nhost=github.com\n",
            "protocol=https\nhost=github.com\n=emptykey\n",
        ):
            with self.subTest(request=request), self.assertRaises(BROKER.BrokerError):
                BROKER.parse_credential_request(request)

    def test_validate_credential_request_accepts_only_pinned_repository(self):
        self.assertEqual(
            BROKER.validate_credential_request(
                {
                    "protocol": "https",
                    "host": "github.com",
                    "path": "mpolatcan/hermes-setup",
                }
            ),
            "mpolatcan/hermes-setup",
        )
        self.assertEqual(
            BROKER.validate_credential_request(
                {
                    "protocol": "https",
                    "host": "github.com",
                    "path": "mpolatcan/hermes-setup.git",
                }
            ),
            "mpolatcan/hermes-setup",
        )

        invalid_requests = (
            {"protocol": "http", "host": "github.com", "path": "mpolatcan/hermes-setup"},
            {"protocol": "https", "host": "gitlab.com", "path": "mpolatcan/hermes-setup"},
            {"protocol": "https", "host": "github.com", "path": "mpolatcan/other"},
            {"protocol": "https", "host": "github.com", "path": "mpolatcan"},
            {"protocol": "https", "host": "github.com", "path": "mpolatcan/hermes-setup/.."},
            {
                "protocol": "https",
                "host": "github.com",
                "path": "mpolatcan/hermes-setup",
                "extra": "field",
            },
        )
        for fields in invalid_requests:
            with self.subTest(fields=fields), self.assertRaises(BROKER.BrokerError):
                BROKER.validate_credential_request(fields)

    def test_emit_credential_response_writes_token_only_to_stdout_protocol(self):
        response = BROKER.emit_credential_response("installation-token-123")
        self.assertIn("protocol=https\n", response)
        self.assertIn("host=github.com\n", response)
        self.assertIn("path=mpolatcan/hermes-setup\n", response)
        self.assertIn("username=x-access-token\n", response)
        self.assertIn("password=installation-token-123\n", response)
        with self.assertRaises(BROKER.BrokerError):
            BROKER.emit_credential_response("")

    @mock.patch.object(BROKER, "mint_installation_token", return_value="credential-token")
    @mock.patch.object(BROKER, "verify_installation")
    @mock.patch.object(BROKER, "build_app_jwt", return_value="app-jwt")
    @mock.patch.object(BROKER, "resolve_references", new_callable=mock.AsyncMock)
    def test_credential_get_prints_scoped_response_and_never_leaks_token_to_args(
        self,
        resolve_references,
        _build_jwt,
        _verify_installation,
        _mint_token,
    ):
        resolve_references.return_value = {
            "app_id": 4550664,
            "installation_id": 152740425,
            "repository": "mpolatcan/hermes-setup",
            "private_key": "private",
        }
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {BROKER.TOKEN_ENV: "bootstrap"}, clear=False),
            mock.patch.object(BROKER.sys, "stdin", io.StringIO("protocol=https\nhost=github.com\npath=mpolatcan/hermes-setup\n\n")),
            mock.patch.object(BROKER.sys, "stdout", stdout),
        ):
            result = BROKER.main(["--credential", "get"])
        self.assertEqual(result, 0)
        self.assertIn("username=x-access-token\n", stdout.getvalue())
        self.assertIn("password=credential-token\n", stdout.getvalue())
        self.assertNotIn("bootstrap", stdout.getvalue())

    @mock.patch.object(BROKER.sys, "stdin", io.StringIO("protocol=https\nhost=github.com\npath=mpolatcan/hermes-setup\n\n"))
    def test_credential_store_and_erase_are_noops(self):
        self.assertEqual(BROKER.main(["--credential", "store"]), 0)
        self.assertEqual(BROKER.main(["--credential", "erase"]), 0)

    def test_credential_dispatch_rejects_bad_usage(self):
        with self.assertRaises(BROKER.BrokerError):
            BROKER.main(["--credential"])
        with self.assertRaises(BROKER.BrokerError):
            BROKER.main(["--credential", "fill"])
        with self.assertRaises(BROKER.BrokerError):
            BROKER.main(["--credential", "get", "extra-argument"])


if __name__ == "__main__":
    unittest.main()
