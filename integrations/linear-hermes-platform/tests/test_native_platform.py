from __future__ import annotations

import asyncio
import datetime as dt
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
import uuid
from pathlib import Path
from unittest import mock

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
LinearAPIError = client_mod.LinearAPIError
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
        self.actor_id = "agent-derya"
        self.calls: list[tuple[str, str, str]] = []
        self.blockers: dict[str, list[dict[str, str]]] = {}
        self.closure_contexts: dict[str, dict] = {}

    async def create_activity(
        self,
        session_id: str,
        activity_type: str,
        body: str,
        *,
        activity_id: str,
    ) -> str:
        self.calls.append((session_id, activity_type, body))
        return activity_id

    async def update_issue_state(self, issue_id, state_name, state_rank, state_ranks):
        self.calls.append((issue_id, "state", state_name))
        return f"state-{state_rank}"

    async def get_open_blockers(self, issue_id):
        return list(self.blockers.get(issue_id, []))

    async def get_issue_closure_context(self, issue_id):
        return dict(self.closure_contexts.get(issue_id, {}))


class LedgerTests(unittest.TestCase):
    def test_issue_session_binding_is_durable_and_tracks_latest_accepted_creation(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "bindings.sqlite3")
            ledger = DeliveryLedger(path)
            ledger.bind_issue_session("issue-1", "session-1", now=100)
            self.assertEqual(ledger.get_issue_session("issue-1"), "session-1")
            ledger.bind_issue_session("issue-1", "session-2", now=101)
            ledger.close()

            reopened = DeliveryLedger(path)
            self.assertEqual(reopened.get_issue_session("issue-1"), "session-2")
            reopened.close()

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

    def test_outbox_persists_and_reclaims_in_flight_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "outbox.sqlite3")
            ledger = DeliveryLedger(path, outbox_claim_timeout_seconds=10)
            self.assertTrue(
                ledger.enqueue_outbox(
                    "item-1",
                    "session-1",
                    "activity.create",
                    {"body": "durable"},
                    now=100,
                )
            )
            claimed = ledger.claim_due_outbox(now=100)
            self.assertIsNotNone(claimed)
            ledger.close()

            reopened = DeliveryLedger(path, outbox_claim_timeout_seconds=10)
            self.assertIsNone(reopened.claim_due_outbox(now=109))
            reclaimed = reopened.claim_due_outbox(now=111)
            self.assertEqual(reclaimed.id, "item-1")
            self.assertEqual(reclaimed.payload, {"body": "durable"})
            reopened.close()

    def test_outbox_orders_per_session_and_deduplicates_producer_retries(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DeliveryLedger(str(Path(td) / "outbox.sqlite3"))
            self.assertTrue(ledger.enqueue_outbox("first", "session-1", "one", {}, now=100))
            self.assertFalse(ledger.enqueue_outbox("first", "session-1", "one", {}, now=101))
            self.assertTrue(ledger.enqueue_outbox("second", "session-1", "two", {}, now=100))
            first = ledger.claim_due_outbox(now=100)
            self.assertEqual(first.id, "first")
            ledger.reschedule_outbox("first", "transient", 10, now=100)
            self.assertIsNone(ledger.claim_due_outbox(now=105))
            first_retry = ledger.claim_due_outbox(now=110)
            self.assertEqual(first_retry.id, "first")
            ledger.mark_outbox_delivered("first", now=110)
            second = ledger.claim_due_outbox(now=110)
            self.assertEqual(second.id, "second")
            ledger.close()

    def test_wait_persists_and_resume_claim_is_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "wait.sqlite3")
            payload = {"type": "AgentSessionEvent", "action": "created"}
            blockers = [{"id": "blocker-7", "identifier": "OPS-7", "state": "Todo"}]
            ledger = DeliveryLedger(path)
            ledger.put_wait("session-8", "issue-8", "delivery-8", payload, blockers, now=100)
            ledger.close()

            reopened = DeliveryLedger(path)
            wait = reopened.get_wait("session-8")
            self.assertEqual(wait["state"], "waiting")
            self.assertEqual(wait["blockers"], blockers)
            self.assertEqual(reopened.find_waiting_by_blocker("blocker-7")[0]["issue_id"], "issue-8")
            self.assertTrue(reopened.claim_wait("session-8", now=101))
            self.assertFalse(reopened.claim_wait("session-8", now=102))
            reopened.close()

            recovered = DeliveryLedger(path)
            self.assertEqual(recovered.get_wait("session-8")["state"], "waiting")
            self.assertIn("Recovered interrupted resume", recovered.get_wait("session-8")["last_error"])
            self.assertTrue(recovered.claim_wait("session-8", now=103))
            recovered.mark_wait_resumed("session-8", now=104)
            self.assertEqual(recovered.get_wait("session-8")["state"], "resumed")
            self.assertEqual(recovered._db.execute("PRAGMA user_version").fetchone()[0], 4)
            recovered.close()

    def test_closure_outbox_recovers_after_restart_without_duplicate_activity(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "closure.sqlite3")
            ledger = DeliveryLedger(path, outbox_claim_timeout_seconds=10)
            inserted = ledger.enqueue_closure_activity(
                "closure-1",
                "issue-1",
                "session-1",
                "activity-1",
                "Closure reconciliation complete.",
                {"actor_id": "human-1"},
                now=100,
            )
            self.assertTrue(inserted)
            self.assertFalse(
                ledger.enqueue_closure_activity(
                    "closure-1",
                    "issue-1",
                    "session-1",
                    "activity-1",
                    "Closure reconciliation complete.",
                    {"actor_id": "human-1"},
                    now=101,
                )
            )
            claimed = ledger.claim_due_outbox(now=100)
            self.assertEqual(claimed.id, "activity:closure:closure-1")
            ledger.close()

            recovered = DeliveryLedger(path, outbox_claim_timeout_seconds=10)
            self.assertIsNone(recovered.claim_due_outbox(now=109))
            replay = recovered.claim_due_outbox(now=111)
            self.assertEqual(replay.id, "activity:closure:closure-1")
            self.assertEqual(replay.payload["activity_id"], "activity-1")
            recovered.mark_outbox_delivered(replay.id, now=112)
            self.assertEqual(recovered.get_closure("closure-1")["state"], "completed")
            self.assertEqual(
                recovered.outbox_counts(),
                {"pending": 0, "in_flight": 0, "delivered": 1, "dead": 0},
            )
            recovered.close()

    def test_dead_letter_and_redrive_keep_closure_state_aligned(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DeliveryLedger(str(Path(td) / "closure-dead.sqlite3"))
            ledger.enqueue_closure_activity(
                "closure-dead",
                "issue-1",
                "session-1",
                "activity-dead",
                "Closure reconciliation complete.",
                {},
                now=100,
            )
            item = ledger.claim_due_outbox(now=100)
            ledger.dead_letter_outbox(item.id, "permanent", now=101)
            self.assertEqual(ledger.get_closure("closure-dead")["state"], "failed")
            self.assertTrue(ledger.requeue_dead_outbox(item.id, now=102))
            self.assertEqual(ledger.get_closure("closure-dead")["state"], "pending")
            ledger.close()

    def test_closure_suppresses_earlier_dead_activity(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DeliveryLedger(str(Path(td) / "closure-dead-predecessor.sqlite3"))
            ledger.enqueue_outbox(
                "dead-before-closure",
                "session-1",
                "activity.create",
                {"activity_type": "thought"},
                now=100,
            )
            dead = ledger.claim_due_outbox(now=100)
            self.assertIsNotNone(dead)
            ledger.dead_letter_outbox(dead.id, "permanent", now=101)

            inserted = ledger.enqueue_closure_activity(
                "closure-after-dead",
                "issue-1",
                "session-1",
                "activity-closure-after-dead",
                "Closure reconciliation complete.",
                {},
                now=102,
            )

            self.assertTrue(inserted)
            suppressed = ledger.get_outbox_item("dead-before-closure")
            self.assertEqual(suppressed["state"], "delivered")
            self.assertIn("authoritative human closure", suppressed["last_error"])
            closure = ledger.claim_due_outbox(now=102)
            self.assertIsNotNone(closure)
            self.assertEqual(closure.id, "activity:closure:closure-after-dead")
            ledger.close()

    def test_dead_letter_can_be_manually_redriven(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DeliveryLedger(str(Path(td) / "dead.sqlite3"))
            ledger.enqueue_outbox("dead-1", "session-1", "activity.create", {}, now=100)
            ledger.dead_letter_outbox("dead-1", "permanent", now=101)
            self.assertTrue(ledger.requeue_dead_outbox("dead-1", now=102))
            self.assertFalse(ledger.requeue_dead_outbox("dead-1", now=103))
            self.assertEqual(ledger.get_outbox_item("dead-1")["state"], "pending")
            ledger.close()


class AdapterCredentialTests(unittest.TestCase):
    def test_boolean_and_team_list_config_values_are_strictly_typed(self):
        base = {
            "oauth_file": "/tmp/linear-oauth.json",
            "database_path": "/tmp/linear.sqlite3",
            "closure_reconciliation_enabled": True,
            "data_change_events_enabled": True,
            "closure_allowed_team_ids": ["team-ops"],
            "issue_status_writeback_enabled": False,
            "dependency_wait_enabled": False,
        }
        unsafe_values = (
            {"closure_reconciliation_enabled": "false"},
            {"data_change_events_enabled": "true"},
            {"issue_status_writeback_enabled": "false"},
            {"dependency_wait_enabled": "false"},
            {"closure_allowed_team_ids": "team-ops"},
            {"closure_allowed_team_ids": ["team-ops", 73]},
        )
        with mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": "s" * 32}, clear=False):
            for unsafe in unsafe_values:
                with self.subTest(unsafe=unsafe):
                    config = PlatformConfig(enabled=True, extra={**base, **unsafe})
                    self.assertFalse(LinearPlatformAdapter.validate_config(config))

        constructed = LinearPlatformAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "database_path": "/tmp/unused.sqlite3",
                    "closure_reconciliation_enabled": "false",
                    "data_change_events_enabled": "true",
                    "dependency_wait_enabled": "true",
                    "issue_status_writeback_enabled": "true",
                    "closure_allowed_team_ids": "team-ops",
                },
            ),
            Platform.WEBHOOK,
        )
        self.assertFalse(constructed._closure_reconciliation_enabled)
        self.assertFalse(constructed._data_change_events_enabled)
        self.assertFalse(constructed._dependency_wait_enabled)
        self.assertFalse(constructed._status_writeback_enabled)
        self.assertEqual(constructed._closure_allowed_team_ids, set())

    def test_validate_config_accepts_process_environment_secret_without_file(self):
        config = PlatformConfig(
            enabled=True,
            extra={
                "oauth_file": "/tmp/linear-oauth.json",
                "database_path": "/tmp/linear.sqlite3",
            },
        )

        with mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": "s" * 32}, clear=False):
            self.assertTrue(LinearPlatformAdapter.validate_config(config))

    def test_closure_mode_requires_data_events_team_allowlist_and_no_state_writeback(self):
        base = {
            "oauth_file": "/tmp/linear-oauth.json",
            "database_path": "/tmp/linear.sqlite3",
            "closure_reconciliation_enabled": True,
            "data_change_events_enabled": True,
            "closure_allowed_team_ids": ["team-ops"],
            "issue_status_writeback_enabled": False,
        }
        with mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": "s" * 32}, clear=False):
            self.assertTrue(
                LinearPlatformAdapter.validate_config(PlatformConfig(enabled=True, extra=base))
            )
            for unsafe in (
                {"data_change_events_enabled": False},
                {"closure_allowed_team_ids": []},
                {"issue_status_writeback_enabled": True},
            ):
                self.assertFalse(
                    LinearPlatformAdapter.validate_config(
                        PlatformConfig(enabled=True, extra={**base, **unsafe})
                    )
                )

    def test_process_environment_secret_overrides_legacy_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "linear-bridge.env"
            path.write_text(
                "LINEAR_WEBHOOK_SECRET=file-current\n"
                "LINEAR_WEBHOOK_SECRET_PREVIOUS=file-previous\n",
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "LINEAR_WEBHOOK_SECRET": "environment-current",
                    "LINEAR_WEBHOOK_SECRET_PREVIOUS": "environment-previous",
                },
                clear=False,
            ):
                credentials = adapter_mod._read_webhook_credentials(str(path))

        self.assertEqual(credentials["LINEAR_WEBHOOK_SECRET"], "environment-current")
        self.assertEqual(
            credentials["LINEAR_WEBHOOK_SECRET_PREVIOUS"],
            "environment-previous",
        )


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
    def test_activity_uuid_is_deterministic_and_v4_shaped(self):
        first = self.adapter._activity_uuid("thought:delivery-8")
        second = self.adapter._activity_uuid("thought:delivery-8")
        self.assertEqual(first, second)
        self.assertEqual(uuid.UUID(first).version, 4)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp.name) / "ledger.sqlite3")
        config = PlatformConfig(enabled=True, extra={"database_path": db_path})
        self.adapter = LinearPlatformAdapter(config, Platform.WEBHOOK)
        self.adapter._signing_secrets = ("s" * 32, "p" * 32)
        self.adapter._linear = FakeLinear("org-1")
        self.adapter._ledger = DeliveryLedger(db_path)
        self.adapter._data_change_events_enabled = True
        self.adapter._dependency_wait_enabled = True
        self.adapter._closure_reconciliation_enabled = False
        self.adapter._closure_allowed_team_ids = set()
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


    def make_data_payload(self, event_type="Issue", **overrides):
        payload = {
            "type": event_type,
            "action": "update",
            "webhookId": "webhook-data-123",
            "webhookTimestamp": int(time.time() * 1000),
            "organizationId": "org-1",
            "actor": {"id": "user-1", "name": "Mutlu"},
            "data": {"id": "blocker-7", "updatedAt": "2026-07-16T10:00:00.000Z"},
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

    async def test_created_acknowledgment_uses_installed_app_actor_name(self):
        self.adapter._linear.actor_name = "Doruk"

        response = await self.adapter._handle_webhook(self.request_for(self.make_payload()))

        self.assertEqual(response.status, 200)
        await asyncio.sleep(0)
        self.assertEqual(
            self.adapter._linear.calls[0][2],
            "Doruk accepted the task; Hermes is processing it.",
        )

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


    async def test_blocked_delegation_waits_without_starting_hermes(self):
        self.adapter._linear.blockers["issue-8"] = [
            {"id": "blocker-7", "identifier": "OPS-7", "title": "Human approval", "state": "Todo"}
        ]
        payload = self.make_payload(
            webhookId="webhook-wait-123",
            agentSession={
                "id": "session-8",
                "issue": {"id": "issue-8", "identifier": "OPS-8", "title": "Resume me"},
            },
        )
        response = await self.adapter._handle_webhook(self.request_for(payload))
        self.assertEqual(json.loads(response.text)["status"], "awaiting_input")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.get_wait("session-8")["state"], "waiting")
        self.assertEqual(self.adapter._linear.calls, [])
        await self.adapter._drain_outbox_once()
        self.assertEqual(self.adapter._linear.calls[0][1], "elicitation")
        self.assertIn("OPS-7", self.adapter._linear.calls[0][2])

    async def test_blocked_delegation_does_not_drain_outbox_in_webhook(self):
        self.adapter._linear.blockers["issue-8"] = [
            {"id": "blocker-7", "identifier": "OPS-7", "title": "Human approval"}
        ]
        payload = self.make_payload(
            webhookId="webhook-wait-no-drain",
            agentSession={
                "id": "session-8",
                "issue": {"id": "issue-8", "identifier": "OPS-8", "title": "No drain"},
            },
        )

        with mock.patch.object(
            self.adapter,
            "_drain_outbox_once",
            side_effect=AssertionError("webhook drained outbox"),
        ):
            response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "awaiting_input")
        self.assertEqual(self.adapter._ledger.outbox_counts()["pending"], 1)

    async def test_blocker_update_resumes_same_session_exactly_once(self):
        self.adapter._linear.blockers["issue-8"] = [
            {"id": "blocker-7", "identifier": "OPS-7", "title": "Human approval", "state": "Todo"}
        ]
        created = self.make_payload(
            webhookId="webhook-wait-456",
            agentSession={
                "id": "session-8",
                "issue": {"id": "issue-8", "identifier": "OPS-8", "title": "Resume me"},
            },
        )
        await self.adapter._handle_webhook(self.request_for(created))
        self.adapter._linear.blockers["issue-8"] = []
        updated = self.make_data_payload(webhookId="webhook-issue-done-1")
        response = await self.adapter._handle_webhook(self.request_for(updated))
        self.assertEqual(json.loads(response.text), {"status": "observed", "resumed": 1})
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].source.chat_id, "session-8")
        self.assertTrue(self.events[0].message_id.startswith("linear-event-"))
        self.assertTrue(self.events[0].metadata["linear_dependency_resume"])
        self.assertIn("All blocking issues are complete", self.events[0].text)
        self.assertIn("frozen creation snapshot", self.events[0].text)
        self.assertLess(
            self.events[0].text.index("Adapter-verified current dependency state"),
            self.events[0].text.index("Linear promptContext"),
        )
        self.assertEqual(self.adapter._ledger.get_wait("session-8")["state"], "resumed")
        duplicate = await self.adapter._handle_webhook(self.request_for(updated))
        self.assertEqual(json.loads(duplicate.text)["status"], "duplicate")
        self.assertEqual(len(self.events), 1)

    async def test_human_started_to_completed_queues_one_closure_without_rerunning_work(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        created = self.make_payload(
            webhookId="webhook-closure-session",
            agentSession={
                "id": "session-closure",
                "issue": {"id": "issue-closure", "identifier": "OPS-73", "title": "Closure"},
            },
        )
        await self.adapter._handle_webhook(self.request_for(created))
        await asyncio.sleep(0)
        self.adapter._linear.calls.clear()
        self.events.clear()
        self.adapter._ledger.put_wait(
            "session-closure",
            "issue-closure",
            "wait-delivery-closure",
            {"text": "do not rerun", "issue_identifier": "OPS-73", "issue_title": "Closure"},
            [{"id": "blocker-1", "identifier": "OPS-1", "title": "Blocker"}],
        )
        self.adapter._linear.closure_contexts["issue-closure"] = {
            "id": "issue-closure",
            "identifier": "OPS-73",
            "title": "Closure",
            "completed_at": "2026-08-04T12:21:07.002Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
            "history": [{
                "actor_id": "user-1",
                "created_at": "2026-08-04T12:21:06.002Z",
                "from_state": {"id": "started-1", "name": "In Progress", "type": "started"},
                "to_state": {"id": "done-1", "name": "Done", "type": "completed"},
            }],
        }
        completed = self.make_data_payload(
            webhookId="webhook-human-completed-1",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": "issue-closure",
                "updatedAt": "2026-08-04T12:21:04.002Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(response.text)["status"], "closure_queued")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._linear.calls, [])
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)

        await self.adapter._drain_outbox_once()
        self.assertEqual(len(self.adapter._linear.calls), 1)
        session_id, activity_type, body = self.adapter._linear.calls[0]
        self.assertEqual((session_id, activity_type), ("session-closure", "response"))
        self.assertIn("Closure reconciliation complete", body)
        self.assertIn("Mutlu", body)
        self.assertIn("In Progress", body)
        self.assertIn("Done", body)
        self.assertIn("not rerun", body)
        self.assertEqual(self.adapter._ledger.get_wait("session-closure")["state"], "canceled")

        late = await self.adapter.send("session-closure", "late main deliverable")
        self.assertTrue(late.success)
        self.assertEqual(len(self.adapter._linear.calls), 1)

        self.adapter._ledger.bind_issue_session(
            "issue-closure", "session-newer", now=int(time.time()) + 1
        )
        closure_duplicate = await self.adapter._reconcile_human_completion(
            completed, "issue-closure"
        )
        self.assertEqual(closure_duplicate, "closure_duplicate")
        self.assertEqual(len(self.adapter._linear.calls), 1)

        tolerated_revision = dict(completed)
        tolerated_revision["data"] = {
            **completed["data"],
            "updatedAt": "2026-08-04T12:21:04.999Z",
        }
        closure_revision_duplicate = await self.adapter._reconcile_human_completion(
            tolerated_revision, "issue-closure"
        )
        self.assertEqual(closure_revision_duplicate, "closure_duplicate")
        self.assertEqual(self.adapter._ledger.closure_counts()["completed"], 1)
        self.assertEqual(len(self.adapter._linear.calls), 1)

        duplicate_delivery = dict(completed, webhookId="webhook-human-completed-2")
        duplicate = await self.adapter._handle_webhook(self.request_for(duplicate_delivery))
        self.assertEqual(json.loads(duplicate.text)["status"], "duplicate")
        self.assertEqual(len(self.adapter._linear.calls), 1)
        self.assertEqual(self.events, [])

    async def test_done_before_session_creation_is_durably_fenced_and_reconciled(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        self.adapter._linear.closure_contexts["issue-out-of-order"] = {
            "id": "issue-out-of-order",
            "identifier": "OPS-73",
            "title": "Out of order closure",
            "completed_at": "2026-08-04T12:30:00.000Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
            "history": [{
                "actor_id": "user-1",
                "created_at": "2026-08-04T12:30:00.000Z",
                "from_state": {"id": "started-1", "type": "started"},
                "to_state": {"id": "done-1", "type": "completed"},
            }],
        }
        completed = self.make_data_payload(
            webhookId="webhook-out-of-order-done",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": "issue-out-of-order",
                "updatedAt": "2026-08-04T12:30:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        deferred = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(deferred.text)["status"], "closure_deferred")
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 1)
        self.assertEqual(self.events, [])

        created = self.make_payload(
            webhookId="webhook-out-of-order-created",
            agentSession={
                "id": "session-out-of-order",
                "issue": {
                    "id": "issue-out-of-order",
                    "identifier": "OPS-73",
                    "title": "Out of order closure",
                },
            },
        )
        reconciled = await self.adapter._handle_webhook(self.request_for(created))

        self.assertEqual(json.loads(reconciled.text)["status"], "closure_queued")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 0)
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)
        await self.adapter._drain_outbox_once()
        self.assertEqual(
            [call[1] for call in self.adapter._linear.calls],
            ["response"],
        )

    async def test_concurrent_done_before_session_creation_fences_dispatch(self):
        class BlockingClosureReadLinear(FakeLinear):
            def __init__(self):
                super().__init__("org-1")
                self.read_started = asyncio.Event()
                self.release_read = asyncio.Event()

            async def get_issue_closure_context(self, issue_id):
                self.read_started.set()
                await self.release_read.wait()
                return await super().get_issue_closure_context(issue_id)

        linear = BlockingClosureReadLinear()
        self.adapter._linear = linear
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-concurrent-order"
        session_id = "session-concurrent-order"
        linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "identifier": "OPS-73",
            "title": "Concurrent closure",
            "completed_at": "2026-08-04T12:31:00.000Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
            "history": [{
                "actor_id": "user-1",
                "created_at": "2026-08-04T12:31:00.000Z",
                "from_state": {"id": "started-1", "type": "started"},
                "to_state": {"id": "done-1", "type": "completed"},
            }],
        }
        completed = self.make_data_payload(
            webhookId="webhook-concurrent-order-done",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": "2026-08-04T12:31:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )
        created = self.make_payload(
            webhookId="webhook-concurrent-order-created",
            agentSession={
                "id": session_id,
                "issue": {"id": issue_id, "identifier": "OPS-73", "title": "Concurrent closure"},
            },
        )

        done_task = asyncio.create_task(self.adapter._handle_webhook(self.request_for(completed)))
        await linear.read_started.wait()
        created_task = asyncio.create_task(self.adapter._handle_webhook(self.request_for(created)))
        await asyncio.sleep(0)
        self.assertEqual(self.events, [])
        self.assertFalse(created_task.done())
        linear.release_read.set()

        self.assertEqual(json.loads((await done_task).text)["status"], "closure_deferred")
        self.assertEqual(json.loads((await created_task).text)["status"], "closure_queued")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 0)

    async def test_obsolete_pending_closure_does_not_consume_created_event(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-obsolete-fence"
        session_id = "session-obsolete-fence"
        event = {
            "actor": {"id": "user-1"},
            "data": {
                "id": issue_id,
                "updatedAt": "2026-08-04T12:32:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            "updatedFrom": {"stateId": "started-1"},
        }
        self.adapter._ledger.stage_pending_closure_event(
            issue_id, 1.0, event
        )
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "state": {"id": "started-2", "name": "Reopened", "type": "started"},
        }
        created = self.make_payload(
            webhookId="webhook-obsolete-fence-created",
            agentSession={
                "id": session_id,
                "issue": {"id": issue_id, "identifier": "OPS-73", "title": "Reopened"},
            },
        )

        response = await self.adapter._handle_webhook(self.request_for(created))

        self.assertEqual(json.loads(response.text)["status"], "accepted")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 0)

    async def test_unverifiable_pending_closure_retries_created_and_degrades_health(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-unverifiable-fence"
        session_id = "session-unverifiable-fence"
        event = {
            "actor": {"id": "user-1"},
            "data": {
                "id": issue_id,
                "updatedAt": "2026-08-04T12:33:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            "updatedFrom": {"stateId": "started-1"},
        }
        self.adapter._ledger.stage_pending_closure_event(
            issue_id, 1.0, event
        )
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "completed_at": "2026-08-04T12:33:00.000Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
            "history": [],
        }
        created = self.make_payload(
            webhookId="webhook-unverifiable-fence-created",
            agentSession={
                "id": session_id,
                "issue": {"id": issue_id, "identifier": "OPS-73", "title": "Unverified"},
            },
        )

        response = await self.adapter._handle_webhook(self.request_for(created))
        self.adapter._running = True
        health = await self.adapter._health(None)
        health_body = json.loads(health.text)

        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.text)["status"], "closure_deferred")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 1)
        self.assertEqual(health_body["status"], "degraded")
        self.assertEqual(health_body["closures"]["pending_session_binding"], 1)

    async def test_closure_fails_closed_for_spoofed_actor_wrong_team_or_wrong_delegate(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        cases = (
            ("spoofed-actor", "attacker", "user-1", "team-ops", "agent-derya"),
            ("wrong-team", "user-1", "user-1", "team-other", "agent-derya"),
            ("wrong-delegate", "user-1", "user-1", "team-ops", "agent-other"),
            ("forged-history", "user-1", "agent-derya", "team-ops", "agent-derya"),
        )
        for index, (label, actor_id, history_actor_id, team_id, delegate_id) in enumerate(cases):
            with self.subTest(label=label):
                issue_id = f"issue-reject-{index}"
                session_id = f"session-reject-{index}"
                self.adapter._linear.closure_contexts[issue_id] = {
                    "id": issue_id,
                    "identifier": f"OPS-{80 + index}",
                    "title": label,
                    "completed_at": f"2026-08-04T12:22:0{index}.000Z",
                    "state": {"id": "done-1", "name": "Done", "type": "completed"},
                    "team": {"id": team_id},
                    "team_states": [
                        {"id": "started-1", "name": "In Progress", "type": "started"},
                        {"id": "done-1", "name": "Done", "type": "completed"},
                    ],
                    "assignee": {"id": "user-1", "name": "Mutlu"},
                    "delegate": {"id": delegate_id, "name": "Delegate"},
                    "history": [{
                        "actor_id": history_actor_id,
                        "created_at": f"2026-08-04T12:22:0{index}.000Z",
                        "from_state": {"id": "started-1", "type": "started"},
                        "to_state": {"id": "done-1", "type": "completed"},
                    }],
                }
                self.adapter._ledger.bind_issue_session(issue_id, session_id)
                payload = self.make_data_payload(
                    webhookId=f"webhook-reject-{index}",
                    actor={"id": actor_id, "name": "Actor"},
                    data={
                        "id": issue_id,
                        "updatedAt": f"2026-08-04T12:22:0{index}.000Z",
                        "state": {"id": "done-1", "type": "completed"},
                    },
                    updatedFrom={"stateId": "started-1"},
                )
                response = await self.adapter._handle_webhook(self.request_for(payload))
                self.assertEqual(json.loads(response.text)["status"], "closure_rejected")
        self.assertEqual(self.adapter._linear.calls, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.closure_counts()["completed"], 0)

    async def test_closure_rejects_timestamp_skew_outside_bounded_tolerance(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        self.adapter._ledger.bind_issue_session("issue-skew", "session-skew")
        self.adapter._linear.closure_contexts["issue-skew"] = {
            "id": "issue-skew",
            "completed_at": "2026-08-04T12:30:06.001Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
            "history": [{
                "actor_id": "user-1",
                "created_at": "2026-08-04T12:30:06.001Z",
                "from_state": {"id": "started-1", "type": "started"},
                "to_state": {"id": "done-1", "type": "completed"},
            }],
        }
        payload = self.make_data_payload(
            webhookId="webhook-skew-too-large",
            data={
                "id": "issue-skew",
                "updatedAt": "2026-08-04T12:30:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "closure_rejected")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 0)
        self.assertEqual(self.adapter._linear.calls, [])

    async def test_closure_waits_for_claimed_network_dispatch_then_suppresses_later_writes(self):
        class BlockingLinear(FakeLinear):
            def __init__(self):
                super().__init__("org-1")
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def create_activity(self, session_id, activity_type, body, *, activity_id):
                self.started.set()
                await self.release.wait()
                return await super().create_activity(
                    session_id, activity_type, body, activity_id=activity_id
                )

        linear = BlockingLinear()
        self.adapter._linear = linear
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        self.adapter._ledger.bind_issue_session("issue-race", "session-race")
        linear.closure_contexts["issue-race"] = {
            "id": "issue-race",
            "completed_at": "2026-08-04T12:40:00.000Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
            "history": [{
                "actor_id": "user-1",
                "created_at": "2026-08-04T12:40:00.000Z",
                "from_state": {"id": "started-1", "type": "started"},
                "to_state": {"id": "done-1", "type": "completed"},
            }],
        }
        self.adapter._enqueue_activity(
            "session-race", "thought", "already claimed", item_key="race-before-closure"
        )
        drain_task = asyncio.create_task(self.adapter._drain_outbox_once())
        await linear.started.wait()
        payload = self.make_data_payload(
            data={
                "id": "issue-race",
                "updatedAt": "2026-08-04T12:40:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )
        closure_task = asyncio.create_task(
            self.adapter._reconcile_human_completion(payload, "issue-race")
        )
        await asyncio.sleep(0)
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 0)

        linear.release.set()
        await drain_task
        self.assertEqual(linear.calls, [("session-race", "thought", "already claimed")])
        self.assertEqual(await closure_task, "closure_queued")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)

        await self.adapter._drain_outbox_once()
        self.assertEqual([call[1] for call in linear.calls], ["thought", "response"])

    async def test_dependency_resume_checks_closure_immediately_before_handle_message(self):
        payload = self.make_payload(
            webhookId="webhook-dependency-closure-race",
            agentSession={
                "id": "session-dependency-race",
                "issue": {"id": "issue-dependency-race", "identifier": "OPS-74", "title": "Race"},
            },
        )
        self.adapter._ledger.bind_issue_session(
            "issue-dependency-race", "session-dependency-race"
        )
        self.adapter._ledger.put_wait(
            "session-dependency-race",
            "issue-dependency-race",
            "dependency-race-delivery",
            payload,
            [{"id": "blocker", "identifier": "OPS-72", "title": "Blocker"}],
        )
        original_claim_wait = self.adapter._ledger.claim_wait

        def claim_then_close(session_id, *, now=None):
            claimed = original_claim_wait(session_id, now=now)
            if claimed:
                self.adapter._ledger.enqueue_closure_activity(
                    "dependency-race-closure",
                    "issue-dependency-race",
                    session_id,
                    "activity-dependency-race-closure",
                    "Closure reconciliation complete.",
                    {},
                )
            return claimed

        self.adapter._ledger.claim_wait = claim_then_close
        self.adapter._linear.blockers["issue-dependency-race"] = []

        resumed = await self.adapter._reconcile_wait("session-dependency-race")

        self.assertFalse(resumed)
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.adapter._ledger.get_wait("session-dependency-race")["state"], "canceled"
        )

    async def test_closure_readback_holds_session_lock_before_dependency_resume(self):
        class BlockingClosureReadLinear(FakeLinear):
            def __init__(self):
                super().__init__("org-1")
                self.read_started = asyncio.Event()
                self.release_read = asyncio.Event()

            async def get_issue_closure_context(self, issue_id):
                self.read_started.set()
                await self.release_read.wait()
                return await super().get_issue_closure_context(issue_id)

        linear = BlockingClosureReadLinear()
        self.adapter._linear = linear
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-readback-race"
        session_id = "session-readback-race"
        payload = self.make_payload(
            webhookId="webhook-readback-race-session",
            agentSession={
                "id": session_id,
                "issue": {"id": issue_id, "identifier": "OPS-73", "title": "Read race"},
            },
        )
        self.adapter._ledger.bind_issue_session(issue_id, session_id)
        self.adapter._ledger.put_wait(
            session_id,
            issue_id,
            "readback-race-delivery",
            payload,
            [{"id": "blocker", "identifier": "OPS-72", "title": "Blocker"}],
        )
        linear.blockers[issue_id] = []
        linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "completed_at": "2026-08-04T12:45:00.000Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
            "history": [{
                "actor_id": "user-1",
                "created_at": "2026-08-04T12:45:00.000Z",
                "from_state": {"id": "started-1", "type": "started"},
                "to_state": {"id": "done-1", "type": "completed"},
            }],
        }
        completed = self.make_data_payload(
            webhookId="webhook-readback-race-done",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": "2026-08-04T12:45:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        closure_task = asyncio.create_task(
            self.adapter._reconcile_human_completion(completed, issue_id)
        )
        await linear.read_started.wait()
        resume_task = asyncio.create_task(self.adapter._reconcile_wait(session_id))
        await asyncio.sleep(0)
        linear.release_read.set()

        self.assertEqual(await closure_task, "closure_queued")
        self.assertFalse(await resume_task)
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.get_wait(session_id)["state"], "canceled")

    async def test_agent_authored_terminal_event_is_ignored_before_closure_readback(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        payload = self.make_data_payload(
            webhookId="webhook-self-terminal",
            actor={"id": "agent-derya", "name": "Derya"},
            data={
                "id": "issue-self",
                "updatedAt": "2026-08-04T12:23:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )
        response = await self.adapter._handle_webhook(self.request_for(payload))
        self.assertEqual(json.loads(response.text)["status"], "ignored_self")
        self.assertEqual(self.adapter._linear.calls, [])
        self.assertEqual(self.adapter._ledger.closure_counts()["completed"], 0)

    async def test_selected_linear_data_types_are_context_only(self):
        event_types = (
            "Comment",
            "IssueLabel",
            "Project",
            "ProjectUpdate",
            "ProjectLabel",
            "Attachment",
            "IssueAttachment",
            "Reaction",
            "CommentReaction",
            "EmojiReaction",
        )
        for index, event_type in enumerate(event_types):
            payload = self.make_data_payload(
                event_type=event_type,
                webhookId=f"webhook-context-{index}",
                data={
                    "id": f"context-{index}",
                    "updatedAt": f"2026-07-16T10:{index:02d}:00.000Z",
                },
            )
            response = await self.adapter._handle_webhook(self.request_for(payload))
            self.assertEqual(json.loads(response.text)["status"], "observed", event_type)
        self.assertEqual(self.events, [])

    async def test_self_event_is_ignored_and_delegate_removal_cancels_wait(self):
        self_event = self.make_data_payload(
            event_type="Comment",
            webhookId="webhook-self-comment",
            actor={"id": "agent-derya", "name": "Derya"},
            data={"id": "comment-self", "updatedAt": "2026-07-16T10:03:00.000Z"},
        )
        ignored = await self.adapter._handle_webhook(self.request_for(self_event))
        self.assertEqual(json.loads(ignored.text)["status"], "ignored_self")

        self.adapter._ledger.put_wait(
            "session-cancel",
            "issue-cancel",
            "delivery-cancel",
            self.make_payload(agentSession={"id": "session-cancel", "issue": {"id": "issue-cancel"}}),
            [{"id": "blocker-7", "identifier": "OPS-7"}],
        )
        removal = self.make_data_payload(
            webhookId="webhook-delegate-remove",
            data={"id": "issue-cancel", "updatedAt": "2026-07-16T10:04:00.000Z", "delegate": None},
            updatedFrom={"delegateId": "agent-derya"},
        )
        await self.adapter._handle_webhook(self.request_for(removal))
        self.assertEqual(self.adapter._ledger.get_wait("session-cancel")["state"], "canceled")

    async def test_stop_cancels_wait_before_forwarding_command(self):
        self.adapter._ledger.put_wait(
            "session-stop-wait",
            "issue-stop",
            "delivery-stop",
            self.make_payload(agentSession={"id": "session-stop-wait", "issue": {"id": "issue-stop"}}),
            [{"id": "blocker-7", "identifier": "OPS-7"}],
        )
        stopped = self.make_payload(
            webhookId="webhook-stop-wait",
            action="prompted",
            agentActivity={"id": "activity-stop-wait", "body": "stop", "signal": "stop"},
            agentSession={
                "id": "session-stop-wait",
                "issue": {"id": "issue-stop", "identifier": "OPS-8", "title": "Stop me"},
            },
        )
        await self.adapter._handle_webhook(self.request_for(stopped))
        self.assertEqual(self.adapter._ledger.get_wait("session-stop-wait")["state"], "canceled")
        self.assertEqual(self.events[-1].text, "/stop")


    async def test_inbox_unassign_cancels_wait_and_oauth_revoke_degrades_health(self):
        self.adapter._linear.blockers["issue-8"] = [
            {"id": "blocker-7", "identifier": "OPS-7", "title": "Blocker", "state": {"type": "started"}}
        ]
        created = self.make_payload()
        created["agentSession"]["issue"]["id"] = "issue-8"
        response = await self.adapter._handle_webhook(self.request_for(created))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.adapter._ledger.get_wait("session-1")["state"], "waiting")

        notification = {
            "type": "AppUserNotification",
            "action": "issueUnassignedFromYou",
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "organizationId": "org-1",
            "oauthClientId": "oauth-1",
            "appUserId": "agent-derya",
            "notification": {"id": "notification-1", "issueId": "issue-8"},
        }
        response = await self.adapter._handle_webhook(self.request_for(notification))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.adapter._ledger.get_wait("session-1")["state"], "canceled")

        revoked = self.make_data_payload(
            "OAuthApp",
            action="revoked",
            data={"id": "oauth-1", "updatedAt": "2026-07-16T10:00:01.000Z"},
        )
        response = await self.adapter._handle_webhook(self.request_for(revoked))
        self.assertEqual(response.status, 200)
        self.adapter._running = True
        health = await self.adapter._health(None)
        body = json.loads(health.body)
        self.assertEqual(body["status"], "degraded")
        self.assertTrue(body["oauth_revoked"])

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
            [(
                "session-failure",
                "error",
                "Hermes encountered an error while processing the task. The issue state was preserved for retry or human triage.",
            )],
        )

    async def test_transient_delivery_survives_adapter_restart(self):
        class FlakyLinear(FakeLinear):
            async def create_activity(self, *args, **kwargs):
                raise LinearAPIError("temporary outage", retryable=True)

        self.adapter._linear = FlakyLinear("org-1")
        self.adapter._outbox_base_delay = 0
        result = await self.adapter.send("session-restart", "persist me")
        self.assertTrue(result.success)
        item_id = next(
            row[0]
            for row in self.adapter._ledger._db.execute(
                "SELECT id FROM outbox WHERE aggregate_key = ?", ("session-restart",)
            ).fetchall()
        )
        self.assertEqual(self.adapter._ledger.get_outbox_item(item_id)["state"], "pending")

        self.adapter._ledger.close()
        self.adapter._ledger = DeliveryLedger(self.adapter.database_path)
        self.adapter._linear = FakeLinear("org-1")
        await self.adapter._drain_outbox_once()
        self.assertEqual(
            self.adapter._linear.calls,
            [("session-restart", "response", "persist me")],
        )
        self.assertEqual(self.adapter._ledger.get_outbox_item(item_id)["state"], "delivered")

    async def test_success_preserves_issue_state_for_mutlu_final_acceptance(self):
        self.adapter._status_writeback_enabled = True
        self.adapter._enqueue_activity("session-status", "response", "finished")
        event = MessageEvent(
            text="task",
            source=self.adapter.build_source(
                chat_id="session-status",
                chat_name="OPS-20 — Test",
                chat_type="dm",
                user_id="user-1",
                user_name="Mutlu",
                role_authorized=True,
            ),
            metadata={"linear_issue_id": "issue-20", "linear_delivery_key": "delivery-20"},
        )
        await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await self.adapter._drain_outbox_once()
        self.assertEqual(
            self.adapter._linear.calls,
            [("session-status", "response", "finished")],
        )


class LinearClientBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_closure_context_does_not_query_internal_agent_sessions(self):
        client = LinearClient("/unused")

        async def fake_graphql(query, variables=None):
            self.assertNotIn("agentSessions", query)
            self.assertEqual(variables, {"id": "issue-1"})
            return {
                "issue": {
                    "id": "issue-1",
                    "team": {"id": "team-ops", "states": {"nodes": []}},
                    "history": {"nodes": []},
                }
            }

        client.graphql = fake_graphql
        context = await client.get_issue_closure_context("issue-1")
        self.assertNotIn("agent_sessions", context)

    async def test_open_blockers_filters_terminal_relations(self):
        client = LinearClient("/unused")

        async def fake_graphql(query, variables=None):
            self.assertIn("inverseRelations", query)
            return {
                "issue": {
                    "inverseRelations": {
                        "nodes": [
                            {
                                "type": "blocks",
                                "issue": {
                                    "id": "open-1",
                                    "identifier": "OPS-7",
                                    "title": "Open",
                                    "state": {"name": "Todo", "type": "unstarted"},
                                },
                            },
                            {
                                "type": "blocks",
                                "issue": {
                                    "id": "done-1",
                                    "identifier": "OPS-6",
                                    "title": "Done",
                                    "state": {"name": "Done", "type": "completed"},
                                },
                            },
                            {
                                "type": "related",
                                "issue": {
                                    "id": "related-1",
                                    "identifier": "OPS-5",
                                    "state": {"name": "Todo", "type": "unstarted"},
                                },
                            },
                        ]
                    }
                }
            }

        client.graphql = fake_graphql
        blockers = await client.get_open_blockers("issue-8")
        self.assertEqual([item["id"] for item in blockers], ["open-1"])

    async def test_human_custom_state_wins_over_status_writeback(self):
        client = LinearClient("/unused")
        calls = []

        async def fake_graphql(query, variables=None):
            calls.append(query)
            return {
                "issue": {
                    "state": {"id": "review-1", "name": "Review", "type": "started"},
                    "team": {"states": {"nodes": []}},
                }
            }

        client.graphql = fake_graphql
        state_id = await client.update_issue_state(
            "issue-1", "Done", 40, {"Todo": 10, "Blocked": 15, "In Progress": 20, "Done": 40}
        )
        self.assertEqual(state_id, "review-1")
        self.assertEqual(len(calls), 1)

    async def test_configured_bridge_state_must_be_authoritatively_non_terminal(self):
        client = LinearClient("/unused")
        calls = []

        async def fake_graphql(query, variables=None):
            calls.append(query)
            if "LinearNativeIssueState" in query:
                return {
                    "issue": {
                        "state": {"id": "todo-1", "name": "Todo", "type": "unstarted"},
                        "team": {
                            "states": {
                                "nodes": [
                                    {
                                        "id": "unsafe-1",
                                        "name": "In Progress",
                                        "type": "completed",
                                    }
                                ]
                            }
                        },
                    }
                }
            raise AssertionError("terminal state mutation dispatched")

        client.graphql = fake_graphql
        with self.assertRaisesRegex(LinearAPIError, "terminal workflow state"):
            await client.update_issue_state(
                "issue-1",
                "In Progress",
                20,
                {"Todo": 10, "Blocked": 15, "In Progress": 20},
            )
        self.assertEqual(len(calls), 1)


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
                activity_id = await client.create_activity(
                    "session-1",
                    "response",
                    "full response",
                    activity_id="activity-client-id",
                )
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
                self.assertEqual(calls["activities"][0]["id"], "activity-client-id")
        finally:
            client_mod.LINEAR_TOKEN_URL = old_token_url
            client_mod.LINEAR_GRAPHQL_URL = old_graphql_url
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
