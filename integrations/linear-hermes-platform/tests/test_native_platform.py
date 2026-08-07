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
from typing import Any, cast
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
linear_tools_mod = __import__(f"{PACKAGE_NAME}.linear_tools", fromlist=["*"])

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
        self.activity_ephemeral: list[bool] = []
        self.blockers: dict[str, list[dict[str, str]]] = {}
        self.closure_contexts: dict[str, dict] = {}
        self.delegate_assignments: list[tuple[str, str]] = []
        self.issue_agent_sessions: dict[str, list[dict[str, str]]] = {}
        self.delivery_contexts: dict[str, dict] = {}

    async def assign_issue_delegate(self, issue_id, delegate_id):
        self.delegate_assignments.append((issue_id, delegate_id))
        return issue_id

    async def create_activity(
        self,
        session_id: str,
        activity_type: str,
        body: str,
        *,
        activity_id: str,
        ephemeral: bool = False,
    ) -> str:
        self.calls.append((session_id, activity_type, body))
        self.activity_ephemeral.append(ephemeral)
        return activity_id

    async def update_issue_state(self, issue_id, state_name, state_rank, state_ranks):
        self.calls.append((issue_id, "state", state_name))
        return f"state-{state_rank}"

    async def get_open_blockers(self, issue_id):
        return list(self.blockers.get(issue_id, []))

    async def get_issue_closure_context(self, issue_id):
        return dict(self.closure_contexts.get(issue_id, {}))

    async def get_issue_agent_sessions(self, issue_id):
        return [dict(item) for item in self.issue_agent_sessions.get(issue_id, [])]

    async def get_agent_session_delivery_context(self, session_id):
        return dict(self.delivery_contexts.get(session_id, {
            "id": session_id,
            "status": "active",
            "app_user_id": self.actor_id,
            "issue_id": f"issue-for-{session_id}",
            "state": {"id": "started-1", "name": "In Progress", "type": "started"},
        }))


class PluginRegistrationTests(unittest.TestCase):
    def test_cron_delivery_registration_bridges_yaml_home_channel(self):
        class FakeContext:
            def __init__(self):
                self.platform_kwargs = None

            def register_platform(self, **kwargs):
                self.platform_kwargs = kwargs

        context = FakeContext()
        with mock.patch.object(linear_tools_mod, "register_outbound_tools"):
            package.register(context)

        kwargs = context.platform_kwargs
        assert kwargs is not None
        self.assertEqual(
            kwargs.get("cron_deliver_env_var"),
            "LINEAR_HOME_CHANNEL",
        )
        bridge = kwargs.get("apply_yaml_config_fn")
        assert callable(bridge)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINEAR_HOME_CHANNEL", None)
            bridge({}, {"home_channel": {"chat_id": "session-home-1"}})
            self.assertEqual(os.environ.get("LINEAR_HOME_CHANNEL"), "session-home-1")
            bridge({}, {"home_channel": {"chat_id": "session-home-2"}})
            self.assertEqual(os.environ.get("LINEAR_HOME_CHANNEL"), "session-home-2")
            bridge({}, {})
            self.assertNotIn("LINEAR_HOME_CHANNEL", os.environ)

        with mock.patch.dict(
            os.environ,
            {"LINEAR_HOME_CHANNEL": "explicit-session"},
            clear=False,
        ):
            bridge({}, {"home_channel": {"chat_id": "yaml-session"}})
            self.assertEqual(os.environ.get("LINEAR_HOME_CHANNEL"), "explicit-session")

    def test_standalone_sender_uses_outbound_only_adapter(self):
        class FakeContext:
            def __init__(self):
                self.platform_kwargs = None

            def register_platform(self, **kwargs):
                self.platform_kwargs = kwargs

        context = FakeContext()
        with mock.patch.object(linear_tools_mod, "register_outbound_tools"):
            package.register(context)
        kwargs = context.platform_kwargs
        assert kwargs is not None
        sender = kwargs.get("standalone_sender_fn")
        assert callable(sender)
        sender_fn = cast(Any, sender)

        fake_adapter = mock.Mock()
        fake_adapter.connect_outbound_only = mock.AsyncMock(return_value=True)
        fake_adapter.send = mock.AsyncMock(
            return_value=mock.Mock(success=True, message_id="activity-1", error=None)
        )
        fake_adapter.disconnect = mock.AsyncMock()
        with mock.patch.object(
            adapter_mod.LinearPlatformAdapter,
            "from_config",
            return_value=fake_adapter,
        ):
            result = asyncio.run(sender_fn(object(), "session-1", "cron body"))

        self.assertEqual(
            result,
            {
                "success": True,
                "platform": "linear",
                "chat_id": "session-1",
                "message_id": "activity-1",
                "note": "Sent to linear target (chat_id: session-1)",
            },
        )
        fake_adapter.connect_outbound_only.assert_awaited_once_with()
        fake_adapter.send.assert_awaited_once_with("session-1", "cron body")
        fake_adapter.disconnect.assert_awaited_once_with()

    def test_standalone_sender_cleanup_failure_does_not_mask_success(self):
        class FakeContext:
            def __init__(self):
                self.platform_kwargs = None

            def register_platform(self, **kwargs):
                self.platform_kwargs = kwargs

        context = FakeContext()
        with mock.patch.object(linear_tools_mod, "register_outbound_tools"):
            package.register(context)
        kwargs = context.platform_kwargs
        assert kwargs is not None
        sender = cast(Any, kwargs["standalone_sender_fn"])

        fake_adapter = mock.Mock()
        fake_adapter.connect_outbound_only = mock.AsyncMock(return_value=True)
        fake_adapter.send = mock.AsyncMock(
            return_value=mock.Mock(success=True, message_id="activity-2", error=None)
        )
        fake_adapter.disconnect = mock.AsyncMock(side_effect=RuntimeError("cleanup failed"))
        with mock.patch.object(
            adapter_mod.LinearPlatformAdapter,
            "from_config",
            return_value=fake_adapter,
        ):
            result = asyncio.run(sender(object(), "session-2", "body"))

        self.assertEqual(result.get("success"), True)
        self.assertEqual(result.get("message_id"), "activity-2")

    def test_standalone_sender_preserves_retryable_error_result(self):
        class FakeContext:
            def __init__(self):
                self.platform_kwargs = None

            def register_platform(self, **kwargs):
                self.platform_kwargs = kwargs

        context = FakeContext()
        with mock.patch.object(linear_tools_mod, "register_outbound_tools"):
            package.register(context)
        sender = cast(Any, context.platform_kwargs["standalone_sender_fn"])
        fake_adapter = mock.Mock()
        fake_adapter.connect_outbound_only = mock.AsyncMock(return_value=True)
        fake_adapter.send = mock.AsyncMock(
            return_value=adapter_mod.SendResult(
                success=False,
                error="Linear GraphQL request timed out",
                retryable=True,
            )
        )
        fake_adapter.disconnect = mock.AsyncMock()

        with mock.patch.object(
            adapter_mod.LinearPlatformAdapter,
            "from_config",
            return_value=fake_adapter,
        ):
            result = asyncio.run(sender(object(), "session-3", "body"))

        self.assertEqual(
            result,
            {"error": "Linear GraphQL request timed out", "retryable": True},
        )

    def test_standalone_sender_preserves_retryable_connect_failure(self):
        class FakeContext:
            def __init__(self):
                self.platform_kwargs = None

            def register_platform(self, **kwargs):
                self.platform_kwargs = kwargs

        context = FakeContext()
        with mock.patch.object(linear_tools_mod, "register_outbound_tools"):
            package.register(context)
        sender = cast(Any, context.platform_kwargs["standalone_sender_fn"])
        fake_adapter = mock.Mock()
        fake_adapter.connect_outbound_only = mock.AsyncMock(return_value=False)
        fake_adapter.last_connect_error = LinearAPIError(
            "transport timeout with private details", retryable=True
        )
        fake_adapter.disconnect = mock.AsyncMock()

        with mock.patch.object(
            adapter_mod.LinearPlatformAdapter,
            "from_config",
            return_value=fake_adapter,
        ):
            result = asyncio.run(sender(object(), "session-4", "body"))

        self.assertEqual(
            result,
            {
                "error": "Linear outbound-only adapter failed to connect",
                "retryable": True,
            },
        )
        self.assertNotIn("private details", result["error"])

    def test_standalone_sender_marks_auth_connect_failure_permanent(self):
        class FakeContext:
            def __init__(self):
                self.platform_kwargs = None

            def register_platform(self, **kwargs):
                self.platform_kwargs = kwargs

        context = FakeContext()
        with mock.patch.object(linear_tools_mod, "register_outbound_tools"):
            package.register(context)
        sender = cast(Any, context.platform_kwargs["standalone_sender_fn"])
        fake_adapter = mock.Mock()
        fake_adapter.connect_outbound_only = mock.AsyncMock(return_value=False)
        fake_adapter.last_connect_error = LinearAPIError(
            "invalid bearer credential", retryable=False
        )
        fake_adapter.disconnect = mock.AsyncMock()

        with mock.patch.object(
            adapter_mod.LinearPlatformAdapter,
            "from_config",
            return_value=fake_adapter,
        ):
            result = asyncio.run(sender(object(), "session-5", "body"))

        self.assertEqual(
            result,
            {
                "error": "Linear outbound-only adapter failed to connect",
                "retryable": False,
            },
        )


class LedgerTests(unittest.TestCase):
    def test_populated_v4_database_migrates_to_v5_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v4.sqlite3"
            existing_at = int(time.time())
            populated = DeliveryLedger(str(path), startup_recovery=False)
            populated.claim("existing-delivery", now=existing_at)
            populated.mark_done("existing-delivery", now=existing_at)
            populated.bind_issue_session(
                "existing-issue", "existing-session", now=existing_at
            )
            populated.put_wait(
                "waiting-session",
                "waiting-issue",
                "waiting-delivery",
                {"type": "AgentSessionEvent", "action": "created"},
                [{"id": "blocker-1"}],
                now=existing_at,
            )
            populated.stage_pending_closure_event(
                "terminal-issue",
                float(existing_at),
                {"data": {"id": "terminal-issue"}},
                now=existing_at,
            )
            populated.enqueue_outbox(
                "existing-outbox",
                "existing-session",
                "activity.create",
                {"body": "preserved"},
                now=existing_at,
            )
            populated.close()

            db = sqlite3.connect(path)
            db.execute("DROP TABLE activation_waits")
            db.execute("DROP TABLE manager_activations")
            db.execute("PRAGMA user_version=4")
            db.commit()
            db.close()

            ledger = DeliveryLedger(str(path))

            self.assertEqual(
                ledger._db.execute("PRAGMA user_version").fetchone()[0], 5
            )
            self.assertEqual(
                ledger._db.execute(
                    "SELECT state, updated_at FROM deliveries WHERE webhook_id=?",
                    ("existing-delivery",),
                ).fetchone(),
                ("done", existing_at),
            )
            self.assertEqual(
                ledger.get_issue_session("existing-issue"), "existing-session"
            )
            self.assertEqual(ledger.get_wait("waiting-session")["state"], "waiting")
            self.assertEqual(
                ledger.get_pending_closure_event("terminal-issue")["event"]["data"]["id"],
                "terminal-issue",
            )
            self.assertEqual(
                ledger.get_outbox_item("existing-outbox")["payload"],
                {"body": "preserved"},
            )
            self.assertEqual(ledger.activation_counts()["waiting"], 0)
            self.assertEqual(ledger.manager_activation_counts()["delegated"], 0)
            ledger.close()

    def test_activation_dispatch_ambiguity_is_restart_durable_and_not_replayable(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "activation.sqlite3")
            payload = {"type": "AgentSessionEvent", "action": "created"}
            ledger = DeliveryLedger(path)
            ledger.put_activation_wait(
                "session-activation",
                "issue-activation",
                "delivery-activation",
                payload,
                now=100,
            )
            self.assertTrue(
                ledger.claim_activation(
                    "issue-activation", "activation-key-1", now=101
                )
            )
            ledger.close()

            recovered = DeliveryLedger(path)
            wait = recovered.get_activation_wait("issue-activation")
            self.assertEqual(wait["state"], "dispatch_unknown")
            self.assertFalse(
                recovered.claim_activation(
                    "issue-activation", "activation-key-1", now=102
                )
            )
            self.assertEqual(recovered.activation_counts()["dispatch_unknown"], 1)
            self.assertEqual(recovered._db.execute("PRAGMA user_version").fetchone()[0], 5)
            recovered.close()

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

    def test_wait_persists_and_resume_claim_suppresses_live_duplicates(self):
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
            self.assertEqual(recovered._db.execute("PRAGMA user_version").fetchone()[0], 5)
            recovered.close()

    def test_closure_outbox_orders_ephemeral_indicator_before_final_response(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DeliveryLedger(str(Path(td) / "closure-indicator.sqlite3"))
            inserted = ledger.enqueue_closure_activity(
                "closure-indicator",
                "issue-1",
                "session-1",
                "activity-final",
                "Closure reconciliation complete.",
                {"actor_id": "human-1"},
                indicator_activity_id="activity-indicator",
                indicator_body="Done received — closure is being verified.",
                now=100,
            )
            self.assertTrue(inserted)

            indicator = ledger.claim_due_outbox(now=100)
            self.assertEqual(indicator.id, "activity:closure:indicator:closure-indicator")
            self.assertEqual(indicator.payload["activity_type"], "thought")
            self.assertTrue(indicator.payload["ephemeral"])
            ledger.mark_outbox_delivered(indicator.id, now=101)
            self.assertEqual(ledger.get_closure("closure-indicator")["state"], "pending")

            final = ledger.claim_due_outbox(now=101)
            self.assertEqual(final.id, "activity:closure:closure-indicator")
            self.assertEqual(final.payload["activity_type"], "response")
            self.assertNotIn("ephemeral", final.payload)
            ledger.mark_outbox_delivered(final.id, now=102)
            self.assertEqual(ledger.get_closure("closure-indicator")["state"], "completed")
            self.assertIsNone(ledger.claim_due_outbox(now=103))
            ledger.close()

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

    def test_final_closure_dead_letter_and_cleanup_are_atomic_and_restart_durable(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "closure-cleanup.sqlite3")
            ledger = DeliveryLedger(path)
            ledger.enqueue_closure_activity(
                "closure-cleanup",
                "issue-1",
                "session-1",
                "activity-final",
                "Closure reconciliation complete.",
                {},
                indicator_activity_id="activity-indicator",
                indicator_body="Done received — closure is being verified.",
                now=100,
            )
            indicator = ledger.claim_due_outbox(now=100)
            ledger.mark_outbox_delivered(indicator.id, now=101)
            final = ledger.claim_due_outbox(now=101)

            self.assertTrue(
                ledger.dead_letter_outbox(
                    final.id,
                    "permanent",
                    closure_cleanup_activity_id="activity-cleanup",
                    closure_cleanup_body="The closure response could not be published.",
                    now=102,
                )
            )
            self.assertFalse(
                ledger.dead_letter_outbox(
                    final.id,
                    "permanent",
                    closure_cleanup_activity_id="activity-cleanup",
                    closure_cleanup_body="The closure response could not be published.",
                    now=103,
                )
            )
            ledger.close()

            recovered = DeliveryLedger(path)
            self.assertEqual(recovered._db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                recovered.get_outbox_item(final.id)["state"],
                "dead",
            )
            self.assertEqual(recovered.get_closure("closure-cleanup")["state"], "failed")
            cleanup = recovered.claim_due_outbox(now=104)
            self.assertEqual(cleanup.id, "activity:closure-error:closure-cleanup")
            self.assertEqual(cleanup.aggregate_key, "closure-cleanup:closure-cleanup")
            self.assertEqual(cleanup.payload["activity_type"], "error")
            recovered.mark_outbox_delivered(cleanup.id, now=105)
            self.assertEqual(recovered.get_outbox_item(final.id)["state"], "dead")
            self.assertTrue(recovered.requeue_dead_outbox(final.id, now=106))
            redriven_final = recovered.claim_due_outbox(now=106)
            self.assertEqual(redriven_final.id, final.id)
            recovered.mark_outbox_delivered(redriven_final.id, now=107)
            self.assertEqual(recovered.get_closure("closure-cleanup")["state"], "completed")
            recovered.close()

    def test_conflicting_cleanup_rolls_back_final_dead_letter_transition(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DeliveryLedger(str(Path(td) / "closure-cleanup-conflict.sqlite3"))
            ledger.enqueue_closure_activity(
                "closure-conflict",
                "issue-1",
                "session-1",
                "activity-final",
                "Closure reconciliation complete.",
                {},
                now=100,
            )
            final = ledger.claim_due_outbox(now=100)
            ledger.enqueue_outbox(
                "activity:closure-error:closure-conflict",
                "wrong-aggregate",
                "activity.create",
                {"body": "wrong payload"},
                now=100,
            )

            with self.assertRaises(sqlite3.IntegrityError):
                ledger.dead_letter_outbox(
                    final.id,
                    "permanent",
                    closure_cleanup_activity_id="activity-cleanup",
                    closure_cleanup_body="The closure response could not be published.",
                    now=101,
                )

            self.assertEqual(ledger.get_outbox_item(final.id)["state"], "in_flight")
            self.assertEqual(ledger.get_closure("closure-conflict")["state"], "pending")
            ledger.close()

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
            "planned_activation_enabled": False,
            "activation_allowed_team_ids": [],
            "planned_owner_ids": [],
        }
        unsafe_values = (
            {"closure_reconciliation_enabled": "false"},
            {"data_change_events_enabled": "true"},
            {"issue_status_writeback_enabled": "false"},
            {"dependency_wait_enabled": "false"},
            {"planned_activation_enabled": "false"},
            {"closure_allowed_team_ids": "team-ops"},
            {"closure_allowed_team_ids": ["team-ops", 73]},
            {"activation_allowed_team_ids": "team-ops"},
            {"activation_allowed_team_ids": ["team-ops", 73]},
            {"planned_owner_ids": "user-1"},
            {"planned_owner_ids": ["user-1", 73]},
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
                    "planned_activation_enabled": "true",
                    "issue_status_writeback_enabled": "true",
                    "closure_allowed_team_ids": "team-ops",
                    "activation_allowed_team_ids": "team-ops",
                    "planned_owner_ids": "user-1",
                },
            ),
            Platform.WEBHOOK,
        )
        self.assertFalse(constructed._closure_reconciliation_enabled)
        self.assertFalse(constructed._data_change_events_enabled)
        self.assertFalse(constructed._dependency_wait_enabled)
        self.assertFalse(constructed._planned_activation_enabled)
        self.assertFalse(constructed._status_writeback_enabled)
        self.assertEqual(constructed._closure_allowed_team_ids, set())
        self.assertEqual(constructed._activation_allowed_team_ids, set())
        self.assertEqual(constructed._planned_owner_ids, set())

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

    def test_planned_activation_requires_data_events_and_team_allowlist(self):
        base = {
            "oauth_file": "/tmp/linear-oauth.json",
            "database_path": "/tmp/linear.sqlite3",
            "planned_activation_enabled": True,
            "data_change_events_enabled": True,
            "activation_allowed_team_ids": ["team-ops"],
            "planned_owner_ids": ["user-1"],
        }
        with mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": "s" * 32}, clear=False):
            self.assertTrue(
                LinearPlatformAdapter.validate_config(PlatformConfig(enabled=True, extra=base))
            )
            for unsafe in (
                {"data_change_events_enabled": False},
                {"activation_allowed_team_ids": []},
                {"planned_owner_ids": []},
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


class AdapterOutboundOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_outbound_only_opens_oauth_and_ledger_without_webhook_server(self):
        class FakeConnectableLinear:
            instances = []

            def __init__(self, oauth_file):
                self.oauth_file = oauth_file
                self.connected = False
                self.closed = False
                self.__class__.instances.append(self)

            async def connect(self):
                self.connected = True

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "outbox.sqlite3")
            live_ledger = DeliveryLedger(db_path)
            live_ledger.put_wait(
                "session-active",
                "issue-active",
                "delivery-active",
                {"body": "resume"},
                [],
                now=100,
            )
            self.assertTrue(live_ledger.claim_wait("session-active", now=101))
            config = PlatformConfig(
                enabled=True,
                extra={
                    "database_path": db_path,
                    "oauth_file": str(Path(td) / "oauth.json"),
                },
            )
            adapter = LinearPlatformAdapter(config, Platform.WEBHOOK)
            with mock.patch.object(adapter_mod, "LinearClient", FakeConnectableLinear):
                self.assertTrue(await adapter.connect_outbound_only())
                self.assertIsNotNone(adapter._ledger)
                self.assertEqual(
                    live_ledger.get_wait("session-active")["state"],
                    "resuming",
                )
                self.assertTrue(FakeConnectableLinear.instances[-1].connected)
                self.assertIsNone(adapter._runner)
                self.assertIsNone(adapter._site)
                self.assertFalse(adapter._running)
                await adapter.disconnect()

            self.assertTrue(FakeConnectableLinear.instances[-1].closed)
            self.assertIsNone(adapter._ledger)
            self.assertEqual(live_ledger.get_wait("session-active")["state"], "resuming")
            live_ledger.close()

    async def test_connect_outbound_only_exposes_linear_error_retryability(self):
        for retryable in (True, False):
            with self.subTest(retryable=retryable), tempfile.TemporaryDirectory() as td:
                class FailingLinear:
                    def __init__(self, _oauth_file):
                        pass

                    async def connect(self):
                        raise LinearAPIError("safe connection failure", retryable=retryable)

                    async def close(self):
                        pass

                config = PlatformConfig(
                    enabled=True,
                    extra={
                        "database_path": str(Path(td) / "outbox.sqlite3"),
                        "oauth_file": str(Path(td) / "oauth.json"),
                    },
                )
                adapter = LinearPlatformAdapter(config, Platform.WEBHOOK)
                with mock.patch.object(adapter_mod, "LinearClient", FailingLinear):
                    self.assertFalse(await adapter.connect_outbound_only())

                self.assertIsInstance(adapter.last_connect_error, LinearAPIError)
                self.assertEqual(adapter.last_connect_error.retryable, retryable)
                self.assertIsNone(adapter._linear)
                self.assertIsNone(adapter._ledger)


class AdapterWebhookTests(unittest.IsolatedAsyncioTestCase):
    def test_activity_uuid_is_deterministic_and_v4_shaped(self):
        first = self.adapter._activity_uuid("thought:delivery-8")
        second = self.adapter._activity_uuid("thought:delivery-8")
        self.assertEqual(first, second)
        self.assertEqual(uuid.UUID(first).version, 4)

    async def test_permanent_closure_response_failure_enqueues_indicator_cleanup_error(self):
        class FinalResponseFailureLinear(FakeLinear):
            async def create_activity(
                self,
                session_id,
                activity_type,
                body,
                *,
                activity_id,
                ephemeral=False,
            ):
                if activity_type == "response":
                    raise LinearAPIError("permanent closure response failure")
                return await super().create_activity(
                    session_id,
                    activity_type,
                    body,
                    activity_id=activity_id,
                    ephemeral=ephemeral,
                )

        self.adapter._linear = FinalResponseFailureLinear()
        self.adapter._ledger.enqueue_closure_activity(
            "closure-cleanup",
            "issue-cleanup",
            "session-cleanup",
            "activity-final",
            "Closure reconciliation complete.",
            {"actor_id": "human-1"},
            indicator_activity_id="activity-indicator",
            indicator_body="Done received — closure is being verified.",
        )

        await self.adapter._drain_outbox_once()
        await self.adapter._drain_outbox_once()

        self.assertEqual(self.adapter._ledger.get_closure("closure-cleanup")["state"], "failed")
        self.assertEqual(
            self.adapter._ledger.get_outbox_item("activity:closure:closure-cleanup")["state"],
            "dead",
        )
        self.assertEqual(self.adapter._linear.calls[0][1], "thought")

        await self.adapter._drain_outbox_once()

        self.assertEqual([call[1] for call in self.adapter._linear.calls], ["thought", "error"])
        self.assertIn("closure response could not be published", self.adapter._linear.calls[1][2])

    async def test_generic_closure_response_failure_enqueues_cleanup_error(self):
        class GenericFinalResponseFailureLinear(FakeLinear):
            async def create_activity(
                self,
                session_id,
                activity_type,
                body,
                *,
                activity_id,
                ephemeral=False,
            ):
                if activity_type == "response":
                    raise RuntimeError("unexpected closure response failure")
                return await super().create_activity(
                    session_id,
                    activity_type,
                    body,
                    activity_id=activity_id,
                    ephemeral=ephemeral,
                )

        self.adapter._linear = GenericFinalResponseFailureLinear()
        self.adapter._ledger.enqueue_closure_activity(
            "generic-cleanup",
            "issue-cleanup",
            "session-cleanup",
            "activity-final",
            "Closure reconciliation complete.",
            {},
            indicator_activity_id="activity-indicator",
            indicator_body="Done received — closure is being verified.",
        )

        await self.adapter._drain_outbox_once()
        await self.adapter._drain_outbox_once()
        await self.adapter._drain_outbox_once()

        self.assertEqual([call[1] for call in self.adapter._linear.calls], ["thought", "error"])
        self.assertEqual(
            self.adapter._ledger.get_outbox_item("activity:closure:generic-cleanup")["state"],
            "dead",
        )
        self.assertEqual(self.adapter._ledger.get_closure("generic-cleanup")["state"], "failed")

    async def test_cleanup_failure_does_not_enqueue_recursive_cleanup(self):
        class AllClosureActivitiesFailLinear(FakeLinear):
            async def create_activity(self, *args, **kwargs):
                activity_type = args[1]
                if activity_type in {"response", "error"}:
                    raise LinearAPIError(f"permanent {activity_type} failure")
                return await super().create_activity(*args, **kwargs)

        self.adapter._linear = AllClosureActivitiesFailLinear()
        self.adapter._ledger.enqueue_closure_activity(
            "nonrecursive-cleanup",
            "issue-cleanup",
            "session-cleanup",
            "activity-final",
            "Closure reconciliation complete.",
            {},
            indicator_activity_id="activity-indicator",
            indicator_body="Done received — closure is being verified.",
        )

        await self.adapter._drain_outbox_once()
        await self.adapter._drain_outbox_once()
        await self.adapter._drain_outbox_once()

        self.assertFalse(await self.adapter._drain_outbox_once())
        self.assertEqual(self.adapter._ledger.outbox_counts()["dead"], 2)
        self.assertIsNone(
            self.adapter._ledger.get_outbox_item(
                "activity:closure-error:error:nonrecursive-cleanup"
            )
        )

    async def test_outbox_preserves_ephemeral_activity_flag(self):
        self.adapter._ledger.enqueue_outbox(
            "activity:ephemeral:1",
            "session-ephemeral",
            "activity.create",
            {
                "activity_id": "activity-ephemeral-1",
                "agent_session_id": "session-ephemeral",
                "activity_type": "thought",
                "body": "Closure is being verified.",
                "ephemeral": True,
            },
        )

        await self.adapter._drain_outbox_once()

        self.assertEqual(self.adapter._linear.activity_ephemeral, [True])

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
        self.adapter._planned_activation_enabled = False
        self.adapter._activation_allowed_team_ids = set()
        self.adapter._planned_owner_ids = set()
        self.adapter._closure_reconciliation_enabled = False
        self.adapter._closure_allowed_team_ids = set()
        self.events = []

        async def capture(event):
            self.events.append(event)

        self.adapter.handle_message = capture

    async def test_health_version_matches_plugin_manifest(self):
        manifest_version = next(
            line.split(":", 1)[1].strip().strip('"\'')
            for line in (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8").splitlines()
            if line.startswith("version:")
        )
        response = await self.adapter._health(None)
        self.assertEqual(json.loads(response.text)["version"], manifest_version)

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

    async def test_todo_transition_delegates_and_starts_one_manager_session(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-manager-intake"
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-07T10:01:00.000Z",
            "state": {"id": "todo-1", "name": "Todo", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "backlog-1", "name": "Backlog", "type": "backlog"},
                {"id": "todo-1", "name": "Todo", "type": "unstarted"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {},
        }
        transition = self.make_data_payload(
            webhookId="webhook-manager-intake",
            data={
                "id": issue_id,
                "updatedAt": "2026-08-07T10:01:00.000Z",
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )

        delegated = await self.adapter._handle_webhook(self.request_for(transition))
        self.assertEqual(json.loads(delegated.text)["status"], "manager_delegated")
        self.assertEqual(
            self.adapter._linear.delegate_assignments,
            [(issue_id, "agent-derya")],
        )
        self.adapter._linear.closure_contexts[issue_id]["delegate"] = {
            "id": "agent-derya",
            "name": "Derya",
        }
        created = self.make_payload(
            webhookId="webhook-manager-session",
            actor={"id": "agent-derya", "name": "Derya"},
            agentSession={
                "id": "session-manager-intake",
                "issue": {
                    "id": issue_id,
                    "identifier": "OPS-202",
                    "title": "Manager intake",
                },
            },
        )
        accepted = await self.adapter._handle_webhook(self.request_for(created))
        self.assertEqual(json.loads(accepted.text)["status"], "accepted")
        self.assertEqual(len(self.events), 1)
        self.assertIn("Adapter-verified lifecycle activation", self.events[0].text)
        self.assertEqual(
            self.adapter._ledger.get_manager_activation(issue_id)["state"],
            "session_started",
        )

        replay = await self.adapter._handle_webhook(self.request_for(created))
        self.assertEqual(json.loads(replay.text)["status"], "duplicate")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.adapter._linear.delegate_assignments), 1)

    async def test_distinct_concurrent_self_created_sessions_cas_one_manager_dispatch(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-manager-concurrent"
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-07T10:01:00.000Z",
            "state": {"id": "todo-1", "name": "Todo", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "team_states": [],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        self.adapter._ledger.claim_manager_activation(
            issue_id, "activation-concurrent", {"issue_id": issue_id}
        )
        self.adapter._ledger.mark_manager_activation(issue_id, "delegated")

        entered = asyncio.Event()

        async def slow_capture(event):
            self.events.append(event)
            entered.set()
            await asyncio.sleep(0.05)

        self.adapter.handle_message = slow_capture
        payloads = [
            self.make_payload(
                webhookId=f"webhook-manager-concurrent-{index}",
                actor={"id": "agent-derya", "name": "Derya"},
                agentSession={
                    "id": f"session-manager-concurrent-{index}",
                    "issue": {
                        "id": issue_id,
                        "identifier": "OPS-203",
                        "title": "Concurrent manager intake",
                    },
                },
            )
            for index in (1, 2)
        ]

        first, second = await asyncio.gather(
            *(self.adapter._handle_webhook(self.request_for(item)) for item in payloads)
        )

        self.assertTrue(entered.is_set())
        self.assertEqual(len(self.events), 1)
        self.assertEqual(
            sorted(json.loads(response.text)["status"] for response in (first, second)),
            ["accepted", "manager_session_duplicate"],
        )
        activation = self.adapter._ledger.get_manager_activation(issue_id)
        self.assertEqual(activation["state"], "session_started")
        self.assertEqual(activation["session_id"], self.events[0].source.chat_id)

    async def test_foreign_actor_created_manager_session_uses_manager_cas_path(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-manager-foreign-actor"
        self.adapter._ledger.claim_manager_activation(
            issue_id, "activation-foreign", {"issue_id": issue_id}
        )
        self.adapter._ledger.mark_manager_activation(issue_id, "delegated")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "state": {"id": "todo-1", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        created = self.make_payload(
            webhookId="webhook-manager-foreign",
            actor={"id": "user-foreign", "name": "Other actor"},
            agentSession={
                "id": "session-manager-foreign",
                "issue": {"id": issue_id, "identifier": "OPS-206", "title": "Foreign"},
            },
        )

        with (
            mock.patch.object(
                self.adapter._linear,
                "get_issue_closure_context",
                wraps=self.adapter._linear.get_issue_closure_context,
            ) as readback,
            mock.patch.object(
                self.adapter._ledger,
                "claim_manager_session",
                wraps=self.adapter._ledger.claim_manager_session,
            ) as claim_session,
        ):
            response = await self.adapter._handle_webhook(self.request_for(created))

        self.assertEqual(json.loads(response.text)["status"], "accepted")
        readback.assert_awaited_once_with(issue_id)
        claim_session.assert_called_once_with(issue_id, "session-manager-foreign")
        self.assertEqual(len(self.events), 1)
        activation = self.adapter._ledger.get_manager_activation(issue_id)
        self.assertEqual(activation["state"], "session_started")
        self.assertEqual(activation["session_id"], "session-manager-foreign")

    async def test_delegation_unknown_self_created_event_retries_until_readback_confirms(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-manager-delegation-unknown"
        self.adapter._ledger.claim_manager_activation(
            issue_id, "activation-delegation-unknown", {"issue_id": issue_id}
        )
        self.adapter._ledger.mark_manager_activation(issue_id, "delegation_unknown")
        context = {
            "id": issue_id,
            "state": {"id": "todo-1", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "assignee": {"id": "user-1"},
            "delegate": {},
        }
        self.adapter._linear.closure_contexts[issue_id] = context
        created = self.make_payload(
            webhookId="webhook-manager-delegation-unknown",
            actor={"id": "agent-derya", "name": "Derya"},
            agentSession={
                "id": "session-manager-delegation-unknown",
                "issue": {"id": issue_id, "identifier": "OPS-207", "title": "Unknown"},
            },
        )

        deferred = await self.adapter._handle_webhook(self.request_for(created))
        self.assertEqual(deferred.status, 503)
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.adapter._ledger.get_manager_activation(issue_id)["state"],
            "delegation_unknown",
        )

        context["delegate"] = {"id": "agent-derya"}
        accepted = await self.adapter._handle_webhook(self.request_for(created))
        self.assertEqual(json.loads(accepted.text)["status"], "accepted")
        self.assertEqual(len(self.events), 1)

    async def test_concurrent_mixed_actor_manager_sessions_cas_one_dispatch(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-manager-mixed-actors"
        self.adapter._ledger.claim_manager_activation(
            issue_id, "activation-mixed", {"issue_id": issue_id}
        )
        self.adapter._ledger.mark_manager_activation(issue_id, "delegated")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "state": {"id": "todo-1", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }

        async def slow_capture(event):
            self.events.append(event)
            await asyncio.sleep(0.05)

        self.adapter.handle_message = slow_capture
        payloads = [
            self.make_payload(
                webhookId=f"webhook-manager-mixed-{index}",
                actor={"id": actor_id},
                agentSession={
                    "id": f"session-manager-mixed-{index}",
                    "issue": {"id": issue_id, "identifier": "OPS-208", "title": "Mixed"},
                },
            )
            for index, actor_id in ((1, "agent-derya"), (2, "user-foreign"))
        ]

        responses = await asyncio.gather(
            *(self.adapter._handle_webhook(self.request_for(item)) for item in payloads)
        )

        self.assertEqual(len(self.events), 1)
        self.assertEqual(
            sorted(json.loads(response.text)["status"] for response in responses),
            ["accepted", "manager_session_duplicate"],
        )

    async def test_manager_dispatch_rechecks_live_todo_policy_and_delegate(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-manager-drift"
        self.adapter._ledger.claim_manager_activation(
            issue_id, "activation-drift", {"issue_id": issue_id}
        )
        self.adapter._ledger.mark_manager_activation(issue_id, "delegated")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-07T10:02:00.000Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [],
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        created = self.make_payload(
            webhookId="webhook-manager-drift",
            actor={"id": "agent-derya", "name": "Derya"},
            agentSession={
                "id": "session-manager-drift",
                "issue": {"id": issue_id, "identifier": "OPS-204", "title": "Drift"},
            },
        )

        response = await self.adapter._handle_webhook(self.request_for(created))

        self.assertEqual(json.loads(response.text)["status"], "activation_policy_denied")
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.adapter._ledger.get_manager_activation(issue_id)["state"], "canceled"
        )

    async def test_manager_dispatch_failure_leaves_ambiguity_and_degrades_without_replay(self):
        self.adapter._running = True
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-manager-unknown"
        self.adapter._ledger.claim_manager_activation(
            issue_id, "activation-unknown", {"issue_id": issue_id}
        )
        self.adapter._ledger.mark_manager_activation(issue_id, "delegated")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "state": {"id": "todo-1", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        calls = 0

        async def lost_acceptance(_event):
            nonlocal calls
            calls += 1
            raise RuntimeError("acceptance response lost")

        self.adapter.handle_message = lost_acceptance
        created = self.make_payload(
            webhookId="webhook-manager-unknown",
            actor={"id": "agent-derya", "name": "Derya"},
            agentSession={
                "id": "session-manager-unknown",
                "issue": {"id": issue_id, "identifier": "OPS-205", "title": "Unknown"},
            },
        )

        failed = await self.adapter._handle_webhook(self.request_for(created))
        retry = dict(created, webhookId="webhook-manager-unknown-retry")
        replay = await self.adapter._handle_webhook(self.request_for(retry))
        health = json.loads((await self.adapter._health(None)).text)

        self.assertEqual(failed.status, 503)
        self.assertEqual(json.loads(replay.text)["status"], "dispatch_ambiguous")
        self.assertEqual(calls, 1)
        self.assertEqual(
            self.adapter._ledger.get_manager_activation(issue_id)["state"],
            "dispatch_unknown",
        )
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["manager_activations"]["dispatch_unknown"], 1)

    async def test_lost_delegate_response_reconciles_from_authoritative_readback(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-delegate-lost-response"
        context = {
            "id": issue_id,
            "updated_at": "2026-08-07T10:03:00.000Z",
            "state": {"id": "todo-1", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "team_states": [{"id": "backlog-1", "type": "backlog"}],
            "assignee": {"id": "user-1"},
            "delegate": {},
        }
        self.adapter._linear.closure_contexts[issue_id] = context

        async def committed_then_lost(issue, delegate):
            self.adapter._linear.delegate_assignments.append((issue, delegate))
            context["delegate"] = {"id": delegate}
            raise RuntimeError("response lost")

        self.adapter._linear.assign_issue_delegate = committed_then_lost
        transition = self.make_data_payload(
            webhookId="webhook-delegate-lost-response",
            data={
                "id": issue_id,
                "updatedAt": context["updated_at"],
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(transition))

        self.assertEqual(json.loads(response.text)["status"], "manager_delegated")
        self.assertEqual(
            self.adapter._ledger.get_manager_activation(issue_id)["state"], "delegated"
        )
        self.assertEqual(len(self.adapter._linear.delegate_assignments), 1)

    async def test_delegate_outcome_unknown_is_not_replayed_and_later_readback_reconciles(self):
        self.adapter._running = True
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-delegate-unknown"
        context = {
            "id": issue_id,
            "updated_at": "2026-08-07T10:04:00.000Z",
            "state": {"id": "todo-1", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "team_states": [{"id": "backlog-1", "type": "backlog"}],
            "assignee": {"id": "user-1"},
            "delegate": {},
        }
        self.adapter._linear.closure_contexts[issue_id] = context
        attempts = 0

        async def always_lost(_issue, _delegate):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("response lost")

        self.adapter._linear.assign_issue_delegate = always_lost
        transition = self.make_data_payload(
            webhookId="webhook-delegate-unknown-1",
            data={
                "id": issue_id,
                "updatedAt": context["updated_at"],
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )

        first = await self.adapter._handle_webhook(self.request_for(transition))
        retry = dict(transition, webhookId="webhook-delegate-unknown-2")
        second = await self.adapter._handle_webhook(self.request_for(retry))
        context["delegate"] = {"id": "agent-derya"}
        later = dict(transition, webhookId="webhook-delegate-unknown-3")
        third = await self.adapter._handle_webhook(self.request_for(later))
        health = json.loads((await self.adapter._health(None)).text)

        self.assertEqual(first.status, 503)
        self.assertEqual(json.loads(second.text)["status"], "delegation_ambiguous")
        self.assertEqual(json.loads(third.text)["status"], "manager_delegated")
        self.assertEqual(attempts, 1)
        self.assertEqual(
            self.adapter._ledger.get_manager_activation(issue_id)["state"], "delegated"
        )
        self.assertEqual(health["manager_activations"]["delegation_unknown"], 0)

    async def test_backlog_session_uses_one_shot_verified_todo_activation(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-planned"
        created = self.make_payload(
            agentSession={
                "id": "session-planned",
                "issue": {
                    "id": issue_id,
                    "identifier": "OPS-200",
                    "title": "Planned work",
                },
            }
        )
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-07T08:00:00.000Z",
            "state": {"id": "backlog-1", "name": "Backlog", "type": "backlog"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "backlog-1", "name": "Backlog", "type": "backlog"},
                {"id": "todo-1", "name": "Todo", "type": "unstarted"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }

        waiting = await self.adapter._handle_webhook(self.request_for(created))
        self.assertEqual(json.loads(waiting.text)["status"], "waiting_for_activation")
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.adapter._ledger.get_activation_wait(issue_id)["session_id"],
            "session-planned",
        )

        self.adapter._linear.closure_contexts[issue_id].update(
            {
                "updated_at": "2026-08-07T08:01:00.000Z",
                "state": {"id": "todo-1", "name": "Todo", "type": "unstarted"},
            }
        )
        activated = self.make_data_payload(
            webhookId="webhook-planned-activation-1",
            data={
                "id": issue_id,
                "updatedAt": "2026-08-07T08:01:00.000Z",
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )
        first = await self.adapter._handle_webhook(self.request_for(activated))
        self.assertEqual(json.loads(first.text)["status"], "activation_resumed")
        self.assertEqual(len(self.events), 1)
        self.assertIn("Adapter-verified lifecycle activation", self.events[0].text)

        semantic_duplicate = dict(activated)
        semantic_duplicate["webhookId"] = "webhook-planned-activation-2"
        second = await self.adapter._handle_webhook(
            self.request_for(semantic_duplicate)
        )
        self.assertEqual(json.loads(second.text)["status"], "duplicate")
        self.assertEqual(len(self.events), 1)

    async def test_backlog_parking_rejects_non_planned_owner(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-wrong-owner"
        self.adapter._linear.closure_contexts[issue_id] = {
            "state": {"id": "backlog-1", "type": "backlog"},
            "team": {"id": "team-ops"},
            "assignee": {"id": "user-2"},
            "delegate": {"id": "agent-derya"},
        }
        created = self.make_payload(
            webhookId="webhook-wrong-owner",
            agentSession={
                "id": "session-wrong-owner",
                "issue": {"id": issue_id, "identifier": "OPS-206", "title": "Owner"},
            },
        )

        response = await self.adapter._handle_webhook(self.request_for(created))

        self.assertEqual(json.loads(response.text)["status"], "activation_policy_denied")
        self.assertEqual(self.adapter._ledger.get_activation_wait(issue_id), None)
        self.assertEqual(self.events, [])

    async def test_parked_dispatch_rechecks_live_delegate_immediately_before_handle(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-parked-delegate-drift"
        created = self.make_payload(
            webhookId="webhook-parked-delegate-drift-created",
            agentSession={
                "id": "session-parked-delegate-drift",
                "issue": {"id": issue_id, "identifier": "OPS-207", "title": "Drift"},
            },
        )
        context = {
            "id": issue_id,
            "updated_at": "2026-08-07T11:00:00.000Z",
            "state": {"id": "backlog-1", "type": "backlog"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "backlog-1", "type": "backlog"},
                {"id": "todo-1", "type": "unstarted"},
            ],
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        self.adapter._linear.closure_contexts[issue_id] = context
        await self.adapter._handle_webhook(self.request_for(created))
        context.update(
            {
                "updated_at": "2026-08-07T11:01:00.000Z",
                "state": {"id": "todo-1", "type": "unstarted"},
                "delegate": {"id": "agent-other"},
            }
        )
        activated = self.make_data_payload(
            webhookId="webhook-parked-delegate-drift-activation",
            data={
                "id": issue_id,
                "updatedAt": context["updated_at"],
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(activated))

        self.assertEqual(json.loads(response.text)["status"], "activation_rejected")
        self.assertEqual(self.events, [])

    async def test_parked_dispatch_failure_is_ambiguous_degraded_and_never_replayed(self):
        self.adapter._running = True
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-parked-unknown"
        created = self.make_payload(
            webhookId="webhook-parked-unknown-created",
            agentSession={
                "id": "session-parked-unknown",
                "issue": {"id": issue_id, "identifier": "OPS-208", "title": "Unknown"},
            },
        )
        context = {
            "id": issue_id,
            "updated_at": "2026-08-07T12:00:00.000Z",
            "state": {"id": "backlog-1", "type": "backlog"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "backlog-1", "type": "backlog"},
                {"id": "todo-1", "type": "unstarted"},
            ],
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        self.adapter._linear.closure_contexts[issue_id] = context
        await self.adapter._handle_webhook(self.request_for(created))
        context.update(
            {
                "updated_at": "2026-08-07T12:01:00.000Z",
                "state": {"id": "todo-1", "type": "unstarted"},
            }
        )
        calls = 0

        async def lost_acceptance(_event):
            nonlocal calls
            calls += 1
            raise RuntimeError("acceptance response lost")

        self.adapter.handle_message = lost_acceptance
        activated = self.make_data_payload(
            webhookId="webhook-parked-unknown-1",
            data={
                "id": issue_id,
                "updatedAt": context["updated_at"],
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )

        first = await self.adapter._handle_webhook(self.request_for(activated))
        retry = dict(activated, webhookId="webhook-parked-unknown-2")
        second = await self.adapter._handle_webhook(self.request_for(retry))
        health = json.loads((await self.adapter._health(None)).text)

        self.assertEqual(first.status, 503)
        self.assertEqual(json.loads(second.text)["status"], "activation_ambiguous")
        self.assertEqual(calls, 1)
        self.assertEqual(
            self.adapter._ledger.get_activation_wait(issue_id)["state"],
            "dispatch_unknown",
        )
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["activations"]["dispatch_unknown"], 1)

    async def test_issue_lock_barrier_stop_wins_over_parked_activation(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-stop-race"
        session_id = "session-stop-race"
        created = self.make_payload(
            webhookId="webhook-stop-race-created",
            agentSession={
                "id": session_id,
                "issue": {"id": issue_id, "identifier": "OPS-209", "title": "Stop race"},
            },
        )
        self.adapter._ledger.put_activation_wait(
            session_id, issue_id, "delivery-stop-race", created
        )
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-07T13:01:00.000Z",
            "state": {"id": "todo-1", "type": "unstarted"},
            "team": {"id": "team-ops"},
            "team_states": [{"id": "backlog-1", "type": "backlog"}],
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        stop = self.make_payload(
            webhookId="webhook-stop-race-stop",
            action="prompted",
            agentActivity={"id": "activity-stop-race", "signal": "stop", "body": "stop"},
            agentSession={
                "id": session_id,
                "issue": {"id": issue_id, "identifier": "OPS-209", "title": "Stop race"},
            },
        )
        activation = self.make_data_payload(
            webhookId="webhook-stop-race-activation",
            data={
                "id": issue_id,
                "updatedAt": "2026-08-07T13:01:00.000Z",
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )
        barrier = self.adapter._issue_lock(issue_id)
        await barrier.acquire()
        stop_task = asyncio.create_task(
            self.adapter._handle_webhook(self.request_for(stop))
        )
        await asyncio.sleep(0)
        activation_task = asyncio.create_task(
            self.adapter._handle_webhook(self.request_for(activation))
        )
        await asyncio.sleep(0)
        barrier.release()

        stop_response, activation_response = await asyncio.gather(
            stop_task, activation_task
        )

        self.assertEqual(json.loads(stop_response.text)["status"], "accepted")
        self.assertEqual(
            json.loads(activation_response.text)["status"], "activation_rejected"
        )
        self.assertEqual([event.text for event in self.events], ["/stop"])
        self.assertEqual(
            self.adapter._ledger.get_activation_wait(issue_id)["state"], "canceled"
        )

    async def test_issue_lock_barrier_done_wins_over_parked_activation(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-done-race"
        self.adapter._ledger.put_activation_wait(
            "session-done-race",
            issue_id,
            "delivery-done-race",
            self.make_payload(),
        )
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-07T14:01:00.000Z",
            "state": {"id": "done-1", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [{"id": "backlog-1", "type": "backlog"}],
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        done = self.make_data_payload(
            webhookId="webhook-done-race-done",
            data={
                "id": issue_id,
                "updatedAt": "2026-08-07T14:01:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )
        activation = self.make_data_payload(
            webhookId="webhook-done-race-activation",
            data={
                "id": issue_id,
                "updatedAt": "2026-08-07T14:00:00.000Z",
                "state": {"id": "todo-1", "type": "unstarted"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )
        barrier = self.adapter._issue_lock(issue_id)
        await barrier.acquire()
        done_task = asyncio.create_task(
            self.adapter._handle_webhook(self.request_for(done))
        )
        await asyncio.sleep(0)
        activation_task = asyncio.create_task(
            self.adapter._handle_webhook(self.request_for(activation))
        )
        await asyncio.sleep(0)
        barrier.release()

        _, activation_response = await asyncio.gather(done_task, activation_task)

        self.assertEqual(
            json.loads(activation_response.text)["status"], "activation_rejected"
        )
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.adapter._ledger.get_activation_wait(issue_id)["state"], "canceled"
        )

    async def test_terminal_human_state_cancels_parked_activation_without_dispatch(self):
        self.adapter._planned_activation_enabled = True
        self.adapter._activation_allowed_team_ids = {"team-ops"}
        self.adapter._planned_owner_ids = {"user-1"}
        issue_id = "issue-terminal-before-todo"
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-07T09:00:00.000Z",
            "state": {"id": "backlog-1", "name": "Backlog", "type": "backlog"},
            "team": {"id": "team-ops"},
            "team_states": [{"id": "backlog-1", "type": "backlog"}],
            "assignee": {"id": "user-1"},
            "delegate": {"id": "agent-derya"},
        }
        created = self.make_payload(
            agentSession={
                "id": "session-terminal-before-todo",
                "issue": {"id": issue_id, "identifier": "OPS-201", "title": "Stop"},
            }
        )
        await self.adapter._handle_webhook(self.request_for(created))

        completed = self.make_data_payload(
            webhookId="webhook-terminal-before-todo",
            data={
                "id": issue_id,
                "updatedAt": "2026-08-07T09:01:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )
        response = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(response.status, 200)
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.adapter._ledger.get_activation_wait(issue_id)["state"], "canceled"
        )

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

    async def test_prompted_event_with_terminal_fence_never_dispatches(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-prompted-terminal-fence"
        terminal_event = {
            "actor": {"id": "user-1"},
            "data": {
                "id": issue_id,
                "updatedAt": "2026-08-05T08:00:00.000Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            "updatedFrom": {"stateId": "started-1"},
        }
        self.adapter._ledger.stage_pending_closure_event(issue_id, 1.0, terminal_event)
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-05T08:00:00.000Z",
            "completed_at": "2026-08-05T08:00:00.000Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        prompted = self.make_payload(
            webhookId="webhook-prompted-terminal-fence",
            action="prompted",
            actor={"id": "user-1", "name": "Mutlu"},
            agentActivity={"id": "activity-terminal-follow-up", "body": "run again"},
            agentSession={
                "id": "session-prompted-terminal-fence",
                "issue": {"id": issue_id, "identifier": "OPS-73", "title": "Closed"},
            },
        )

        response = await self.adapter._handle_webhook(self.request_for(prompted))

        self.assertEqual(json.loads(response.text)["status"], "terminal_fenced")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.get_issue_session(issue_id), None)
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 1)

    async def test_self_authored_agent_session_event_is_ignored(self):
        payload = self.make_payload(
            webhookId="webhook-self-agent-session",
            actor={"id": "agent-derya", "name": "Derya"},
            agentSession={
                "id": "session-self-agent-session",
                "issue": {"id": "issue-self-agent-session", "identifier": "OPS-80"},
            },
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "ignored_self")
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.adapter._ledger.get_issue_session("issue-self-agent-session"), None
        )

    async def test_different_app_actor_agent_session_handoff_is_accepted(self):
        payload = self.make_payload(
            webhookId="webhook-other-app-handoff",
            actor={"id": "agent-doruk", "name": "Doruk"},
            agentSession={
                "id": "session-other-app-handoff",
                "issue": {"id": "issue-other-app-handoff", "identifier": "OPS-81"},
            },
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "accepted")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].source.user_id, "agent-doruk")
        self.assertEqual(
            self.adapter._ledger.get_issue_session("issue-other-app-handoff"),
            "session-other-app-handoff",
        )


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

    async def test_blocker_update_uses_one_shot_live_resume_claim(self):
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
        self.adapter._linear.activity_ephemeral.clear()
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
            "updated_at": "2026-08-04T12:21:04.002Z",
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
        self.assertEqual((session_id, activity_type), ("session-closure", "thought"))
        self.assertIn("Done received", body)
        self.assertTrue(self.adapter._linear.activity_ephemeral[0])
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)

        await self.adapter._drain_outbox_once()
        self.assertEqual(len(self.adapter._linear.calls), 2)
        session_id, activity_type, body = self.adapter._linear.calls[1]
        self.assertEqual((session_id, activity_type), ("session-closure", "response"))
        self.assertFalse(self.adapter._linear.activity_ephemeral[1])
        self.assertIn("Closure reconciliation complete", body)
        self.assertIn("Mutlu", body)
        self.assertIn("In Progress", body)
        self.assertIn("Done", body)
        self.assertIn("not rerun", body)
        self.assertEqual(self.adapter._ledger.closure_counts()["completed"], 1)
        self.assertEqual(self.adapter._ledger.get_wait("session-closure")["state"], "canceled")

        late = await self.adapter.send("session-closure", "late main deliverable")
        self.assertTrue(late.success)
        self.assertEqual(len(self.adapter._linear.calls), 2)

        self.adapter._ledger.bind_issue_session(
            "issue-closure", "session-newer", now=int(time.time()) + 1
        )
        closure_duplicate = await self.adapter._reconcile_human_completion(
            completed, "issue-closure"
        )
        self.assertEqual(closure_duplicate, "closure_duplicate")
        self.assertEqual(len(self.adapter._linear.calls), 2)

        tolerated_revision = dict(completed)
        tolerated_revision["data"] = {
            **completed["data"],
            "updatedAt": "2026-08-04T12:21:04.999Z",
        }
        closure_revision_rejected = await self.adapter._reconcile_human_completion(
            tolerated_revision, "issue-closure"
        )
        self.assertEqual(closure_revision_rejected, "closure_rejected")
        self.assertEqual(self.adapter._ledger.closure_counts()["completed"], 1)
        self.assertEqual(len(self.adapter._linear.calls), 2)

        duplicate_delivery = dict(completed, webhookId="webhook-human-completed-2")
        duplicate = await self.adapter._handle_webhook(self.request_for(duplicate_delivery))
        self.assertEqual(json.loads(duplicate.text)["status"], "duplicate")
        self.assertEqual(len(self.adapter._linear.calls), 2)
        self.assertEqual(self.events, [])

    async def test_done_before_session_creation_is_durably_fenced_and_reconciled(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        self.adapter._linear.closure_contexts["issue-out-of-order"] = {
            "id": "issue-out-of-order",
            "identifier": "OPS-73",
            "title": "Out of order closure",
            "updated_at": "2026-08-04T12:30:00.000Z",
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

        self.assertEqual(json.loads(deferred.text)["status"], "terminal_fenced")
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 1)
        self.assertEqual(self.events, [])
        self.adapter._running = True
        fenced_health = json.loads((await self.adapter._health(None)).text)
        self.assertEqual(fenced_health["status"], "ok")
        self.assertEqual(fenced_health["closures"]["terminal_fences"], 1)
        self.assertEqual(fenced_health["closures"]["blocked_dispatch"], 0)
        self.assertEqual(fenced_health["closures"]["pending_session_binding"], 0)

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
            ["thought"],
        )
        self.assertTrue(self.adapter._linear.activity_ephemeral[0])
        await self.adapter._drain_outbox_once()
        self.assertEqual(
            [call[1] for call in self.adapter._linear.calls],
            ["thought", "response"],
        )

    async def test_human_done_recovers_unique_authoritative_session_binding(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-preexisting-session"
        session_id = "session-preexisting"
        revision = "2026-08-07T01:02:03.456Z"
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "identifier": "OPS-104",
            "title": "Preexisting session closure",
            "updated_at": revision,
            "completed_at": revision,
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        self.adapter._linear.issue_agent_sessions[issue_id] = [{
            "id": session_id,
            "status": "complete",
            "started_at": "2026-08-06T23:00:00.000Z",
            "ended_at": "2026-08-07T00:30:00.000Z",
            "app_user_id": "agent-derya",
        }]
        completed = self.make_data_payload(
            webhookId="webhook-preexisting-session-done",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": revision,
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        async with self.adapter._session_lock(session_id):
            closure_task = asyncio.create_task(
                self.adapter._handle_webhook(self.request_for(completed))
            )
            await asyncio.sleep(0)
            self.assertFalse(
                closure_task.done(),
                "recovered closure must acquire the discovered session lock",
            )

        response = await closure_task

        self.assertEqual(json.loads(response.text)["status"], "closure_queued")
        self.assertEqual(self.adapter._ledger.get_issue_session(issue_id), session_id)
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 0)
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)
        self.assertEqual(self.events, [])

    def configure_unbound_human_done(self, issue_id: str, revision: str):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "identifier": "OPS-106",
            "title": "Recover authoritative closure session",
            "updated_at": revision,
            "completed_at": revision,
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        return self.make_data_payload(
            webhookId=f"webhook-{issue_id}",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": revision,
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

    async def test_human_done_prefers_unique_open_session_over_complete_sessions(self):
        issue_id = "issue-open-preferred"
        completed = self.configure_unbound_human_done(
            issue_id, "2026-08-07T02:00:00.000Z"
        )
        self.adapter._linear.issue_agent_sessions[issue_id] = [
            {"id": "complete-1", "status": "complete", "app_user_id": "agent-derya"},
            {"id": "active-1", "status": "active", "app_user_id": "agent-derya"},
            {"id": "complete-2", "status": "complete", "app_user_id": "agent-derya"},
        ]

        response = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(response.text)["status"], "closure_queued")
        self.assertEqual(self.adapter._ledger.get_issue_session(issue_id), "active-1")

    async def test_human_done_does_not_guess_between_ambiguous_owned_sessions(self):
        issue_id = "issue-ambiguous-sessions"
        completed = self.configure_unbound_human_done(
            issue_id, "2026-08-07T02:01:00.000Z"
        )
        self.adapter._linear.issue_agent_sessions[issue_id] = [
            {"id": "active-1", "status": "active", "app_user_id": "agent-derya"},
            {"id": "pending-2", "status": "pending", "app_user_id": "agent-derya"},
        ]

        response = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(response.text)["status"], "terminal_fenced")
        self.assertIsNone(self.adapter._ledger.get_issue_session(issue_id))
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 0)

    async def test_human_done_ignores_foreign_session_candidates(self):
        issue_id = "issue-foreign-session"
        completed = self.configure_unbound_human_done(
            issue_id, "2026-08-07T02:02:00.000Z"
        )
        self.adapter._linear.issue_agent_sessions[issue_id] = [
            {"id": "foreign-1", "status": "active", "app_user_id": "agent-other"}
        ]

        response = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(response.text)["status"], "terminal_fenced")
        self.assertIsNone(self.adapter._ledger.get_issue_session(issue_id))

    async def test_human_done_retries_when_session_list_read_fails_then_replays(self):
        issue_id = "issue-session-read-failure"
        completed = self.configure_unbound_human_done(
            issue_id, "2026-08-07T02:03:00.000Z"
        )
        self.adapter._linear.get_issue_agent_sessions = mock.AsyncMock(
            side_effect=[
                LinearAPIError("temporary policy read failure", retryable=True),
                [{
                    "id": "session-after-retry",
                    "status": "active",
                    "app_user_id": "agent-derya",
                }],
            ]
        )

        deferred = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(deferred.status, 503)
        self.assertIsNone(self.adapter._ledger.get_issue_session(issue_id))
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 0)
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 0)

        replay = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(replay.text)["status"], "closure_queued")
        self.assertEqual(
            self.adapter._ledger.get_issue_session(issue_id), "session-after-retry"
        )

    async def test_human_done_fails_closed_on_permanent_session_policy_failure(self):
        issue_id = "issue-session-permanent-failure"
        completed = self.configure_unbound_human_done(
            issue_id, "2026-08-07T02:03:30.000Z"
        )
        self.adapter._linear.get_issue_agent_sessions = mock.AsyncMock(
            side_effect=LinearAPIError("incomplete policy data", retryable=False)
        )

        response = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(response.text)["status"], "terminal_fenced")
        self.assertIsNone(self.adapter._ledger.get_issue_session(issue_id))
        self.assertEqual(self.adapter._ledger.pending_closure_count(), 1)

    async def test_human_done_fences_when_issue_has_no_sessions(self):
        issue_id = "issue-without-sessions"
        completed = self.configure_unbound_human_done(
            issue_id, "2026-08-07T02:04:00.000Z"
        )

        response = await self.adapter._handle_webhook(self.request_for(completed))

        self.assertEqual(json.loads(response.text)["status"], "terminal_fenced")
        self.assertIsNone(self.adapter._ledger.get_issue_session(issue_id))

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
            "updated_at": "2026-08-04T12:31:00.000Z",
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
        self.adapter._running = True
        reconciling_health = json.loads((await self.adapter._health(None)).text)
        self.assertEqual(reconciling_health["status"], "ok")
        self.assertEqual(reconciling_health["closures"]["terminal_fences"], 0)
        self.assertEqual(reconciling_health["closures"]["blocked_dispatch"], 0)
        linear.release_read.set()

        self.assertEqual(json.loads((await done_task).text)["status"], "terminal_fenced")
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
            "updated_at": "2026-08-04T12:33:10.000Z",
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
        self.assertEqual(health_body["closures"]["terminal_fences"], 0)
        self.assertEqual(health_body["closures"]["blocked_dispatch"], 1)
        self.assertEqual(health_body["closures"]["pending_session_binding"], 1)

    async def test_closure_accepts_current_signed_transition_without_history(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-history-stale"
        session_id = "session-history-stale"
        self.adapter._ledger.bind_issue_session(issue_id, session_id)
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "identifier": "OPS-78",
            "title": "Reopened closure canary",
            "updated_at": "2026-08-05T06:21:33.866Z",
            "completed_at": "2026-08-05T06:21:33.866Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "backlog-1", "name": "Backlog", "type": "backlog"},
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        payload = self.make_data_payload(
            webhookId="webhook-history-stale-current-completion",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": "2026-08-05T06:21:33.866Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "closure_queued")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)

    async def test_closure_accepts_stale_completed_at_when_live_revision_matches(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-stale-completed-at"
        self.adapter._ledger.bind_issue_session(issue_id, "session-stale-completed-at")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-05T07:04:39.273Z",
            # Linear can retain the prior completion time after reopen/recomplete.
            "completed_at": "2026-08-05T06:21:33.871Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        payload = self.make_data_payload(
            webhookId="webhook-stale-completed-at-current-revision",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": "2026-08-05T07:04:39.273Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "closure_queued")
        self.assertEqual(self.events, [])
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)

        # Audit-only completedAt changes must not change semantic closure identity.
        self.adapter._linear.closure_contexts[issue_id]["completed_at"] = (
            "2026-08-05T07:04:39.273Z"
        )
        duplicate = await self.adapter._reconcile_human_completion(payload, issue_id)
        self.assertEqual(duplicate, "closure_duplicate")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)

        next_revision = json.loads(json.dumps(payload))
        next_revision["data"]["updatedAt"] = "2026-08-05T07:05:39.273Z"
        self.adapter._linear.closure_contexts[issue_id]["updated_at"] = (
            "2026-08-05T07:05:39.273Z"
        )
        distinct = await self.adapter._reconcile_human_completion(next_revision, issue_id)
        self.assertEqual(distinct, "closure_queued")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 2)

    async def test_closure_rejects_live_revision_mismatch(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-live-revision-mismatch"
        self.adapter._ledger.bind_issue_session(issue_id, "session-live-revision-mismatch")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-05T06:21:34.000Z",
            "completed_at": "2026-08-05T06:21:33.866Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        payload = self.make_data_payload(
            webhookId="webhook-live-revision-mismatch",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": "2026-08-05T06:21:33.866Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "started-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "closure_rejected")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 0)

    async def test_closure_rejects_missing_webhook_destination_state(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-missing-destination-state"
        self.adapter._ledger.bind_issue_session(issue_id, "session-missing-destination-state")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-05T06:21:33.866Z",
            "completed_at": "2026-08-05T06:21:33.866Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        payload = self.make_data_payload(
            webhookId="webhook-missing-destination-state",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": "2026-08-05T06:21:33.866Z",
                "state": {},
            },
            updatedFrom={"stateId": "started-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "closure_rejected")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 0)

    async def test_closure_rejects_non_started_previous_state(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        issue_id = "issue-backlog-done"
        self.adapter._ledger.bind_issue_session(issue_id, "session-backlog-done")
        self.adapter._linear.closure_contexts[issue_id] = {
            "id": issue_id,
            "updated_at": "2026-08-05T06:16:56.110Z",
            "completed_at": "2026-08-05T06:16:56.110Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "backlog-1", "name": "Backlog", "type": "backlog"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
        }
        payload = self.make_data_payload(
            webhookId="webhook-backlog-done-rejected",
            actor={"id": "user-1", "name": "Mutlu"},
            data={
                "id": issue_id,
                "updatedAt": "2026-08-05T06:16:56.110Z",
                "state": {"id": "done-1", "type": "completed"},
            },
            updatedFrom={"stateId": "backlog-1"},
        )

        response = await self.adapter._handle_webhook(self.request_for(payload))

        self.assertEqual(json.loads(response.text)["status"], "closure_rejected")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 0)
        self.assertEqual(self.adapter._linear.calls, [])

    async def test_closure_fails_closed_for_spoofed_actor_wrong_team_or_wrong_delegate(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        cases = (
            ("spoofed-actor", "attacker", "team-ops", "agent-derya"),
            ("wrong-team", "user-1", "team-other", "agent-derya"),
            ("wrong-delegate", "user-1", "team-ops", "agent-other"),
        )
        for index, (label, actor_id, team_id, delegate_id) in enumerate(cases):
            with self.subTest(label=label):
                issue_id = f"issue-reject-{index}"
                session_id = f"session-reject-{index}"
                self.adapter._linear.closure_contexts[issue_id] = {
                    "id": issue_id,
                    "identifier": f"OPS-{80 + index}",
                    "title": label,
                    "updated_at": f"2026-08-04T12:22:0{index}.000Z",
                    "completed_at": f"2026-08-04T12:22:0{index}.000Z",
                    "state": {"id": "done-1", "name": "Done", "type": "completed"},
                    "team": {"id": team_id},
                    "team_states": [
                        {"id": "started-1", "name": "In Progress", "type": "started"},
                        {"id": "done-1", "name": "Done", "type": "completed"},
                    ],
                    "assignee": {"id": "user-1", "name": "Mutlu"},
                    "delegate": {"id": delegate_id, "name": "Delegate"},
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

    async def test_closure_treats_completed_at_skew_as_audit_only(self):
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        self.adapter._ledger.bind_issue_session("issue-skew", "session-skew")
        self.adapter._linear.closure_contexts["issue-skew"] = {
            "id": "issue-skew",
            "updated_at": "2026-08-04T12:30:00.000Z",
            "completed_at": "2026-08-04T12:30:06.001Z",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
            "team": {"id": "team-ops"},
            "team_states": [
                {"id": "started-1", "name": "In Progress", "type": "started"},
                {"id": "done-1", "name": "Done", "type": "completed"},
            ],
            "assignee": {"id": "user-1", "name": "Mutlu"},
            "delegate": {"id": "agent-derya", "name": "Derya"},
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

        self.assertEqual(json.loads(response.text)["status"], "closure_queued")
        self.assertEqual(self.adapter._ledger.closure_counts()["pending"], 1)
        self.assertEqual(self.adapter._linear.calls, [])

    async def test_closure_waits_for_claimed_network_dispatch_then_suppresses_later_writes(self):
        class BlockingLinear(FakeLinear):
            def __init__(self):
                super().__init__("org-1")
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def create_activity(
                self,
                session_id,
                activity_type,
                body,
                *,
                activity_id,
                ephemeral=False,
            ):
                self.started.set()
                await self.release.wait()
                return await super().create_activity(
                    session_id,
                    activity_type,
                    body,
                    activity_id=activity_id,
                    ephemeral=ephemeral,
                )

        linear = BlockingLinear()
        self.adapter._linear = linear
        self.adapter._closure_reconciliation_enabled = True
        self.adapter._closure_allowed_team_ids = {"team-ops"}
        self.adapter._ledger.bind_issue_session("issue-race", "session-race")
        linear.closure_contexts["issue-race"] = {
            "id": "issue-race",
            "updated_at": "2026-08-04T12:40:00.000Z",
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
        self.assertEqual([call[1] for call in linear.calls], ["thought", "thought"])
        self.assertEqual(linear.activity_ephemeral, [False, True])
        await self.adapter._drain_outbox_once()
        self.assertEqual([call[1] for call in linear.calls], ["thought", "thought", "response"])
        self.assertEqual(linear.activity_ephemeral, [False, True, False])

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
            "updated_at": "2026-08-04T12:45:00.000Z",
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

    async def test_operational_inbox_on_terminal_issue_remains_a_valid_transport_anchor(self):
        self.adapter._linear.delivery_contexts["session-terminal-inbox"] = {
            "id": "session-terminal-inbox",
            "status": "active",
            "app_user_id": "agent-derya",
            "issue_id": "issue-terminal-inbox",
            "state": {"id": "done-1", "name": "Done", "type": "completed"},
        }

        result = await self.adapter.send("session-terminal-inbox", "cron result")

        self.assertTrue(result.success)
        self.assertEqual(
            self.adapter._linear.calls,
            [("session-terminal-inbox", "response", "cron result")],
        )

    async def test_operational_delivery_rejects_session_owned_by_other_app(self):
        self.adapter._linear.delivery_contexts["session-other-app"] = {
            "id": "session-other-app",
            "status": "active",
            "app_user_id": "agent-other",
            "issue_id": "issue-other-app",
            "state": {"id": "started-1", "name": "In Progress", "type": "started"},
        }

        result = await self.adapter.send("session-other-app", "misrouted")

        self.assertFalse(result.success)
        self.assertIn("another app user", str(result.error))
        self.assertEqual(self.adapter._linear.calls, [])

    async def test_real_preflight_transport_failures_remain_retryable(self):
        class FakeOAuthStore:
            async def access_token(self, **_kwargs):
                return "redacted-test-token"

        class FailingRequest:
            def __init__(self, error):
                self.error = error

            async def __aenter__(self):
                raise self.error

            async def __aexit__(self, *_args):
                return False

        class FailingSession:
            def __init__(self, error):
                self.error = error

            def post(self, *_args, **_kwargs):
                return FailingRequest(self.error)

        client = LinearClient(oauth_store=FakeOAuthStore())
        client.actor_id = "agent-derya"
        client._session = cast(Any, FailingSession(asyncio.TimeoutError()))
        self.adapter._linear = client

        result = await self.adapter.send("session-timeout", "cron result")

        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertIn("timed out", str(result.error))

        self.adapter._outbox_base_delay = 0
        item_id = "activity:queued-preflight"
        self.adapter._ledger.enqueue_outbox(
            item_id,
            "session-queued",
            "activity.create",
            {
                "activity_id": "activity-queued",
                "agent_session_id": "session-queued",
                "activity_type": "response",
                "body": "queued cron result",
            },
        )
        await self.adapter._drain_outbox_once()

        item = self.adapter._ledger.get_outbox_item(item_id)
        self.assertEqual(item["state"], "pending")
        self.assertIn("timed out", item["last_error"])

        client._session = cast(
            Any,
            FailingSession(client_mod.aiohttp.ClientConnectionError("connection reset")),
        )
        await self.adapter._drain_outbox_once()

        item = self.adapter._ledger.get_outbox_item(item_id)
        self.assertEqual(item["state"], "pending")
        self.assertIn("connection failed", item["last_error"])

    def test_linear_declares_noneditable_to_disable_streaming_previews(self):
        self.assertFalse(
            getattr(self.adapter, "SUPPORTS_MESSAGE_EDITING", True),
            "Linear AgentActivities cannot edit streaming previews in place",
        )

    async def test_home_channel_notice_is_nonterminal_thought(self):
        body = (
            "📬 No home channel is set for Linear. "
            "A home channel is where Hermes delivers cron job results "
            "and cross-platform messages.\n\n"
            "Type /sethome to make this chat your home channel, "
            "or ignore to skip."
        )
        result = await self.adapter.send("session-setup", body)
        self.assertTrue(result.success)
        self.assertEqual(
            self.adapter._linear.calls[-1],
            ("session-setup", "thought", body),
        )

    async def test_near_match_home_channel_text_remains_response(self):
        body = "📬 No home channel is set for Linear"
        result = await self.adapter.send("session-near-match", body)
        self.assertTrue(result.success)
        self.assertEqual(
            self.adapter._linear.calls[-1],
            ("session-near-match", "response", body),
        )

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
    async def test_user_by_url_paginates_and_returns_one_exact_live_user(self):
        client = LinearClient("/unused")
        target_url = "https://linear.app/mpolatcan/profiles/doruk"
        calls = []

        async def fake_graphql(query, variables=None):
            self.assertIn("users(first: 50, after: $after)", query)
            calls.append(dict(variables or {}))
            if variables["after"] is None:
                return {"users": {
                    "nodes": [{"id": "actor-1", "url": "https://linear.app/mpolatcan/profiles/derya"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                }}
            return {"users": {
                "nodes": [{"id": "actor-2", "url": target_url}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}

        client.graphql = fake_graphql
        self.assertEqual(
            await client.get_user_by_url(target_url),
            {"id": "actor-2", "url": target_url},
        )
        self.assertEqual(calls, [{"after": None}, {"after": "cursor-1"}])

    async def test_user_by_url_returns_none_for_unresolved_target(self):
        client = LinearClient("/unused")

        async def fake_graphql(_query, variables=None):
            return {"users": {
                "nodes": [{"id": "actor-1", "url": "https://linear.app/mpolatcan/profiles/derya"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}

        client.graphql = fake_graphql
        self.assertIsNone(
            await client.get_user_by_url("https://linear.app/mpolatcan/profiles/nobody")
        )

    async def test_issue_agent_sessions_returns_only_normalized_session_fields(self):
        client = LinearClient("/unused")

        async def fake_graphql(query, variables=None):
            self.assertIn("agentSessions(first: 50, after: $after)", query)
            self.assertIn("appUser { id }", query)
            self.assertEqual(variables, {"id": "OPS-1", "after": None})
            return {
                "issue": {
                    "id": "issue-1",
                    "agentSessions": {
                        "nodes": [{
                            "id": "session-1",
                            "status": "active",
                            "startedAt": "2026-08-05T14:00:00Z",
                            "endedAt": None,
                            "appUser": {"id": "actor-1"},
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }

        client.graphql = fake_graphql
        sessions = await client.get_issue_agent_sessions("OPS-1")
        self.assertEqual(sessions, [{
            "id": "session-1",
            "status": "active",
            "started_at": "2026-08-05T14:00:00Z",
            "ended_at": "",
            "app_user_id": "actor-1",
        }])

    async def test_agent_session_delivery_context_is_authoritative_and_minimal(self):
        client = LinearClient("/unused")

        async def fake_graphql(query, variables=None):
            self.assertIn("agentSession(id: $id)", query)
            self.assertNotIn("issue {", query)
            self.assertNotIn("state {", query)
            self.assertEqual(variables, {"id": "session-1"})
            return {"agentSession": {
                "id": "session-1",
                "status": "active",
                "appUser": {"id": "actor-1"},
                "issue": {
                    "id": "issue-1",
                    "state": {"id": "done-1", "name": "Done", "type": "completed"},
                },
            }}

        client.graphql = fake_graphql

        self.assertEqual(
            await client.get_agent_session_delivery_context("session-1"),
            {
                "id": "session-1",
                "app_user_id": "actor-1",
            },
        )

    async def test_agent_session_delivery_context_rejects_incomplete_readback(self):
        client = LinearClient("/unused")
        client.graphql = mock.AsyncMock(return_value={
            "agentSession": {
                "id": "session-1",
                "status": "active",
                "appUser": None,
            }
        })

        with self.assertRaisesRegex(LinearAPIError, "incomplete"):
            await client.get_agent_session_delivery_context("session-1")

    async def test_issue_agent_sessions_paginates_until_open_session_is_visible(self):
        client = LinearClient("/unused")
        calls = []

        async def fake_graphql(_query, variables=None):
            self.assertIsNotNone(variables)
            variables = variables or {}
            calls.append(variables)
            if variables["after"] is None:
                return {"issue": {"id": "issue-1", "agentSessions": {
                    "nodes": [{
                        "id": "complete-1", "status": "complete",
                        "startedAt": "2026-08-01T00:00:00Z",
                        "endedAt": "2026-08-01T00:01:00Z",
                        "appUser": {"id": "actor-1"},
                    }],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                }}}
            return {"issue": {"id": "issue-1", "agentSessions": {
                "nodes": [{
                    "id": "active-2", "status": "active",
                    "startedAt": "2026-08-05T14:00:00Z", "endedAt": None,
                    "appUser": {"id": "actor-1"},
                }],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}

        client.graphql = fake_graphql
        sessions = await client.get_issue_agent_sessions("issue-1")
        self.assertEqual([item["id"] for item in sessions], ["complete-1", "active-2"])
        self.assertEqual(calls, [{"id": "issue-1", "after": None}, {"id": "issue-1", "after": "cursor-1"}])

    async def test_issue_agent_sessions_rejects_incomplete_policy_data(self):
        client = LinearClient("/unused")
        cases = [
            {"issue": {"id": "issue-1", "agentSessions": None}},
            {"issue": {"id": "issue-1", "agentSessions": {"nodes": [], "pageInfo": None}}},
            {"issue": {"id": "issue-1", "agentSessions": {
                "nodes": [{"id": "session-1", "status": "active", "appUser": None}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}},
            {"issue": {"id": "issue-1", "agentSessions": {
                "nodes": [{"id": 7, "status": "active", "appUser": {"id": "actor-1"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}},
            {"issue": {"id": "issue-1", "agentSessions": {
                "nodes": [{"id": "session-1", "status": "active", "appUser": {"id": ["actor-1"]}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}},
            {"issue": {"id": "issue-1", "agentSessions": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": ["cursor-1"]},
            }}},
            {"issue": {"id": "issue-1", "agentSessions": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": ["terminal-cursor"]},
            }}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                async def fake_graphql(_query, variables=None, payload=payload):
                    return payload
                client.graphql = fake_graphql
                with self.assertRaises(LinearAPIError):
                    await client.get_issue_agent_sessions("OPS-1")

    async def test_issue_agent_sessions_accepts_identifier_resolving_to_uuid(self):
        client = LinearClient("/unused")
        client.graphql = mock.AsyncMock(return_value={
            "issue": {
                "id": "issue-other",
                "agentSessions": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        })

        self.assertEqual(await client.get_issue_agent_sessions("OPS-1"), [])

    async def test_create_activity_places_ephemeral_on_top_level_input(self):
        client = LinearClient("/unused")

        async def fake_graphql(query, variables=None):
            self.assertIn("agentActivityCreate", query)
            self.assertEqual(
                variables,
                {
                    "input": {
                        "id": "activity-1",
                        "agentSessionId": "session-1",
                        "content": {"type": "thought", "body": "Closure is being verified."},
                        "ephemeral": True,
                    }
                },
            )
            return {"agentActivityCreate": {"success": True, "agentActivity": {"id": "activity-1"}}}

        client.graphql = fake_graphql
        result = await client.create_activity(
            "session-1",
            "thought",
            "Closure is being verified.",
            activity_id="activity-1",
            ephemeral=True,
        )
        self.assertEqual(result, "activity-1")

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

    async def test_assign_issue_delegate_uses_official_delegate_id_and_confirms_readback(self):
        client = LinearClient("/unused")

        async def fake_graphql(query, variables=None):
            self.assertIn("issueUpdate", query)
            self.assertIn("delegateId", query)
            self.assertEqual(
                variables, {"id": "issue-1", "delegateId": "agent-derya"}
            )
            return {
                "issueUpdate": {
                    "success": True,
                    "issue": {
                        "id": "issue-1",
                        "delegate": {"id": "agent-derya"},
                    },
                }
            }

        client.graphql = fake_graphql
        self.assertEqual(
            await client.assign_issue_delegate("issue-1", "agent-derya"),
            "issue-1",
        )

    async def test_issue_start_context_requests_official_started_state_order_inputs(self):
        client = LinearClient("/unused")

        async def fake_graphql(query, variables=None):
            self.assertIn('states(filter: { type: { eq: "started" } })', query)
            self.assertIn("position", query)
            self.assertEqual(variables, {"id": "OPS-1"})
            return {
                "issue": {
                    "id": "issue-1",
                    "state": {"id": "todo-1", "name": "Todo", "type": "unstarted"},
                    "delegate": {"id": "actor-1", "name": "Derya"},
                    "team": {
                        "id": "ops-1",
                        "states": {
                            "nodes": [
                                {
                                    "id": "progress-1",
                                    "name": "In Progress",
                                    "type": "started",
                                    "position": 20,
                                }
                            ]
                        },
                    },
                }
            }

        client.graphql = fake_graphql
        context = await client.get_issue_start_context("OPS-1")
        self.assertEqual(context["team"], {"id": "ops-1"})
        self.assertEqual(context["delegate"]["id"], "actor-1")
        self.assertEqual(context["started_states"][0]["position"], 20)

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
