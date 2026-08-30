from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from oauth_store import LinearAPIError, LinearOAuthStore  # noqa: E402
from linear_client import LinearClient  # noqa: E402


class LinearOAuthStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.oauth_path = Path(self.tempdir.name) / "linear-oauth.json"
        self.refresh_calls = 0

        async def token_handler(request: web.Request) -> web.Response:
            form = await request.post()
            self.assertEqual(form["grant_type"], "refresh_token")
            self.assertEqual(form["client_id"], "client-1")
            self.refresh_calls += 1
            await asyncio.sleep(0.05)
            return web.json_response(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": "read write app:assignable app:mentionable",
                }
            )

        app = web.Application()
        app.router.add_post("/oauth/token", token_handler)

        async def redirect_handler(_request):
            return web.Response(status=307, headers={"Location": "/oauth/token"})

        app.router.add_post("/oauth/redirect", redirect_handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        server = self.site._server
        assert server is not None and server.sockets
        port = server.sockets[0].getsockname()[1]
        self.token_url = f"http://127.0.0.1:{port}/oauth/token"
        self.redirect_token_url = f"http://127.0.0.1:{port}/oauth/redirect"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()
        self.tempdir.cleanup()

    def write_credential(self, **overrides) -> None:
        payload = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "oauth_client_id": "client-1",
            "expires_at": 1,
        }
        payload.update(overrides)
        self.oauth_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(self.oauth_path, 0o600)

    async def test_rejects_group_or_world_readable_credential(self):
        self.write_credential(expires_at=9999999999)
        os.chmod(self.oauth_path, 0o644)
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        with self.assertRaisesRegex(LinearAPIError, "permissions are too broad"):
            await store.access_token()

    async def test_rejects_symlinked_credential_file(self):
        self.write_credential(expires_at=9999999999)
        linked_path = self.oauth_path.with_name("linked-oauth.json")
        linked_path.symlink_to(self.oauth_path)
        store = LinearOAuthStore(str(linked_path), token_url=self.token_url)
        with self.assertRaisesRegex(LinearAPIError, "symlink"):
            await store.access_token()

    async def test_parent_alias_is_canonicalized_before_credential_use(self):
        root = Path(self.tempdir.name)
        real_parent = root / "real"
        real_parent.mkdir(mode=0o700)
        alias_parent = root / "alias"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        credential = real_parent / "credential.json"
        credential.write_text(
            json.dumps(
                {
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "oauth_client_id": "client-1",
                    "expires_at": 9999999999,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(credential, 0o600)
        store = LinearOAuthStore(str(alias_parent / "credential.json"), token_url=self.token_url)
        self.assertEqual(store.oauth_file, real_parent.resolve() / "credential.json")
        self.assertEqual(await store.access_token(), "old-access")

    async def test_credential_read_uses_no_follow_descriptor(self):
        self.write_credential(expires_at=9999999999)
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        with mock.patch("oauth_store.os.open", wraps=os.open) as opened:
            self.assertEqual(await store.access_token(), "old-access")
        credential_opens = [
            call for call in opened.call_args_list if Path(call.args[0]) == store.oauth_file
        ]
        self.assertEqual(len(credential_opens), 1)
        self.assertTrue(credential_opens[0].args[1] & os.O_NOFOLLOW)

    async def test_rejects_symlinked_lock_file(self):
        self.write_credential(expires_at=0)
        lock_target = self.oauth_path.with_name("lock-target")
        lock_target.write_text("do-not-touch", encoding="utf-8")
        Path(f"{self.oauth_path}.lock").symlink_to(lock_target)
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        with self.assertRaisesRegex(LinearAPIError, "lock"):
            await store.access_token()
        self.assertEqual(lock_target.read_text(encoding="utf-8"), "do-not-touch")

    async def test_fresh_token_does_not_refresh(self):
        self.write_credential(expires_at=9999999999)
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        token = await store.access_token()
        self.assertEqual(token, "old-access")
        self.assertEqual(self.refresh_calls, 0)

    async def test_two_store_instances_refresh_once_and_share_rotation(self):
        self.write_credential()
        first = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        second = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)

        one, two = await asyncio.gather(first.access_token(), second.access_token())

        self.assertEqual((one, two), ("new-access", "new-access"))
        self.assertEqual(self.refresh_calls, 1)
        stored = json.loads(self.oauth_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["access_token"], "new-access")
        self.assertEqual(stored["refresh_token"], "new-refresh")
        self.assertEqual(self.oauth_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.oauth_path.with_suffix(".json.lock").stat().st_mode & 0o777, 0o600)

    async def test_missing_refresh_material_fails_without_clobber(self):
        self.write_credential(refresh_token="")
        before = self.oauth_path.read_bytes()
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        with self.assertRaisesRegex(LinearAPIError, "refresh_token or client_id is missing"):
            await store.access_token()
        self.assertEqual(self.oauth_path.read_bytes(), before)
        self.assertEqual(self.refresh_calls, 0)

    async def test_error_never_contains_token_values(self):
        self.write_credential(refresh_token="secret-refresh-value", oauth_client_id="")
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        with self.assertRaises(LinearAPIError) as caught:
            await store.access_token()
        rendered = str(caught.exception)
        self.assertNotIn("secret-refresh-value", rendered)
        self.assertNotIn("old-access", rendered)

    async def test_oauth_refresh_rejects_redirects(self):
        self.write_credential(expires_at=1)
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.redirect_token_url)
        with self.assertRaisesRegex(LinearAPIError, "HTTP 307"):
            await store.access_token()
        self.assertEqual(self.refresh_calls, 0)
    async def test_refresh_does_not_path_chmod_after_atomic_replace(self):
        self.write_credential(expires_at=1)
        store = LinearOAuthStore(str(self.oauth_path), token_url=self.token_url)
        with mock.patch("oauth_store.os.chmod", side_effect=AssertionError("path chmod")):
            self.assertEqual(await store.access_token(), "new-access")


class LinearClientSharedStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_graphql_401_refreshes_through_injected_store_once(self):
        calls = []

        class FakeStore:
            def __init__(self):
                self.token = "old-access"

            async def access_token(self, *, force_refresh=False, stale_token=None):
                calls.append((force_refresh, stale_token))
                if force_refresh:
                    self.token = "new-access"
                return self.token

        async def graphql_handler(request: web.Request) -> web.Response:
            if request.headers.get("Authorization") == "Bearer old-access":
                return web.json_response({"error": "expired"}, status=401)
            return web.json_response(
                {
                    "data": {
                        "viewer": {"id": "actor-1", "name": "Derya"},
                        "organization": {"id": "org-1", "name": "Studio"},
                    }
                }
            )

        app = web.Application()
        app.router.add_post("/graphql", graphql_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None and server.sockets
        port = server.sockets[0].getsockname()[1]

        import linear_client as client_mod

        old_url = client_mod.LINEAR_GRAPHQL_URL
        client_mod.LINEAR_GRAPHQL_URL = f"http://127.0.0.1:{port}/graphql"
        client = LinearClient(oauth_store=FakeStore())
        try:
            await client.connect()
            self.assertEqual(client.actor_name, "Derya")
            self.assertEqual(
                calls,
                [(False, None), (True, "old-access"), (False, None)],
            )
        finally:
            await client.close()
            client_mod.LINEAR_GRAPHQL_URL = old_url
            await runner.cleanup()

    async def test_graphql_rejects_redirects(self):
        target_calls = 0

        class FakeStore:
            async def access_token(self, **_kwargs):
                return "access"

        async def redirect_handler(_request):
            return web.Response(status=307, headers={"Location": "/target"})

        async def target_handler(_request):
            nonlocal target_calls
            target_calls += 1
            return web.json_response({"data": {}})

        app = web.Application()
        app.router.add_post("/graphql", redirect_handler)
        app.router.add_post("/target", target_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None and server.sockets
        port = server.sockets[0].getsockname()[1]
        import linear_client as client_mod

        old_url = client_mod.LINEAR_GRAPHQL_URL
        client_mod.LINEAR_GRAPHQL_URL = f"http://127.0.0.1:{port}/graphql"
        client = LinearClient(oauth_store=FakeStore())
        try:
            with self.assertRaisesRegex(LinearAPIError, "HTTP 307"):
                await client.connect()
            self.assertEqual(target_calls, 0)
        finally:
            await client.close()
            client_mod.LINEAR_GRAPHQL_URL = old_url
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
