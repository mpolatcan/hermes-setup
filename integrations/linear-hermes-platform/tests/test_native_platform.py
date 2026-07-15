from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import web

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "linear_native_test_plugin"
spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
assert spec is not None and spec.loader is not None
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE_NAME] = package
spec.loader.exec_module(package)

from gateway.config import Platform, PlatformConfig  # noqa: E402

adapter_mod = __import__(f"{PACKAGE_NAME}.adapter", fromlist=["*"])
client_mod = __import__(f"{PACKAGE_NAME}.linear_client", fromlist=["*"])
ledger_mod = __import__(f"{PACKAGE_NAME}.ledger", fromlist=["*"])

LinearPlatformAdapter = adapter_mod.LinearPlatformAdapter
build_agent_prompt = adapter_mod.build_agent_prompt
MessageEvent = adapter_mod.MessageEvent
MessageType = adapter_mod.MessageType
ProcessingOutcome = adapter_mod.ProcessingOutcome
LinearClient = client_mod.LinearClient
DeliveryLedger = ledger_mod.DeliveryLedger


class FakeRequest:
    def __init__(self, body: bytes, signature: str):
        self._body = body
        self.headers = {"Linear-Signature": signature}

    async def read(self) -> bytes:
        return self._body


class FakeLinear:
    def __init__(self, organization_id: str = "org-1"):
        self.organization_id = organization_id
        self.calls: list[tuple[str, str, str]] = []

    async def create_activity(self, session_id: str, activity_type: str, body: str) -> str:
        self.calls.append((session_id, activity_type, body))
        return f"activity-{len(self.calls)}"


class LedgerTests(unittest.TestCase):
    def test_claim_done_duplicate_and_stale_processing_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DeliveryLedger(
                str(Path(td) / "ledger.sqlite3"),
                processing_timeout_seconds=10,
                retention_seconds=100,
            )
            self.assertTrue(ledger.claim("webhook-one", now=100))
            self.assertFalse(ledger.claim("webhook-one", now=105))
            self.assertTrue(ledger.claim("webhook-one", now=111))
            ledger.mark_done("webhook-one", now=112)
            self.assertFalse(ledger.claim("webhook-one", now=500))
            ledger.close()

    def test_existing_bridge_check_schema_accepts_processing_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite3"
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE deliveries ("
                "webhook_id TEXT PRIMARY KEY, "
                "state TEXT NOT NULL CHECK(state IN ('processing', 'done')), "
                "updated_at INTEGER NOT NULL)"
            )
            db.commit()
            db.close()

            ledger = DeliveryLedger(str(path))
            self.assertTrue(ledger.claim("linear-event-legacy", now=100))
            state = ledger._db.execute(
                "SELECT state FROM deliveries WHERE webhook_id = ?",
                ("linear-event-legacy",),
            ).fetchone()[0]
            self.assertEqual(state, "processing")
            ledger.mark_done("linear-event-legacy", now=101)
            ledger.close()


class PromptTests(unittest.TestCase):
    def test_created_uses_prompt_context(self):
        text = build_agent_prompt(
            {
                "action": "created",
                "promptContext": "<issue>Do the concrete task</issue>",
                "agentSession": {"issue": {"identifier": "OPS-3", "title": "Test"}},
            }
        )
        self.assertIn("Do the concrete task", text)
        self.assertIn("OPS-3", text)

    def test_prompted_uses_agent_activity_body(self):
        text = build_agent_prompt(
            {
                "action": "prompted",
                "agentActivity": {"body": "Follow-up from the user"},
                "agentSession": {"issue": {"identifier": "OPS-3", "title": "Test"}},
            }
        )
        self.assertIn("Follow-up from the user", text)
        self.assertIn("User follow-up", text)

    def test_prompted_uses_nested_typed_activity_content_body(self):
        text = build_agent_prompt(
            {
                "action": "prompted",
                "agentActivity": {
                    "id": "activity-typed",
                    "content": {"type": "prompt", "body": "Native tamam"},
                },
                "agentSession": {"issue": {"identifier": "OPS-5", "title": "Test"}},
            }
        )
        self.assertIn("Native tamam", text)
        self.assertNotIn("(empty prompt)", text)


class AdapterWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp.name) / "ledger.sqlite3")
        config = PlatformConfig(enabled=True, extra={"database_path": db_path})
        self.adapter = LinearPlatformAdapter(config, Platform.WEBHOOK)
        self.adapter._signing_secrets = ("s" * 32, "p" * 32)
        self.adapter._linear = FakeLinear("org-1")
        self.adapter._ledger = DeliveryLedger(db_path)
        self.events = []

        async def capture(event):
            self.events.append(event)

        self.adapter.handle_message = capture

    async def asyncTearDown(self):
        pending = [task for tasks in self.adapter._ack_tasks.values() for task in tasks]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.adapter._ledger is not None:
            self.adapter._ledger.close()
            self.adapter._ledger = None
        self.temp.cleanup()

    def make_payload(self, **overrides):
        payload = {
            "type": "AgentSessionEvent",
            "action": "created",
            "webhookId": "webhook-native-123",
            "webhookTimestamp": int(time.time() * 1000),
            "organizationId": "org-1",
            "actor": {"id": "user-1", "name": "Mutlu"},
            "promptContext": "<issue>Native test request</issue>",
            "agentSession": {
                "id": "session-1",
                "issue": {"identifier": "OPS-3", "title": "Native adapter"},
            },
        }
        payload.update(overrides)
        return payload

    def request_for(self, payload, *, valid_signature=True, secret="s"):
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode() * 32, body, hashlib.sha256).hexdigest()
        if not valid_signature:
            signature = "0" * 64
        return FakeRequest(body, signature)

    async def test_valid_event_is_accepted_and_duplicate_is_suppressed(self):
        request = self.request_for(self.make_payload())
        response = await self.adapter._handle_webhook(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].source.chat_id, "session-1")
        self.assertTrue(self.events[0].source.role_authorized)
        await asyncio.sleep(0)
        self.assertEqual(self.adapter._linear.calls[0][1], "thought")

        duplicate_payload = self.make_payload(webhookTimestamp=int(time.time() * 1000) + 1)
        duplicate = await self.adapter._handle_webhook(self.request_for(duplicate_payload))
        self.assertEqual(duplicate.status, 200)
        self.assertEqual(json.loads(duplicate.text)["status"], "duplicate")
        self.assertEqual(len(self.events), 1)

    async def test_same_subscription_id_accepts_distinct_agent_sessions(self):
        first = self.make_payload(
            agentSession={
                "id": "session-1",
                "issue": {"identifier": "OPS-3", "title": "First"},
            }
        )
        second = self.make_payload(
            agentSession={
                "id": "session-2",
                "issue": {"identifier": "OPS-5", "title": "Second"},
            }
        )
        first_response = await self.adapter._handle_webhook(self.request_for(first))
        second_response = await self.adapter._handle_webhook(self.request_for(second))
        self.assertEqual(first_response.status, 200)
        self.assertEqual(second_response.status, 200)
        self.assertEqual([event.source.chat_id for event in self.events], ["session-1", "session-2"])

    async def test_same_session_accepts_distinct_prompt_activity_ids(self):
        first = self.make_payload(
            action="prompted",
            agentActivity={"id": "activity-1", "body": "first prompt"},
        )
        second = self.make_payload(
            action="prompted",
            agentActivity={"id": "activity-2", "body": "second prompt"},
        )
        first_response = await self.adapter._handle_webhook(self.request_for(first))
        second_response = await self.adapter._handle_webhook(self.request_for(second))
        self.assertEqual(first_response.status, 200)
        self.assertEqual(second_response.status, 200)
        self.assertEqual(len(self.events), 2)
        self.assertIn("first prompt", self.events[0].text)
        self.assertIn("second prompt", self.events[1].text)

    async def test_previous_signing_secret_is_accepted_during_rotation(self):
        payload = self.make_payload(webhookId="webhook-previous-secret")
        response = await self.adapter._handle_webhook(self.request_for(payload, secret="p"))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.events), 1)

    async def test_stop_signal_maps_to_canonical_command_without_thought(self):
        payload = self.make_payload(
            webhookId="webhook-stop-signal",
            action="prompted",
            agentActivity={"body": "stop", "signal": "stop"},
        )
        response = await self.adapter._handle_webhook(self.request_for(payload))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.text, "/stop")
        self.assertEqual(event.message_type, adapter_mod.MessageType.COMMAND)
        self.assertEqual(event.metadata["linear_signal"], "stop")
        await asyncio.sleep(0)
        self.assertEqual(self.adapter._linear.calls, [])

    async def test_invalid_signature_stale_and_wrong_organization_fail_closed(self):
        invalid = await self.adapter._handle_webhook(
            self.request_for(self.make_payload(webhookId="webhook-invalid-1"), valid_signature=False)
        )
        self.assertEqual(invalid.status, 401)

        stale_payload = self.make_payload(
            webhookId="webhook-stale-1",
            webhookTimestamp=int((time.time() - 120) * 1000),
        )
        stale = await self.adapter._handle_webhook(self.request_for(stale_payload))
        self.assertEqual(stale.status, 401)

        wrong_org = await self.adapter._handle_webhook(
            self.request_for(self.make_payload(webhookId="webhook-org-1", organizationId="org-2"))
        )
        self.assertEqual(wrong_org.status, 403)
        self.assertEqual(self.events, [])

    async def test_send_preserves_full_response_without_500_character_hook_limit(self):
        body = "x" * 5000
        result = await self.adapter.send("session-1", body)
        self.assertTrue(result.success)
        self.assertEqual(self.adapter._linear.calls[-1], ("session-1", "response", body))

    async def test_cancelled_processing_does_not_write_duplicate_error_activity(self):
        event = MessageEvent(
            text="/stop",
            message_type=MessageType.COMMAND,
            source=self.adapter.build_source(
                chat_id="session-stop",
                chat_name="OPS-5 — Test",
                chat_type="dm",
                user_id="user-1",
                user_name="Mutlu",
                role_authorized=True,
            ),
        )
        await self.adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)
        self.assertEqual(self.adapter._linear.calls, [])

    async def test_failed_processing_still_writes_error_activity(self):
        event = MessageEvent(
            text="task",
            message_type=MessageType.TEXT,
            source=self.adapter.build_source(
                chat_id="session-failure",
                chat_name="OPS-5 — Test",
                chat_type="dm",
                user_id="user-1",
                user_name="Mutlu",
                role_authorized=True,
            ),
        )
        await self.adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
        self.assertEqual(
            self.adapter._linear.calls,
            [("session-failure", "error", "Hermes encountered an error while processing the task.")],
        )


class OAuthRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_access_token_refreshes_rotates_and_writes_activity(self):
        calls = {"refresh": 0, "activities": []}

        async def token_handler(request):
            form = await request.post()
            self.assertEqual(form["grant_type"], "refresh_token")
            self.assertEqual(form["client_id"], "client-1")
            calls["refresh"] += 1
            return web.json_response(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "app:assignable app:mentionable read write",
                }
            )

        async def graphql_handler(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer new-access")
            payload = await request.json()
            if "LinearNativeIdentity" in payload["query"]:
                return web.json_response(
                    {
                        "data": {
                            "viewer": {"id": "actor-1", "name": "Derya"},
                            "organization": {"id": "org-1", "name": "Studio"},
                        }
                    }
                )
            calls["activities"].append(payload["variables"]["input"])
            return web.json_response(
                {
                    "data": {
                        "agentActivityCreate": {
                            "success": True,
                            "agentActivity": {"id": "activity-1"},
                        }
                    }
                }
            )

        app = web.Application()
        app.router.add_post("/oauth/token", token_handler)
        app.router.add_post("/graphql", graphql_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        sockets = getattr(server, "sockets", None)
        assert sockets
        port = sockets[0].getsockname()[1]

        old_token_url = client_mod.LINEAR_TOKEN_URL
        old_graphql_url = client_mod.LINEAR_GRAPHQL_URL
        client_mod.LINEAR_TOKEN_URL = f"http://127.0.0.1:{port}/oauth/token"
        client_mod.LINEAR_GRAPHQL_URL = f"http://127.0.0.1:{port}/graphql"
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "oauth.json"
                path.write_text(
                    json.dumps(
                        {
                            "access_token": "expired-access",
                            "refresh_token": "old-refresh",
                            "oauth_client_id": "client-1",
                            "expires_at": 1,
                        }
                    )
                )
                os.chmod(path, 0o600)
                client = LinearClient(str(path))
                await client.connect()
                activity_id = await client.create_activity("session-1", "response", "full response")
                await client.close()
                stored = json.loads(path.read_text())
                self.assertEqual(calls["refresh"], 1)
                self.assertEqual(stored["refresh_token"], "new-refresh")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(activity_id, "activity-1")
                self.assertEqual(
                    calls["activities"][0]["content"],
                    {"type": "response", "body": "full response"},
                )
        finally:
            client_mod.LINEAR_TOKEN_URL = old_token_url
            client_mod.LINEAR_GRAPHQL_URL = old_graphql_url
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
