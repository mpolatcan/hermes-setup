from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "linear_mobile_pkce_once.py"
SPEC = importlib.util.spec_from_file_location("linear_mobile_pkce_once", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
mobile_pkce = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mobile_pkce
SPEC.loader.exec_module(mobile_pkce)


def issue_http_request(
    server: object,
    path: str,
    host: str = "defne-linear.mutlupolatcan.com",
    *,
    method: str = "GET",
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    client_socket, handler_socket = socket.socketpair()
    try:
        rendered_extra_headers = "".join(
            f"{name}: {value}\r\n" for name, value in extra_headers
        )
        request = (
            f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"{rendered_extra_headers}"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        client_socket.sendall(request)
        mobile_pkce.MobileCallbackHandler(
            handler_socket, ("127.0.0.1", 0), server
        )
        response = http.client.HTTPResponse(client_socket)
        setattr(response, "_method", method)
        response.begin()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        handler_socket.close()
        client_socket.close()


class MobilePkceFlowTests(unittest.TestCase):
    def test_start_uses_https_callback_without_exposing_verifier(self) -> None:
        tokens = iter(["state-token", "verifier-token"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )

        authorization_url = flow.begin()
        parsed = urllib.parse.urlparse(authorization_url)
        query = urllib.parse.parse_qs(parsed.query)
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(b"verifier-token").digest()
        ).rstrip(b"=").decode("ascii")

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "linear.app")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(
            query["redirect_uri"],
            ["https://defne-linear.mutlupolatcan.com/oauth/callback"],
        )
        self.assertEqual(query["state"], ["state-token"])
        self.assertEqual(query["code_challenge"], [expected_challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("code_verifier", query)

    def test_repeated_start_is_idempotent_and_does_not_rotate_state(self) -> None:
        tokens = iter(["stable-state", "stable-verifier"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )

        first = flow.begin()
        second = flow.begin()

        self.assertEqual(second, first)
        self.assertEqual(flow.state, "stable-state")
        self.assertEqual(flow.verifier, "stable-verifier")

    def test_public_base_url_requires_exact_https_oauth_path(self) -> None:
        invalid_urls = [
            "https://user@defne-linear.mutlupolatcan.com/oauth",
            "https://defne-linear.mutlupolatcan.com:444/oauth",
            "https://defne-linear.mutlupolatcan.com",
            "https://defne-linear.mutlupolatcan.com/other",
            "https://Defne-linear.mutlupolatcan.com/oauth",
            "https://defne_linear.mutlupolatcan.com/oauth",
            "https://-defne.mutlupolatcan.com/oauth",
            "https://defne-.mutlupolatcan.com/oauth",
            "https://defne..mutlupolatcan.com/oauth",
        ]
        for public_base_url in invalid_urls:
            with self.subTest(public_base_url=public_base_url):
                with self.assertRaises(ValueError):
                    mobile_pkce.MobilePkceFlow(
                        client_id="linear-client-id-123",
                        public_base_url=public_base_url,
                    )

    def test_public_base_url_rejects_hostname_output_injection(self) -> None:
        for character in ("\r", "\n", "\t", " "):
            public_base_url = (
                f"https://defne{character}linear.mutlupolatcan.com/oauth"
            )
            with self.subTest(character=repr(character)):
                with self.assertRaises(ValueError):
                    mobile_pkce.MobilePkceFlow(
                        client_id="linear-client-id-123",
                        public_base_url=public_base_url,
                    )

    def test_public_base_url_is_reconstructed_canonically(self) -> None:
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
        )

        self.assertEqual(
            flow.public_base_url,
            "https://defne-linear.mutlupolatcan.com/oauth",
        )

    def test_callback_validates_state_and_is_consumed_exactly_once(self) -> None:
        tokens = iter(["expected-state", "server-only-verifier"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )
        flow.begin()

        with self.assertRaisesRegex(ValueError, "state mismatch"):
            flow.accept_callback({"state": "wrong-state", "code": "ignored-code"})

        grant = flow.accept_callback(
            {"state": "expected-state", "code": "authorization-code"}
        )
        self.assertEqual(grant.code, "authorization-code")
        self.assertEqual(grant.verifier, "server-only-verifier")
        self.assertEqual(
            grant.redirect_uri,
            "https://defne-linear.mutlupolatcan.com/oauth/callback",
        )

        with self.assertRaisesRegex(RuntimeError, "already completed"):
            flow.accept_callback(
                {"state": "expected-state", "code": "authorization-code"}
            )

    def test_http_start_is_fail_closed_by_host_and_path(self) -> None:
        tokens = iter(["state-token", "verifier-token"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0), flow, bind_and_activate=False
        )

        def request(path: str, host: str) -> tuple[int, dict[str, str], bytes]:
            return issue_http_request(server, path, host)

        try:
            wrong_host, _, _ = request(server.start_path, "wrong.example.com")
            wrong_post_host, _, _ = issue_http_request(
                server,
                server.start_path,
                "wrong.example.com",
                method="POST",
            )
            wrong_path, _, _ = request(
                "/not-oauth", "defne-linear.mutlupolatcan.com"
            )
            preview_status, preview_headers, preview_body = request(
                server.start_path, "defne-linear.mutlupolatcan.com"
            )
            state_after_preview = flow.state
            status, headers, body = issue_http_request(
                server,
                server.start_path,
                method="POST",
            )
        finally:
            server.server_close()

        self.assertEqual(wrong_host, 404)
        self.assertEqual(wrong_post_host, 404)
        self.assertEqual(wrong_path, 404)
        self.assertEqual(preview_status, 200)
        self.assertEqual(preview_headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Continue to Linear", preview_body)
        self.assertIsNone(state_after_preview)
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("https://linear.app/oauth/authorize?"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Continue to Linear", body)
        self.assertIn(b"https://linear.app/oauth/authorize?", body)
        self.assertNotIn(b"verifier-token", body)
        self.assertIsNone(server.grant)

    def test_http_start_requires_exact_one_shot_capability(self) -> None:
        tokens = iter(["state-token", "verifier-token"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0),
            flow,
            capability_factory=lambda: "unguessable-capability",
            bind_and_activate=False,
        )

        def request(path: str, *, method: str = "GET") -> tuple[int, str | None]:
            status, headers, _ = issue_http_request(server, path, method=method)
            return status, headers.get("Location")

        try:
            absent, absent_location = request("/oauth/start")
            wrong, wrong_location = request("/oauth/start/wrong-capability")
            wrong_query, wrong_query_location = request(
                f"{server.start_path}?preview=1", method="POST"
            )
            preview, preview_location = request(server.start_path)
            accepted, authorization_url = request(server.start_path, method="POST")
            replayed, replayed_location = request(server.start_path, method="POST")
            replayed_get, replayed_get_location = request(server.start_path)
        finally:
            server.server_close()

        self.assertEqual(
            (absent, wrong, wrong_query, preview, accepted, replayed, replayed_get),
            (404, 404, 404, 200, 303, 404, 404),
        )
        self.assertIsNone(absent_location)
        self.assertIsNone(wrong_location)
        self.assertIsNone(wrong_query_location)
        self.assertIsNone(preview_location)
        self.assertIsNone(replayed_location)
        self.assertIsNone(replayed_get_location)
        self.assertIsNotNone(authorization_url)
        assert authorization_url is not None
        self.assertNotIn("unguessable-capability", authorization_url)
        self.assertNotIn("unguessable-capability", flow.redirect_uri)

    def test_http_rejects_malformed_host_port(self) -> None:
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0), flow, bind_and_activate=False
        )
        try:
            status, _, _ = issue_http_request(
                server,
                server.start_path,
                "defne-linear.mutlupolatcan.com:garbage",
            )
        finally:
            server.server_close()

        self.assertEqual(status, 404)

    def test_duplicate_host_headers_fail_closed_without_consuming_state(self) -> None:
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                flow = mobile_pkce.MobilePkceFlow(
                    client_id="linear-client-id-123",
                    public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
                )
                server = mobile_pkce.create_server(
                    ("127.0.0.1", 0),
                    flow,
                    capability_factory=lambda: "exact-capability",
                    bind_and_activate=False,
                )
                try:
                    status, _, _ = issue_http_request(
                        server,
                        server.start_path,
                        method=method,
                        extra_headers=(("Host", "wrong.example.com"),),
                    )
                finally:
                    server.server_close()
                self.assertEqual(status, 404)
                self.assertEqual(server.start_capability, "exact-capability")
                self.assertIsNone(flow.state)

        tokens = iter(["expected-state", "server-only-verifier"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0), flow, bind_and_activate=False
        )
        try:
            self.assertEqual(issue_http_request(server, server.start_path)[0], 200)
            self.assertEqual(
                issue_http_request(server, server.start_path, method="POST")[0],
                303,
            )
            status, _, _ = issue_http_request(
                server,
                "/oauth/callback?state=expected-state&code=authorization-code",
                extra_headers=(("Host", "wrong.example.com"),),
            )
        finally:
            server.server_close()
        self.assertEqual(status, 404)
        self.assertIsNone(server.grant)
        self.assertFalse(flow.completed)
        self.assertEqual(flow.state, "expected-state")

    def test_noncanonical_host_case_fails_closed(self) -> None:
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                flow = mobile_pkce.MobilePkceFlow(
                    client_id="linear-client-id-123",
                    public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
                )
                server = mobile_pkce.create_server(
                    ("127.0.0.1", 0), flow, bind_and_activate=False
                )
                try:
                    status, _, _ = issue_http_request(
                        server,
                        server.start_path,
                        "DEFNE-LINEAR.MUTLUPOLATCAN.COM",
                        method=method,
                    )
                finally:
                    server.server_close()
                self.assertEqual(status, 404)
                self.assertIsNone(flow.state)

    def test_double_slash_raw_targets_never_consume_capability_or_state(self) -> None:
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                flow = mobile_pkce.MobilePkceFlow(
                    client_id="linear-client-id-123",
                    public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
                )
                server = mobile_pkce.create_server(
                    ("127.0.0.1", 0),
                    flow,
                    capability_factory=lambda: "exact-capability",
                    bind_and_activate=False,
                )
                try:
                    status, _, _ = issue_http_request(
                        server, "/" + server.start_path, method=method
                    )
                finally:
                    server.server_close()
                self.assertEqual(status, 404)
                self.assertEqual(server.start_capability, "exact-capability")
                self.assertIsNone(flow.state)

        tokens = iter(["expected-state", "server-only-verifier"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0), flow, bind_and_activate=False
        )
        try:
            self.assertEqual(issue_http_request(server, server.start_path)[0], 200)
            self.assertEqual(
                issue_http_request(server, server.start_path, method="POST")[0],
                303,
            )
            status, _, _ = issue_http_request(
                server,
                "//oauth/callback?state=expected-state&code=authorization-code",
            )
        finally:
            server.server_close()
        self.assertEqual(status, 404)
        self.assertIsNone(server.grant)
        self.assertEqual(flow.state, "expected-state")

    def test_unsupported_methods_include_security_headers(self) -> None:
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0), flow, bind_and_activate=False
        )
        try:
            for method in ("HEAD", "OPTIONS"):
                with self.subTest(method=method):
                    status, headers, _ = issue_http_request(
                        server, server.start_path, method=method
                    )
                    self.assertEqual(status, 501)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        finally:
            server.server_close()

    def test_start_requires_exact_raw_request_target(self) -> None:
        suffixes = ["?", ";params", "#fragment"]
        for method in ("GET", "POST"):
            for suffix in suffixes:
                with self.subTest(method=method, suffix=suffix):
                    flow = mobile_pkce.MobilePkceFlow(
                        client_id="linear-client-id-123",
                        public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
                    )
                    server = mobile_pkce.create_server(
                        ("127.0.0.1", 0),
                        flow,
                        capability_factory=lambda: "exact-capability",
                        bind_and_activate=False,
                    )
                    try:
                        status, _, _ = issue_http_request(
                            server,
                            f"{server.start_path}{suffix}",
                            method=method,
                        )
                    finally:
                        server.server_close()
                    self.assertEqual(status, 404)
                    self.assertEqual(server.start_capability, "exact-capability")
                    self.assertIsNone(flow.state)

        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0),
            flow,
            capability_factory=lambda: "exact-capability",
            bind_and_activate=False,
        )
        try:
            absolute_target = (
                "https://defne-linear.mutlupolatcan.com" + server.start_path
            )
            status, _, _ = issue_http_request(server, absolute_target, method="POST")
        finally:
            server.server_close()
        self.assertEqual(status, 404)
        self.assertEqual(server.start_capability, "exact-capability")
        self.assertIsNone(flow.state)

    def test_http_callback_accepts_one_matching_state(self) -> None:
        tokens = iter(["expected-state", "server-only-verifier"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0), flow, bind_and_activate=False
        )

        def request(path: str) -> int:
            status, _, _ = issue_http_request(server, path)
            return status

        try:
            self.assertEqual(request(server.start_path), 200)
            self.assertEqual(
                issue_http_request(server, server.start_path, method="POST")[0],
                303,
            )
            self.assertEqual(
                request("/oauth/callback?state=wrong&code=ignored"), 400
            )
            self.assertIsNone(server.grant)
            self.assertEqual(
                request(
                    "/oauth/callback?state=expected-state&code=authorization-code"
                ),
                200,
            )
            self.assertEqual(
                request(
                    "/oauth/callback?state=expected-state&code=authorization-code"
                ),
                409,
            )
        finally:
            server.server_close()

        self.assertIsNotNone(server.grant)
        self.assertEqual(server.grant.code, "authorization-code")
        self.assertEqual(server.grant.verifier, "server-only-verifier")

    def test_callback_rejects_raw_empty_params_and_fragment_delimiters(self) -> None:
        malformed_targets = [
            "/oauth/callback;?state=expected-state&code=authorization-code",
            "/oauth/callback?state=expected-state&code=authorization-code#",
        ]
        for target in malformed_targets:
            with self.subTest(target=target):
                tokens = iter(["expected-state", "server-only-verifier"])
                flow = mobile_pkce.MobilePkceFlow(
                    client_id="linear-client-id-123",
                    public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
                    token_factory=lambda: next(tokens),
                )
                server = mobile_pkce.create_server(
                    ("127.0.0.1", 0), flow, bind_and_activate=False
                )
                try:
                    self.assertEqual(
                        issue_http_request(server, server.start_path)[0], 200
                    )
                    self.assertEqual(
                        issue_http_request(
                            server, server.start_path, method="POST"
                        )[0],
                        303,
                    )
                    status, _, _ = issue_http_request(server, target)
                finally:
                    server.server_close()
                self.assertEqual(status, 404)
                self.assertIsNone(server.grant)
                self.assertFalse(flow.completed)
                self.assertEqual(flow.state, "expected-state")

    def test_matching_oauth_denial_is_terminal_but_wrong_state_is_not(self) -> None:
        tokens = iter(["expected-state", "server-only-verifier"])
        flow = mobile_pkce.MobilePkceFlow(
            client_id="linear-client-id-123",
            public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
            token_factory=lambda: next(tokens),
        )
        server = mobile_pkce.create_server(
            ("127.0.0.1", 0), flow, bind_and_activate=False
        )
        try:
            self.assertEqual(issue_http_request(server, server.start_path)[0], 200)
            self.assertEqual(
                issue_http_request(server, server.start_path, method="POST")[0],
                303,
            )
            self.assertEqual(
                issue_http_request(
                    server, "/oauth/callback?state=wrong&error=access_denied"
                )[0],
                400,
            )
            self.assertIsNone(server.terminal_error)
            self.assertEqual(
                issue_http_request(
                    server,
                    "/oauth/callback?state=expected-state&error=access_denied",
                )[0],
                403,
            )
            self.assertIsInstance(server.terminal_error, PermissionError)
        finally:
            server.server_close()


class OnePasswordClientIdTests(unittest.TestCase):
    def test_resolves_only_the_linear_client_id_field(self) -> None:
        reference = "op://vault-id/item-id/LINEAR_CLIENT_ID"

        class FakeSecrets:
            async def resolve_all(self, references: list[str]) -> SimpleNamespace:
                self.references = references
                return SimpleNamespace(
                    individual_responses={
                        reference: SimpleNamespace(
                            error=None,
                            content=SimpleNamespace(secret="valid-client-id-123"),
                        )
                    }
                )

        class FakeClient:
            secrets = FakeSecrets()

        class FakeClientType:
            @classmethod
            async def authenticate(cls, **kwargs: str) -> FakeClient:
                cls.auth_kwargs = kwargs
                return FakeClient()

        client_id = asyncio.run(
            mobile_pkce.resolve_client_id(
                reference,
                service_account_token="scoped-service-account-token",
                client_type=FakeClientType,
            )
        )

        self.assertEqual(client_id, "valid-client-id-123")
        self.assertEqual(FakeClient.secrets.references, [reference])
        self.assertEqual(
            FakeClientType.auth_kwargs["integration_name"],
            "Hermes Linear Mobile PKCE",
        )


class MobilePkceCredentialTests(unittest.TestCase):
    @staticmethod
    def _destination(root: Path, profile: str = "defne") -> Path:
        destination = root / profile / "credentials" / "linear-oauth.json"
        destination.parent.parent.mkdir(parents=True)
        return destination

    def test_viewer_query_requests_organization_identity(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "data": {
                            "viewer": {
                                "id": "app-user-1",
                                "name": "Defne",
                                "organization": {"id": "organization-1"},
                            }
                        }
                    }
                ).encode()

        with mock.patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            viewer = mobile_pkce.query_viewer_context("fake-access-token")

        request = urlopen.call_args.args[0]
        query = json.loads(request.data)["query"]
        self.assertIn("organization { id }", query)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-access-token")
        self.assertEqual(viewer["organization"]["id"], "organization-1")

    def test_exchange_writes_verified_profile_local_credential_as_0600(self) -> None:
        grant = mobile_pkce.AuthorizationGrant(
            code="authorization-code",
            verifier="server-only-verifier",
            redirect_uri="https://defne-linear.mutlupolatcan.com/oauth/callback",
        )
        token_response = {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "expires_in": 3600,
            "scope": "read write app:assignable app:mentionable",
        }

        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td).resolve() / "profiles"
            destination = self._destination(profiles_root)
            result = mobile_pkce.complete_install(
                grant=grant,
                client_id="linear-client-id-123",
                expected_organization_id="organization-1",
                destination=destination,
                profiles_root=profiles_root,
                post_form_fn=lambda url, values: token_response,
                query_viewer_fn=lambda token: {
                    "id": "app-user-1",
                    "name": "Defne",
                    "organization": {"id": "organization-1"},
                },
                now_fn=lambda: 1_800_000_000,
            )

            stored = json.loads(destination.read_text())
            mode = os.stat(destination).st_mode & 0o777

        self.assertEqual(mode, 0o600)
        self.assertEqual(stored["oauth_client_id"], "linear-client-id-123")
        self.assertEqual(stored["redirect_uri"], grant.redirect_uri)
        self.assertEqual(stored["app_user"], {"id": "app-user-1", "name": "Defne"})
        self.assertEqual(stored["organization_id"], "organization-1")
        self.assertEqual(stored["expires_at"], 1_800_003_600)
        self.assertEqual(result, {"app_user_id": "app-user-1", "app_user_name": "Defne", "granted_scopes": token_response["scope"]})

    def test_organization_mismatch_never_persists_credential(self) -> None:
        grant = mobile_pkce.AuthorizationGrant(
            code="authorization-code",
            verifier="server-only-verifier",
            redirect_uri="https://defne-linear.mutlupolatcan.com/oauth/callback",
        )
        token_response = {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "expires_in": 3600,
            "scope": "read write app:assignable app:mentionable",
        }

        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td) / "profiles"
            destination = self._destination(profiles_root)
            with self.assertRaisesRegex(PermissionError, "organization mismatch"):
                mobile_pkce.complete_install(
                    grant=grant,
                    client_id="linear-client-id-123",
                    expected_organization_id="organization-1",
                    destination=destination,
                    profiles_root=profiles_root,
                    post_form_fn=lambda url, values: token_response,
                    query_viewer_fn=lambda token: {
                        "id": "foreign-app-user",
                        "name": "Foreign",
                        "organization": {"id": "foreign-organization"},
                    },
                    now_fn=lambda: 1_800_000_000,
                )
            self.assertFalse(destination.exists())

    def test_rejects_destination_outside_profiles_root_and_unsafe_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td) / "profiles"
            profiles_root.mkdir()
            invalid = [
                Path(td) / "outside" / "credentials" / "linear-oauth.json",
                profiles_root / ".." / "escape" / "credentials" / "linear-oauth.json",
                profiles_root / "unsafe.profile" / "credentials" / "linear-oauth.json",
            ]
            for destination in invalid:
                with self.subTest(destination=destination):
                    with self.assertRaises(ValueError):
                        mobile_pkce.validate_credential_destination(
                            destination, profiles_root=profiles_root
                        )

    def test_rejects_symlinked_profile_or_credentials_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            profiles_root = base / "profiles"
            profiles_root.mkdir()
            real_profile = base / "real-profile"
            (real_profile / "credentials").mkdir(parents=True)
            (profiles_root / "linked-profile").symlink_to(
                real_profile, target_is_directory=True
            )

            with self.assertRaises(OSError):
                mobile_pkce.validate_credential_destination(
                    profiles_root
                    / "linked-profile"
                    / "credentials"
                    / "linear-oauth.json",
                    profiles_root=profiles_root,
                )

            profile = profiles_root / "defne"
            profile.mkdir()
            (profile / "credentials").symlink_to(
                real_profile / "credentials", target_is_directory=True
            )
            with self.assertRaises(OSError):
                mobile_pkce.validate_credential_destination(
                    profile / "credentials" / "linear-oauth.json",
                    profiles_root=profiles_root,
                )

    def test_atomic_install_does_not_clobber_destination_created_at_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td).resolve() / "profiles"
            destination = self._destination(profiles_root)
            real_link = os.link

            def race_link(
                src: str,
                dst: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
                follow_symlinks: bool,
            ) -> None:
                destination.write_text("racer-won")
                real_link(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with mock.patch.object(mobile_pkce.os, "link", side_effect=race_link):
                with self.assertRaises(FileExistsError):
                    mobile_pkce.atomic_install_json(
                        destination,
                        {"access_token": "must-not-overwrite"},
                        profiles_root=profiles_root,
                    )

            self.assertEqual(destination.read_text(), "racer-won")
            self.assertEqual(
                list(destination.parent.glob(".linear-oauth.json.*.tmp")), []
            )

    def test_atomic_install_rejects_symlinked_profiles_root_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            real_ancestor = base / "real-ancestor"
            profiles_root = real_ancestor / "profiles"
            destination = self._destination(profiles_root)
            linked_ancestor = base / "linked-ancestor"
            linked_ancestor.symlink_to(real_ancestor, target_is_directory=True)
            linked_root = linked_ancestor / "profiles"
            linked_destination = (
                linked_root / "defne" / "credentials" / "linear-oauth.json"
            )

            with self.assertRaises(OSError):
                mobile_pkce.atomic_install_json(
                    linked_destination,
                    {"access_token": "must-not-write"},
                    profiles_root=linked_root,
                )

            self.assertFalse(destination.exists())
            self.assertEqual(
                list(destination.parent.glob(".linear-oauth.json.*.tmp")), []
            )


class MobilePkceCliTests(unittest.TestCase):
    def test_cli_exposes_only_reference_based_client_id_input(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            check=True,
            text=True,
            capture_output=True,
        )

        self.assertIn("--client-id-reference", result.stdout)
        self.assertIn("--public-base-url", result.stdout)
        self.assertIn("--destination", result.stdout)
        self.assertIn("--bind-port", result.stdout)
        self.assertNotIn("--client-id-from-clipboard", result.stdout)
        self.assertNotRegex(result.stdout, r"(?m)^\s*--client-id\s")

    def test_runtime_validation_rejects_existing_credential(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td) / "profiles"
            destination = (
                profiles_root / "defne" / "credentials" / "linear-oauth.json"
            )
            destination.parent.mkdir(parents=True)
            destination.write_text("existing")
            args = SimpleNamespace(
                destination=destination,
                bind_port=8796,
                timeout_seconds=600,
            )

            with self.assertRaises(FileExistsError):
                mobile_pkce.validate_runtime_inputs(
                    args, profiles_root=profiles_root
                )

    def test_runtime_validation_requires_absolute_credentials_destination(self) -> None:
        invalid_destinations = [
            Path("credentials/linear-oauth.json"),
            Path("/tmp/linear-oauth.json"),
            Path("/tmp/credentials/other.json"),
        ]
        for destination in invalid_destinations:
            with self.subTest(destination=destination):
                args = SimpleNamespace(
                    destination=destination,
                    bind_port=8796,
                    timeout_seconds=600,
                )
                with self.assertRaises(ValueError):
                    mobile_pkce.validate_runtime_inputs(args)

    def test_runtime_validation_enforces_numeric_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td) / "profiles"
            destination = (
                profiles_root / "defne" / "credentials" / "linear-oauth.json"
            )
            invalid_values = [(0, 600), (65536, 600), (8796, 29), (8796, 1801)]
            for bind_port, timeout_seconds in invalid_values:
                with self.subTest(
                    bind_port=bind_port, timeout_seconds=timeout_seconds
                ):
                    args = SimpleNamespace(
                        destination=destination,
                        bind_port=bind_port,
                        timeout_seconds=timeout_seconds,
                    )
                    with self.assertRaises(ValueError):
                        mobile_pkce.validate_runtime_inputs(
                            args, profiles_root=profiles_root
                        )

    def test_run_keeps_secret_inputs_out_of_user_output(self) -> None:
        service_token = "fake-scoped-service-token"
        client_id = "fake-linear-client-id"
        environment = {"OP_SERVICE_ACCOUNT_TOKEN": service_token}
        output = io.StringIO()
        calls: dict[str, object] = {}

        async def fake_resolve(reference: str, **kwargs: str) -> str:
            calls["reference"] = reference
            calls["service_token"] = kwargs["service_account_token"]
            return client_id

        class FakeServer:
            def __init__(self) -> None:
                self.start_url = (
                    "https://defne-linear.mutlupolatcan.com/oauth/start/"
                    "start-capability"
                )
                self.grant = mobile_pkce.AuthorizationGrant(
                    code="authorization-code",
                    verifier="server-only-verifier",
                    redirect_uri=(
                        "https://defne-linear.mutlupolatcan.com/oauth/callback"
                    ),
                )
                self.timeout = None
                self.closed = False

            def handle_request(self) -> None:
                raise AssertionError("completed fake server must not block")

            def server_close(self) -> None:
                self.closed = True

        fake_server = FakeServer()

        def fake_create_server(address: tuple[str, int], flow: object) -> FakeServer:
            calls["address"] = address
            calls["flow"] = flow
            return fake_server

        def fake_complete_install(**kwargs: object) -> dict[str, str]:
            calls["complete"] = kwargs
            return {
                "app_user_id": "app-user-1\nMOBILE_PKCE_COMPLETE=false",
                "app_user_name": "Defne\nMOBILE_PKCE_COMPLETE=false",
                "granted_scopes": "read\twrite\nMOBILE_PKCE_COMPLETE=false",
            }

        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td) / "profiles"
            destination = (
                profiles_root / "defne" / "credentials" / "linear-oauth.json"
            )
            destination.parent.parent.mkdir(parents=True)
            args = SimpleNamespace(
                client_id_reference="op://vault/item/LINEAR_CLIENT_ID",
                public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
                expected_organization_id="organization-1",
                destination=destination,
                bind_port=8796,
                timeout_seconds=600,
            )
            result = mobile_pkce.run(
                args,
                environ=environment,
                resolve_client_id_fn=fake_resolve,
                create_server_fn=fake_create_server,
                complete_install_fn=fake_complete_install,
                output=output,
                profiles_root=profiles_root,
            )

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertEqual(environment, {})
        self.assertEqual(calls["service_token"], service_token)
        self.assertEqual(calls["address"], ("127.0.0.1", 8796))
        self.assertTrue(fake_server.closed)
        self.assertNotIn(service_token, rendered)
        self.assertNotIn(client_id, rendered)
        self.assertIn(
            'START_URL="https://defne-linear.mutlupolatcan.com/oauth/start/'
            'start-capability"',
            rendered,
        )
        self.assertIn(
            'APP_USER_ID="app-user-1\\nMOBILE_PKCE_COMPLETE=false"', rendered
        )
        self.assertIn("MOBILE_PKCE_COMPLETE=true", rendered)
        self.assertIn(
            'APP_USER_NAME_JSON="Defne\\nMOBILE_PKCE_COMPLETE=false"', rendered
        )
        self.assertEqual(
            sum(
                line.startswith("MOBILE_PKCE_COMPLETE=")
                for line in rendered.splitlines()
            ),
            1,
        )
        self.assertIn(
            'GRANTED_SCOPES="read\\twrite\\nMOBILE_PKCE_COMPLETE=false"',
            rendered,
        )
        self.assertNotIn("server-only-verifier", rendered)
        self.assertEqual(rendered.count("start-capability"), 1)
        for line in rendered.splitlines():
            _, encoded_value = line.split("=", 1)
            json.loads(encoded_value)

    def test_run_waits_until_callback_grant_arrives(self) -> None:
        async def fake_resolve(reference: str, **kwargs: str) -> str:
            return "fake-linear-client-id"

        class FakeServer:
            def __init__(self) -> None:
                self.start_url = (
                    "https://defne-linear.mutlupolatcan.com/oauth/start/"
                    "start-capability"
                )
                self.grant = None
                self.timeout = None
                self.handled = 0
                self.closed = False

            def handle_request(self) -> None:
                self.handled += 1
                self.grant = mobile_pkce.AuthorizationGrant(
                    code="authorization-code",
                    verifier="server-only-verifier",
                    redirect_uri=(
                        "https://defne-linear.mutlupolatcan.com/oauth/callback"
                    ),
                )

            def server_close(self) -> None:
                self.closed = True

        fake_server = FakeServer()
        with tempfile.TemporaryDirectory() as td:
            profiles_root = Path(td) / "profiles"
            destination = (
                profiles_root / "defne" / "credentials" / "linear-oauth.json"
            )
            destination.parent.parent.mkdir(parents=True)
            args = SimpleNamespace(
                client_id_reference="op://vault/item/LINEAR_CLIENT_ID",
                public_base_url="https://defne-linear.mutlupolatcan.com/oauth",
                expected_organization_id="organization-1",
                destination=destination,
                bind_port=8796,
                timeout_seconds=600,
            )
            result = mobile_pkce.run(
                args,
                environ={"OP_SERVICE_ACCOUNT_TOKEN": "fake-token"},
                resolve_client_id_fn=fake_resolve,
                create_server_fn=lambda address, flow: fake_server,
                complete_install_fn=lambda **kwargs: {
                    "app_user_id": "app-user-1",
                    "app_user_name": "Defne",
                    "granted_scopes": "read write app:assignable app:mentionable",
                },
                output=io.StringIO(),
                profiles_root=profiles_root,
            )

        self.assertEqual(result, 0)
        self.assertEqual(fake_server.handled, 1)
        self.assertEqual(fake_server.timeout, 1.0)
        self.assertTrue(fake_server.closed)

    def test_wait_for_grant_times_out_without_callback(self) -> None:
        class FakeServer:
            grant = None
            timeout = None

            def __init__(self) -> None:
                self.handled = 0

            def handle_request(self) -> None:
                self.handled += 1

        fake_server = FakeServer()
        times = iter([0.0, 0.0, 601.0])

        with self.assertRaises(TimeoutError):
            mobile_pkce.wait_for_grant(
                fake_server,
                timeout_seconds=600,
                monotonic_fn=lambda: next(times),
            )

        self.assertEqual(fake_server.handled, 1)
        self.assertEqual(fake_server.timeout, 1.0)

    def test_wait_for_grant_stops_immediately_on_terminal_oauth_denial(self) -> None:
        denial = PermissionError("sensitive-linear-error")

        class FakeServer:
            grant = None
            terminal_error = None
            timeout = None

            def __init__(self) -> None:
                self.handled = 0

            def handle_request(self) -> None:
                self.handled += 1
                self.terminal_error = denial

        fake_server = FakeServer()
        times = iter([0.0, 0.0])

        with self.assertRaises(PermissionError) as raised:
            mobile_pkce.wait_for_grant(
                fake_server,
                timeout_seconds=600,
                monotonic_fn=lambda: next(times),
            )

        self.assertIs(raised.exception, denial)
        self.assertEqual(fake_server.handled, 1)

    def test_main_redacts_exception_details(self) -> None:
        error_output = io.StringIO()

        def fail_with_sensitive_detail(args: object) -> int:
            raise ValueError("do-not-print-this-client-id")

        result = mobile_pkce.main(
            [
                "--client-id-reference",
                "op://vault/item/LINEAR_CLIENT_ID",
                "--public-base-url",
                "https://defne-linear.mutlupolatcan.com/oauth",
                "--expected-organization-id",
                "organization-1",
                "--destination",
                "/tmp/credentials/linear-oauth.json",
                "--bind-port",
                "8796",
            ],
            run_fn=fail_with_sensitive_detail,
            error_output=error_output,
        )

        rendered = error_output.getvalue()
        self.assertEqual(result, 1)
        self.assertEqual(
            rendered.strip(),
            'MOBILE_PKCE_ERROR="INPUT_OR_VERIFICATION_FAILED"',
        )
        self.assertNotIn("do-not-print-this-client-id", rendered)

    def test_main_redacts_malformed_sensitive_argument(self) -> None:
        sensitive_value = "secret-looking-client-id"
        error_output = io.StringIO()

        result = mobile_pkce.main(
            ["--bind-port", sensitive_value],
            error_output=error_output,
        )

        rendered = error_output.getvalue()
        self.assertEqual(result, 1)
        self.assertEqual(
            rendered.strip(),
            'MOBILE_PKCE_ERROR="INPUT_OR_VERIFICATION_FAILED"',
        )
        self.assertNotIn(sensitive_value, rendered)

    def test_main_maps_operational_failures_to_safe_markers(self) -> None:
        cases = [
            (FileExistsError, "DESTINATION_EXISTS"),
            (TimeoutError, "CALLBACK_TIMEOUT"),
            (PermissionError, "AUTHORIZATION_REJECTED"),
            (OSError, "LOCAL_LISTENER_OR_WRITE_FAILED"),
            (RuntimeError, "UNEXPECTED_FAILURE"),
        ]
        argv = [
            "--client-id-reference",
            "op://vault/item/LINEAR_CLIENT_ID",
            "--public-base-url",
            "https://defne-linear.mutlupolatcan.com/oauth",
            "--expected-organization-id",
            "organization-1",
            "--destination",
            "/tmp/credentials/linear-oauth.json",
            "--bind-port",
            "8796",
        ]
        for error_type, marker in cases:
            with self.subTest(error_type=error_type.__name__):
                error_output = io.StringIO()

                def fail(
                    args: object,
                    error_type: type[Exception] = error_type,
                ) -> int:
                    raise error_type("sensitive-detail")

                result = mobile_pkce.main(
                    argv,
                    run_fn=fail,
                    error_output=error_output,
                )
                rendered = error_output.getvalue()
                self.assertEqual(result, 1)
                self.assertEqual(
                    rendered.strip(),
                    f"MOBILE_PKCE_ERROR={json.dumps(marker)}",
                )
                self.assertNotIn("sensitive-detail", rendered)


if __name__ == "__main__":
    unittest.main()
