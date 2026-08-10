import base64
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
