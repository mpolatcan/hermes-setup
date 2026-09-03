from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Callable, ClassVar
from unittest import mock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "linear_native_continuation_test_plugin"
spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
assert spec is not None and spec.loader is not None
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE_NAME] = package
spec.loader.exec_module(package)

adapter_mod = __import__(f"{PACKAGE_NAME}.adapter", fromlist=["*"])
client_mod = __import__(f"{PACKAGE_NAME}.linear_client", fromlist=["*"])
ledger_mod = __import__(f"{PACKAGE_NAME}.ledger", fromlist=["*"])

LinearPlatformAdapter = adapter_mod.LinearPlatformAdapter
LinearClient = client_mod.LinearClient
LinearAPIError = client_mod.LinearAPIError
DeliveryLedger = ledger_mod.DeliveryLedger


def source(session_id: str = "linear-session") -> SessionSource:
    return SessionSource(
        platform=Platform.WEBHOOK,
        chat_id=session_id,
        user_id="human-1",
        user_name="Human",
        chat_name="OPS-164",
        chat_type="dm",
    )


def turn_event(*, internal: bool = False, decision_id: str = "") -> MessageEvent:
    event = MessageEvent(
        text="continue canonically" if internal else "work the issue",
        message_type=MessageType.TEXT,
        source=source(),
        message_id=decision_id or "webhook-event",
        internal=internal,
        metadata={
            "linear_agent_session_id": "linear-session",
            "linear_issue_id": "issue-164",
            "linear_delivery_key": "delivery-164",
        },
    )
    if decision_id:
        event.metadata["linear_continuation_decision_id"] = decision_id
    event._gateway_turn_result = MappingProxyType(
        {
            "completed": False,
            "failed": False,
            "interrupted": False,
            "turn_exit_reason": "max_iterations_reached(90)",
            "session_id": "hermes-session",
            "input_tokens": 11,
            "output_tokens": 7,
        }
    )
    return event


class FakeLinear:
    actor_id = "app-user"
    organization_id = "org"

    def __init__(
        self,
        *,
        status: str = "active",
        state_type: str = "started",
        description: str = "## Acceptance\n- [ ] tests pass\n- [ ] restart is safe",
    ) -> None:
        self.status = status
        self.state_type = state_type
        self.description = description

    async def get_agent_turn_context(self, session_id: str) -> dict:
        return {
            "id": session_id,
            "status": self.status,
            "app_user_id": self.actor_id,
            "issue": {
                "id": "issue-164",
                "identifier": "OPS-164",
                "title": "Native continuation",
                "description": self.description,
                "state": {"id": "started", "name": "In Progress", "type": self.state_type},
                "delegate": {"id": self.actor_id},
            },
            "open_blockers": [],
        }


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, separators=(",", ":")).encode()
        self.headers = {
            "Linear-Signature": hmac.new(b"s" * 32, self._body, hashlib.sha256).hexdigest()
        }

    async def read(self) -> bytes:
        return self._body


class FakeGoalManager:
    instances: list["FakeGoalManager"] = []
    existing = False
    decision = {
        "status": "active",
        "should_continue": True,
        "continuation_prompt": "NATIVE CANONICAL CONTINUATION",
        "verdict": "continue",
        "reason": "acceptance evidence missing",
        "message": "continuing",
    }
    before_evaluate: ClassVar[Callable[[], None] | None] = None
    resume_calls = 0
    pause_calls = 0
    existing_status = "active"
    existing_turns = 2
    existing_paused_reason: str | None = None
    waiting = False
    background_snapshots: list[object] = []

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.set_calls = []
        self.state = (
            SimpleNamespace(
                status=self.existing_status, created_at=123.0,
                turns_used=self.existing_turns, max_turns=20,
                paused_reason=self.existing_paused_reason,
            )
            if self.existing
            else None
        )
        self.instances.append(self)

    def has_goal(self) -> bool:
        return self.state is not None

    def is_active(self) -> bool:
        return self.state is not None and self.state.status == "active"

    def set(self, goal: str, *, contract) -> object:
        self.set_calls.append((goal, contract))
        self.state = SimpleNamespace(
            status="active", created_at=123.0, turns_used=0,
            max_turns=20, paused_reason=None,
        )
        return self.state

    def evaluate_after_turn(
        self, response: str, *, user_initiated: bool, background_processes=None
    ) -> dict:
        assert response
        assert user_initiated is True
        type(self).background_snapshots.append(background_processes)
        if type(self).waiting:
            return dict(self.decision)
        callback = type(self).before_evaluate
        if callback is not None:
            callback()
        self.state.turns_used += 1
        self.state.status = str(self.decision.get("status") or "active")
        self.state.paused_reason = (
            str(self.decision.get("reason") or "")
            if self.state.status == "paused" else None
        )
        return dict(self.decision)

    def resume(self, *, reset_budget: bool = True) -> object:
        type(self).resume_calls += 1
        self.state.status = "active"
        self.state.paused_reason = None
        if reset_budget:
            self.state.turns_used = 0
        return self.state

    def pause(self, reason: str = "user-paused") -> object:
        type(self).pause_calls += 1
        self.state.status = "paused"
        self.state.paused_reason = reason
        return self.state

    def next_continuation_prompt(self) -> str:
        return "NATIVE CANONICAL CONTINUATION"

    def is_waiting(self) -> bool:
        return type(self).waiting


class DecisionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "ledger.sqlite3")
        self.ledger = DeliveryLedger(self.path)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_additive_schema_contains_no_prompt_or_summary_columns(self):
        columns = {
            row[1]
            for row in self.ledger._db.execute("PRAGMA table_info(turn_decisions)")
        }
        self.assertEqual(
            columns,
            {
                "decision_id",
                "agent_session_id",
                "issue_id",
                "hermes_session_id",
                "goal_generation",
                "ordinal",
                "outcome",
                "dispatch_state",
                "error",
                "created_at",
                "updated_at",
                "completed_at",
                "source_json",
            },
        )
        self.assertFalse({"prompt", "prompt_json", "summary", "summary_json"} & columns)

    def test_decision_identity_and_dispatch_cas_are_idempotent(self):
        args = ("linear-session", "issue-164", "hermes-session", 123000000, 1, "continue")
        first = self.ledger.reserve_turn_decision(*args, now=10)
        second = self.ledger.reserve_turn_decision(*args, now=11)
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertTrue(self.ledger.transition_turn_decision(first["decision_id"], "pending", "enqueued", now=12))
        self.assertFalse(self.ledger.transition_turn_decision(first["decision_id"], "pending", "enqueued", now=13))

    def test_running_rows_are_not_recovered_and_terminal_rows_are_pruned(self):
        pending = self.ledger.reserve_turn_decision(
            "s1", "i1", "h1", 1, 1, "continue", now=10
        )
        running = self.ledger.reserve_turn_decision(
            "s2", "i2", "h2", 1, 1, "continue", now=10
        )
        terminal = self.ledger.reserve_turn_decision(
            "s3", "i3", "h3", 1, 1, "blocked", now=10
        )
        self.ledger.transition_turn_decision(running["decision_id"], "pending", "enqueued", now=11)
        self.ledger.transition_turn_decision(running["decision_id"], "enqueued", "running", now=12)
        self.ledger.transition_turn_decision(terminal["decision_id"], "pending", "completed", now=12)
        self.assertEqual(
            [row["decision_id"] for row in self.ledger.recoverable_turn_decisions(limit=10)],
            [pending["decision_id"]],
        )
        self.ledger.retention_seconds = 5
        self.assertEqual(self.ledger.prune(now=20), 1)

    def test_recovery_query_returns_only_continue_outcomes(self):
        continuing = self.ledger.reserve_turn_decision(
            "s1", "i1", "h1", 1, 1, "continue", now=10
        )
        self.ledger.reserve_turn_decision(
            "s2", "i2", "h2", 1, 1, "blocked", now=11
        )

        self.assertEqual(
            [row["decision_id"] for row in self.ledger.recoverable_turn_decisions()],
            [continuing["decision_id"]],
        )

    def test_budget_rollover_count_survives_turn_decision_retention(self):
        row = self.ledger.reserve_turn_decision(
            "s1", "i1", "h1", 1000, 20, "continue", now=10
        )
        self.assertTrue(
            self.ledger.claim_budget_rollover(row["decision_id"], "s1", 1000, 2)
        )
        self.assertTrue(
            self.ledger.complete_budget_rollover(row["decision_id"], "s1", 1000)
        )
        self.ledger.transition_turn_decision(
            row["decision_id"], "pending", "completed", now=11
        )
        self.ledger.retention_seconds = 5

        self.ledger.prune(now=20)

        self.assertEqual(self.ledger.count_budget_rollovers("s1", 1000), 1)

    def test_fence_pending_is_same_transactional_boundary_and_duplicate_safe(self):
        row = self.ledger.reserve_turn_decision(
            "linear-session", "issue-164", "h1", 1, 1, "continue", now=10
        )
        self.ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued", now=11)
        self.assertEqual(self.ledger.fence_turn_decisions("linear-session", "stopped", now=12), 1)
        self.assertEqual(self.ledger.fence_turn_decisions("linear-session", "stopped", now=13), 0)
        self.assertEqual(self.ledger.get_turn_decision(row["decision_id"])["dispatch_state"], "fenced")

    def test_authoritative_fence_also_stops_running_decision(self):
        row = self.ledger.reserve_turn_decision(
            "linear-session", "issue-164", "h1", 1, 2, "continue", now=10
        )
        self.ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued", now=11)
        self.ledger.transition_turn_decision(row["decision_id"], "enqueued", "running", now=12)

        self.assertEqual(self.ledger.fence_turn_decisions("linear-session", "stop", now=13), 1)
        fenced = self.ledger.get_turn_decision(row["decision_id"])
        self.assertEqual(fenced["dispatch_state"], "fenced")
        self.assertEqual(fenced["outcome"], "stopped")


class NativeContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeGoalManager.instances.clear()
        FakeGoalManager.existing = False
        FakeGoalManager.before_evaluate = None
        FakeGoalManager.resume_calls = 0
        FakeGoalManager.pause_calls = 0
        FakeGoalManager.existing_status = "active"
        FakeGoalManager.existing_turns = 2
        FakeGoalManager.existing_paused_reason = None
        FakeGoalManager.waiting = False
        FakeGoalManager.background_snapshots = []
        FakeGoalManager.decision = {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "NATIVE CANONICAL CONTINUATION",
            "verdict": "continue",
            "reason": "acceptance evidence missing",
            "message": "continuing",
        }
        self.temp = tempfile.TemporaryDirectory()
        path = str(Path(self.temp.name) / "ledger.sqlite3")
        self.adapter = LinearPlatformAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "database_path": path,
                    "native_goal_continuation_enabled": True,
                },
            ),
            Platform.WEBHOOK,
        )
        self.adapter._ledger = DeliveryLedger(path)
        self.adapter._linear = FakeLinear()
        self.adapter._running = True
        self.admitted: list[MessageEvent] = []

        async def admit(event: MessageEvent) -> None:
            self.admitted.append(event)

        self.adapter.handle_message = admit
        self.adapter.gateway_runner = SimpleNamespace(
            interrupt_session_processing=mock.Mock(return_value=True),
        )
        self.goal_patch = mock.patch.object(adapter_mod, "GoalManager", FakeGoalManager)
        self.goal_patch.start()
        self.drain_patch = mock.patch.object(
            self.adapter, "_drain_outbox_once", new=mock.AsyncMock(return_value=False)
        )
        self.drain_patch.start()

    async def asyncTearDown(self) -> None:
        self.goal_patch.stop()
        self.drain_patch.stop()
        self.adapter._ledger.close()
        self.temp.cleanup()

    def test_continuation_uses_public_platform_callback_only(self):
        source_text = (PLUGIN_DIR / "adapter.py").read_text(encoding="utf-8")
        plugin_text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        combined = source_text + plugin_text
        for forbidden in (
            "_gateway_injection" + "_allowed",
            "_profile_runtime" + "_scope",
            "_resolve_profile_home" + "_for_source",
            "_profile_name" + "_for_source",
            "inject_" + "message(",
            "set_continuation_" + "injector",
            "set_continuation_injection_" + "allowed",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn("await self.handle_message(event)", source_text)

    async def test_supported_hook_result_is_classified_at_adapter_send_boundary(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)
        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-1",
            completed=False,
            failed=False,
            interrupted=False,
            turn_exit_reason="max_iterations_reached(90/90)",
        )

        result = await self.adapter.send("linear-session", "budget summary")

        self.assertTrue(result.success)
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].text, "NATIVE CANONICAL CONTINUATION")
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 0)

    async def test_structured_turn_hook_fences_native_goal_duplicate_owner(self):
        FakeGoalManager.existing = True
        event = turn_event(internal=True)
        await self.adapter.on_processing_start(event)

        self.adapter.record_completed_turn(
            chat_id="linear-session",
            hermes_session_id="hermes-session",
            turn_id="turn-1",
            completed=False,
            failed=False,
            interrupted=False,
            turn_exit_reason="max_iterations_reached(90/90)",
        )

        self.assertEqual(FakeGoalManager.pause_calls, 1)
        self.assertFalse(FakeGoalManager.instances[-1].is_active())

    async def test_missing_supported_hook_result_fails_closed_before_final_response(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)

        result = await self.adapter.send("linear-session", "must not leak as final")

        self.assertFalse(result.success)
        self.assertIn("structured turn result", result.error)
        outbox_count = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox"
        ).fetchone()[0]
        self.assertEqual(outbox_count, 0)

    async def test_disabled_feature_fails_before_goal_or_decision_mutation(self):
        self.adapter._native_goal_continuation_enabled = False
        event = turn_event()

        result = await self.adapter.prepare_turn_delivery(
            event, "ordinary response", event._gateway_turn_result
        )

        self.assertEqual(result, "ordinary response")
        self.assertEqual(FakeGoalManager.instances, [])
        decision_count = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM turn_decisions"
        ).fetchone()[0]
        self.assertEqual(decision_count, 0)

    async def test_callback_failure_is_atomically_fenced_and_visible(self):
        self.adapter.handle_message = mock.AsyncMock(
            side_effect=RuntimeError("callback unavailable")
        )
        event = turn_event()

        await self.adapter.prepare_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "fenced")
        item = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{row['decision_id']}"
        )
        self.assertEqual(item["payload"]["activity_type"], "error")
        self.assertIn("callback unavailable", item["payload"]["body"])

    async def test_callback_return_is_only_scheduling_until_processing_start(self):
        event = turn_event()
        await self.adapter.prepare_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )
        continuation = self.admitted[0]
        row = self.adapter._ledger.get_turn_decision(
            continuation.metadata["linear_continuation_decision_id"]
        )
        self.assertEqual(row["dispatch_state"], "enqueued")

        self.assertTrue(await self.adapter.on_processing_start(continuation))
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "running",
        )

    async def test_max_iteration_summary_becomes_ephemeral_thought_and_native_prompt(self):
        event = turn_event()
        result = await self.adapter.prepare_turn_delivery(
            event, "summary that must not become a response", event._gateway_turn_result
        )
        self.assertIsNone(result)
        manager = FakeGoalManager.instances[-1]
        self.assertEqual(manager.session_id, "hermes-session")
        goal, contract = manager.set_calls[0]
        self.assertEqual(goal, "Complete Linear issue OPS-164 — Native continuation")
        self.assertIn("tests pass", contract.verification)
        self.assertIn("restart is safe", contract.verification)
        self.assertIn("PASS", contract.verification)
        self.assertEqual(len(self.admitted), 1)
        continuation = self.admitted[0]
        self.assertTrue(continuation.internal)
        self.assertEqual(continuation.text, "NATIVE CANONICAL CONTINUATION")
        self.assertEqual(continuation.source, event.source)
        self.assertEqual(continuation.message_id, continuation.metadata["linear_continuation_decision_id"])
        self.assertTrue(continuation.metadata["gateway_session_strict"])
        self.assertEqual(continuation.metadata["gateway_session_id"], "hermes-session")
        self.assertTrue(continuation.metadata["gateway_session_key"])
        self.assertTrue(continuation.metadata["gateway_adapter_manages_continuation"])
        outbox = self.adapter._ledger.get_outbox_item(
            f"activity:turn-summary:{continuation.message_id}"
        )
        self.assertEqual(outbox["payload"]["activity_type"], "thought")
        self.assertTrue(outbox["payload"]["ephemeral"])
        self.assertNotIn("summary", self.adapter._ledger.get_turn_decision(continuation.message_id))

    async def test_core_delivery_hook_routes_structured_turn_result(self):
        event = turn_event()

        result = await self.adapter.prepare_response_for_delivery(
            event, "summary that must not become a response"
        )

        self.assertIsNone(result)
        self.assertEqual(len(self.admitted), 1)

    def test_linear_policy_owned_delivery_disables_response_streaming(self):
        self.assertIs(self.adapter.supports_response_streaming, False)

    async def test_inflight_recovery_requests_followup_pass_for_new_enqueued_decision(self):
        self.adapter.handle_message = mock.AsyncMock(return_value=None)
        self.adapter._turn_recovery_task = __import__("asyncio").current_task()
        event = turn_event()

        result = await self.adapter.prepare_turn_delivery(
            event, "summary", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "enqueued")

    async def test_partial_stream_metadata_cannot_bypass_failed_turn_classification(self):
        event = turn_event()
        event.metadata["streamed"] = True
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "failed": True}
        )

        result = await self.adapter.prepare_response_for_delivery(
            event, "undelivered terminal error"
        )

        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_non_budget_incomplete_turn_fails_closed(self):
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "turn_exit_reason": "unknown_incomplete"}
        )

        result = await self.adapter.prepare_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_goal_pause_blocks_and_emits_visible_error_without_admission(self):
        FakeGoalManager.decision = {
            "status": "paused",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "continue",
            "reason": "budget exhausted",
            "message": "native goal paused",
        }
        event = turn_event()
        result = await self.adapter.prepare_turn_delivery(event, "unfinished", event._gateway_turn_result)
        self.assertIsNone(result)
        self.assertEqual(self.admitted, [])
        rows = self.adapter._ledger.list_turn_decisions("linear-session")
        self.assertEqual(rows[-1]["outcome"], "blocked")
        activity = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{rows[-1]['decision_id']}"
        )
        self.assertEqual(activity["payload"]["activity_type"], "error")

    async def test_budget_pause_rolls_over_same_native_goal_after_fresh_linear_gates(self):
        FakeGoalManager.decision = {
            "status": "paused",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "continue",
            "reason": "turn budget exhausted (20/20)",
            "message": "native goal paused",
        }
        self.adapter._goal_max_budget_rollovers = 2
        context = await FakeLinear().get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=[context, context]
        )
        event = turn_event()

        result = await self.adapter.prepare_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(FakeGoalManager.resume_calls, 1)
        self.assertEqual(self.adapter._linear.get_agent_turn_context.await_count, 2)
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].text, "NATIVE CANONICAL CONTINUATION")
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["budget_rollover"], 1)

    async def test_budget_rollover_cap_fails_closed_without_resuming(self):
        FakeGoalManager.decision = {
            "status": "paused",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "continue",
            "reason": "turn budget exhausted (20/20)",
            "message": "native goal paused",
        }
        self.adapter._goal_max_budget_rollovers = 0
        event = turn_event()

        await self.adapter.prepare_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

        self.assertEqual(FakeGoalManager.resume_calls, 0)
        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_native_process_wait_parks_then_recovers_without_burning_a_turn(self):
        FakeGoalManager.decision = {
            "status": "active",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "wait",
            "reason": "session proc-123",
            "message": "native goal parked",
        }
        FakeGoalManager.waiting = True
        event = turn_event()

        await self.adapter.prepare_turn_delivery(
            event, "waiting for process", event._gateway_turn_result
        )

        self.assertEqual(self.admitted, [])
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "pending")
        self.assertEqual(row["error"], "native_process_wait")
        self.assertTrue(self.adapter._turn_recovery_requested)
        self.assertIsNotNone(FakeGoalManager.background_snapshots[-1])

        FakeGoalManager.existing = True
        FakeGoalManager.existing_turns = 0
        FakeGoalManager.waiting = False
        await self.adapter._recover_turn_decisions()

        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].message_id, row["decision_id"])

    async def test_completed_turn_with_checked_acceptance_allows_true_final_response(self):
        self.adapter._linear = FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        )
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        result = await self.adapter.prepare_turn_delivery(
            event, "final evidence", event._gateway_turn_result
        )
        self.assertIsNone(result)
        decision = self.adapter._ledger.list_turn_decisions("linear-session")[-1]
        self.assertEqual(decision["outcome"], "success")
        self.assertEqual(decision["dispatch_state"], "completed")
        outbox = self.adapter._ledger.get_outbox_item(
            f"activity:turn-success:{decision['decision_id']}"
        )
        self.assertEqual(outbox["payload"]["activity_type"], "response")
        self.assertEqual(outbox["payload"]["body"], "final evidence")
        duplicate = await self.adapter.prepare_turn_delivery(
            event, "final evidence", event._gateway_turn_result
        )
        self.assertIsNone(duplicate)
        self.assertEqual(
            self.adapter._ledger.get_outbox_item(
                f"activity:turn-success:{decision['decision_id']}"
            )["payload"]["body"],
            "final evidence",
        )
        self.assertEqual(FakeGoalManager.instances, [])

    async def test_success_revalidates_all_live_gates_immediately_before_response(self):
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        delegate_removed = {
            **checked,
            "issue": {**checked["issue"], "delegate": {"id": "someone-else"}},
        }
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=[checked, delegate_removed]
        )
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )

        await self.adapter.prepare_turn_delivery(
            event, "must not become response", event._gateway_turn_result
        )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(self.adapter._linear.get_agent_turn_context.await_count, 2)
        self.assertEqual(row["outcome"], "stopped")
        responses = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(responses, 0)

    async def test_delayed_success_outbox_revalidates_before_linear_dispatch(self):
        checked = await FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        ).get_agent_turn_context("linear-session")
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            side_effect=[checked, checked]
        )
        self.adapter._linear.create_activity = mock.AsyncMock(return_value="activity")
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )
        await self.adapter.prepare_turn_delivery(
            event, "accepted evidence", event._gateway_turn_result
        )
        drifted = {
            **checked,
            "issue": {**checked["issue"], "delegate": {"id": "someone-else"}},
        }
        self.adapter._linear.get_agent_turn_context = mock.AsyncMock(
            return_value=drifted
        )

        self.assertTrue(
            await LinearPlatformAdapter._drain_outbox_once(self.adapter)
        )

        self.adapter._linear.create_activity.assert_not_awaited()
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "fenced")
        self.assertNotEqual(row["outcome"], "success")

    async def test_terminal_activity_and_decision_complete_without_second_cas(self):
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "failed": True, "turn_exit_reason": "failed"}
        )
        original = self.adapter._ledger.transition_turn_decision

        def reject_terminal_cas(decision_id, expected, new, **kwargs):
            if new == "completed":
                raise AssertionError("terminal persistence used a second transaction")
            return original(decision_id, expected, new, **kwargs)

        with mock.patch.object(
            self.adapter._ledger,
            "transition_turn_decision",
            side_effect=reject_terminal_cas,
        ):
            await self.adapter.prepare_turn_delivery(
                event, "generic failure text", event._gateway_turn_result
            )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["dispatch_state"], "completed")
        item = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{row['decision_id']}"
        )
        self.assertEqual(item["payload"]["activity_type"], "error")
        self.assertNotEqual(item["payload"]["body"], "generic failure text")

    async def test_unknown_workflow_state_cannot_authorize_success(self):
        self.adapter._linear = FakeLinear(
            state_type="",
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe",
        )
        event = turn_event()
        event._gateway_turn_result = MappingProxyType(
            {**dict(event._gateway_turn_result), "completed": True}
        )

        result = await self.adapter.prepare_turn_delivery(
            event, "must not deliver", event._gateway_turn_result
        )

        self.assertIsNone(result)
        self.assertEqual(
            self.adapter._ledger.list_turn_decisions("linear-session")[-1]["outcome"],
            "blocked",
        )

    async def test_native_done_with_unchecked_acceptance_fails_closed(self):
        FakeGoalManager.decision = {
            "status": "done",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "done",
            "reason": "blocked on human input",
            "message": "done because blocked",
        }
        event = turn_event()

        result = await self.adapter.prepare_turn_delivery(
            event, "blocked", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_native_done_block_reason_never_succeeds_with_checked_acceptance(self):
        FakeGoalManager.decision = {
            "status": "done",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "done",
            "reason": "blocked and needs user input",
            "message": "done because blocked",
        }
        self.adapter._linear = FakeLinear(
            description="## Acceptance\n- [x] tests pass\n- [X] restart is safe"
        )
        event = turn_event()

        result = await self.adapter.prepare_turn_delivery(
            event, "blocked", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "blocked")

    async def test_decision_is_reserved_before_native_goal_evaluation(self):
        event = turn_event()

        def assert_reserved():
            rows = self.adapter._ledger.list_turn_decisions("linear-session")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["dispatch_state"], "pending")

        FakeGoalManager.before_evaluate = assert_reserved

        await self.adapter.prepare_turn_delivery(
            event, "unfinished", event._gateway_turn_result
        )

    async def test_awaiting_input_and_approval_use_elicitation_and_never_continue(self):
        for reason, expected in (("awaiting_input", "awaiting_input"), ("approval", "approval")):
            with self.subTest(reason=reason):
                self.adapter._linear = FakeLinear(status="awaitingInput")
                event = turn_event()
                event.message_id = f"webhook-{reason}"
                event._gateway_turn_result = MappingProxyType(
                    {**dict(event._gateway_turn_result), "turn_exit_reason": reason}
                )
                result = await self.adapter.prepare_turn_delivery(event, "not final", event._gateway_turn_result)
                self.assertIsNone(result)
                row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
                self.assertEqual(row["outcome"], expected)
                item = self.adapter._ledger.get_outbox_item(
                    f"activity:turn-decision:{row['decision_id']}"
                )
                self.assertEqual(item["payload"]["activity_type"], "elicitation")
        self.assertEqual(self.admitted, [])

    async def test_processing_hooks_mark_running_then_completed(self):
        event = turn_event()
        await self.adapter.prepare_turn_delivery(event, "unfinished", event._gateway_turn_result)
        continuation = self.admitted[0]
        decision_id = continuation.metadata["linear_continuation_decision_id"]
        self.assertEqual(self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"], "enqueued")
        await self.adapter.on_processing_start(continuation)
        self.assertEqual(self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"], "running")
        await self.adapter.on_processing_complete(continuation, ProcessingOutcome.SUCCESS)
        self.assertEqual(self.adapter._ledger.get_turn_decision(decision_id)["dispatch_state"], "completed")

    async def test_fenced_continuation_is_rejected_before_handler_execution(self):
        event = turn_event(internal=True, decision_id="fenced-decision")
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        event.metadata["linear_continuation_decision_id"] = row["decision_id"]
        self.adapter._ledger.fence_turn_decisions("linear-session", "stop")

        result = await self.adapter.on_processing_start(event)

        self.assertIs(result, False)

    async def test_restart_recovery_uses_real_admit_hook_and_native_prompt(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 2, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        FakeGoalManager.existing = True
        await self.adapter._recover_turn_decisions()
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].text, "NATIVE CANONICAL CONTINUATION")
        self.assertEqual(self.admitted[0].message_id, row["decision_id"])

    async def test_restart_recovery_preserves_exact_public_session_source(self):
        original = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="linear-session",
            user_id="human-1",
            user_name="Human",
            chat_name="OPS-164",
            chat_type="dm",
            thread_id="thread-9",
            scope_id="org-scope",
            profile="researcher",
        )
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session",
            "issue-164",
            "hermes-session",
            123000000,
            2,
            "continue",
            source=self.adapter._source_snapshot(original),
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        FakeGoalManager.existing = True

        await self.adapter._recover_turn_decisions()

        self.assertEqual(self.admitted[0].source, original)
        self.assertEqual(
            self.admitted[0].metadata["gateway_session_id"], "hermes-session"
        )

    async def test_restart_recovery_completes_reserved_budget_rollover_exactly_once(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 20, "continue"
        )
        self.assertTrue(
            self.adapter._ledger.claim_budget_rollover(
                row["decision_id"], "linear-session", 123000000, 3
            )
        )
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_turns = 20
        FakeGoalManager.existing_paused_reason = "turn budget exhausted (20/20)"

        await self.adapter._recover_turn_decisions()

        self.assertEqual(FakeGoalManager.resume_calls, 1)
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].message_id, row["decision_id"])
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "enqueued",
        )

    async def test_restart_recovery_claims_pause_persisted_before_rollover_marker(self):
        self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 20, "continue"
        )
        FakeGoalManager.existing = True
        FakeGoalManager.existing_status = "paused"
        FakeGoalManager.existing_turns = 20
        FakeGoalManager.existing_paused_reason = "turn budget exhausted (20/20)"

        await self.adapter._recover_turn_decisions()

        self.assertEqual(FakeGoalManager.resume_calls, 1)
        self.assertEqual(
            self.adapter._ledger.count_budget_rollovers("linear-session", 123000000),
            1,
        )
        self.assertEqual(len(self.admitted), 1)

    async def test_recovery_applies_terminal_gate_before_wait_or_rollover_mutation(self):
        wait_row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 1, "continue"
        )
        self.assertTrue(
            self.adapter._ledger.mark_pending_process_wait(wait_row["decision_id"])
        )
        self.adapter._linear = FakeLinear(state_type="completed")
        FakeGoalManager.existing = True
        FakeGoalManager.existing_turns = 1
        FakeGoalManager.waiting = True

        await self.adapter._recover_turn_decisions()

        self.assertEqual(self.admitted, [])
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(wait_row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.assertEqual(FakeGoalManager.resume_calls, 0)
        self.assertEqual(
            self.adapter._ledger.count_budget_rollovers("linear-session", 123000000),
            0,
        )

    def test_background_process_snapshot_is_scoped_to_exact_gateway_session(self):
        running = {"session_id": "proc-1", "status": "running", "pid": 42}
        with mock.patch(
            "tools.process_registry.process_registry.list_sessions",
            return_value=[running],
        ) as list_sessions:
            rows = self.adapter._session_background_processes(source())

        self.assertEqual(rows, [running])
        self.assertIsNone(list_sessions.call_args.kwargs.get("task_id"))
        self.assertTrue(list_sessions.call_args.kwargs["session_key"])

    def test_background_process_session_key_prefers_multiplex_stamped_profile(self):
        stamped = source()
        stamped.profile = "researcher"
        with (
            mock.patch.object(adapter_mod, "build_session_key", return_value="scoped") as build_key,
            mock.patch(
                "tools.process_registry.process_registry.list_sessions",
                return_value=[],
            ),
        ):
            self.adapter._session_background_processes(stamped)

        self.assertEqual(build_key.call_args.kwargs["profile"], "researcher")

    async def test_parked_goal_reuses_current_ordinal_without_consuming_a_turn(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_turns = 7
        FakeGoalManager.waiting = True
        FakeGoalManager.decision = {
            "status": "active",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "wait",
            "reason": "session proc-123",
            "message": "native goal parked",
        }
        event = turn_event()

        await self.adapter.prepare_turn_delivery(
            event, "still waiting", event._gateway_turn_result
        )

        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["ordinal"], 7)
        goal_state = FakeGoalManager.instances[-1].state
        assert goal_state is not None
        self.assertEqual(goal_state.turns_used, 7)
        self.assertEqual(row["error"], "native_process_wait")

    async def test_zero_turn_parked_goal_recovers_without_ordinal_drift(self):
        FakeGoalManager.existing = True
        FakeGoalManager.existing_turns = 0
        FakeGoalManager.waiting = True
        FakeGoalManager.decision = {
            "status": "active",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "wait",
            "reason": "session proc-123",
            "message": "native goal parked",
        }
        event = turn_event()
        await self.adapter.prepare_turn_delivery(
            event, "waiting", event._gateway_turn_result
        )
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["ordinal"], 0)

        FakeGoalManager.waiting = False
        await self.adapter._recover_turn_decisions()

        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.admitted[0].message_id, row["decision_id"])

    async def test_restart_fences_running_decision_after_visible_error(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 123000000, 9, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "enqueued", "running")

        await self.adapter._recover_turn_decisions()

        recovered = self.adapter._ledger.get_turn_decision(row["decision_id"])
        self.assertEqual(recovered["dispatch_state"], "fenced")
        self.assertIn("interrupted by restart", recovered["error"])

    async def test_completed_issue_fences_turn_instead_of_delivering_success(self):
        self.adapter._linear = FakeLinear(state_type="completed")
        event = turn_event()

        result = await self.adapter.prepare_turn_delivery(
            event, "must not be delivered", event._gateway_turn_result
        )

        self.assertIsNone(result)
        row = self.adapter._ledger.get_turn_decision(event._linear_turn_decision_id)
        self.assertEqual(row["outcome"], "stopped")

    async def test_duplicate_prepare_callback_admits_once(self):
        event = turn_event()
        await self.adapter.prepare_turn_delivery(event, "unfinished", event._gateway_turn_result)
        await self.adapter.prepare_turn_delivery(event, "unfinished", event._gateway_turn_result)
        self.assertEqual(len(self.admitted), 1)

    async def test_historical_decision_failure_does_not_degrade_health(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "blocked"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "completed")
        response = await self.adapter._health(None)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text.count('"status": "ok"'), 1)

    async def test_stop_webhook_fences_enqueued_decision_before_dispatch(self):
        self.adapter._signing_secrets = ("s" * 32,)
        self.adapter._data_change_events_enabled = False
        self.adapter._planned_activation_enabled = False
        self.adapter._dependency_wait_enabled = False
        self.adapter._closure_reconciliation_enabled = False
        self.adapter._activation_allowed_team_ids = set()
        self.adapter._planned_owner_ids = set()
        self.adapter.handle_message = mock.AsyncMock()
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.transition_turn_decision(row["decision_id"], "pending", "enqueued")
        payload = {
            "type": "AgentSessionEvent",
            "action": "prompted",
            "webhookId": "webhook-stop-ops164",
            "webhookTimestamp": int(__import__("time").time() * 1000),
            "organizationId": "org",
            "actor": {"id": "human-1", "name": "Human"},
            "agentActivity": {
                "id": "activity-stop-ops164",
                "signal": "stop",
                "body": "stop",
            },
            "agentSession": {
                "id": "linear-session",
                "issue": {
                    "id": "issue-164",
                    "identifier": "OPS-164",
                    "title": "Native continuation",
                },
            },
        }
        response = await self.adapter._handle_webhook(FakeRequest(payload))
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.adapter.handle_message.assert_awaited_once()
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )

    async def test_new_human_prompt_fences_stale_continuation_before_dispatch(self):
        self.adapter._signing_secrets = ("s" * 32,)
        self.adapter._data_change_events_enabled = False
        self.adapter._planned_activation_enabled = False
        self.adapter._dependency_wait_enabled = False
        self.adapter._closure_reconciliation_enabled = False
        self.adapter._activation_allowed_team_ids = set()
        self.adapter._planned_owner_ids = set()
        self.adapter.handle_message = mock.AsyncMock()
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        payload = {
            "type": "AgentSessionEvent",
            "action": "prompted",
            "webhookId": "webhook-human-preempt-ops164",
            "webhookTimestamp": int(__import__("time").time() * 1000),
            "organizationId": "org",
            "actor": {"id": "human-1", "name": "Human"},
            "agentActivity": {
                "id": "activity-human-preempt-ops164",
                "signal": "prompt",
                "body": "new instruction",
            },
            "agentSession": {
                "id": "linear-session",
                "status": "active",
                "issue": {
                    "id": "issue-164",
                    "identifier": "OPS-164",
                    "title": "Native continuation",
                },
            },
        }

        response = await self.adapter._handle_webhook(FakeRequest(payload))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )
        self.adapter.handle_message.assert_awaited_once()

    async def test_cancel_interrupts_runner_before_releasing_adapter_lane(self):
        self.adapter.cancel_session_processing = mock.AsyncMock()

        await self.adapter._cancel_linear_session_processing("linear-session")

        self.adapter.gateway_runner.interrupt_session_processing.assert_called_once()
        session_key, reason = self.adapter.gateway_runner.interrupt_session_processing.call_args.args
        self.assertTrue(session_key)
        self.assertEqual(reason, "linear_authoritative_stop")
        self.adapter.cancel_session_processing.assert_awaited_once_with(session_key)

    async def test_bound_blocker_fences_and_cancels_without_dependency_wait_feature(self):
        self.adapter._dependency_wait_enabled = False
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        self.adapter._linear.get_open_blockers = mock.AsyncMock(
            return_value=[{"id": "blocker-1"}]
        )
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()

        stopped = await self.adapter._stop_bound_turns_if_blocked("issue-164")

        self.assertTrue(stopped)
        self.assertEqual(
            self.adapter._ledger.get_turn_decision(row["decision_id"])["dispatch_state"],
            "fenced",
        )
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )

    async def test_bound_blocker_cancels_initial_turn_without_decision_row(self):
        self.adapter._ledger.bind_issue_session("issue-164", "linear-session")
        self.adapter._linear.get_open_blockers = mock.AsyncMock(
            return_value=[{"id": "blocker-1"}]
        )
        self.adapter._cancel_linear_session_processing = mock.AsyncMock()

        stopped = await self.adapter._stop_bound_turns_if_blocked("issue-164")

        self.assertTrue(stopped)
        self.adapter._cancel_linear_session_processing.assert_awaited_once_with(
            "linear-session"
        )

    async def test_rejected_strict_session_fences_running_decision_with_visible_error(self):
        row = self.adapter._ledger.reserve_turn_decision(
            "linear-session", "issue-164", "hermes-session", 1, 1, "continue"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "pending", "enqueued"
        )
        self.adapter._ledger.transition_turn_decision(
            row["decision_id"], "enqueued", "running"
        )
        event = turn_event(internal=True, decision_id=row["decision_id"])
        event.metadata["gateway_session_rejected"] = "strict_session_mismatch"

        await self.adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

        fenced = self.adapter._ledger.get_turn_decision(row["decision_id"])
        self.assertEqual(fenced["dispatch_state"], "fenced")
        activity = self.adapter._ledger.get_outbox_item(
            f"activity:turn-decision:{row['decision_id']}"
        )
        self.assertEqual(activity["payload"]["activity_type"], "error")

    async def test_failure_completion_keeps_turn_fence_until_generic_error_send(self):
        event = turn_event()
        await self.adapter.on_processing_start(event)

        await self.adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
        self.assertIn("linear-session", self.adapter._active_turn_events)
        result = await self.adapter.send(
            "linear-session", "Hermes encountered an unexpected processing error."
        )

        self.assertFalse(result.success)
        response_rows = self.adapter._ledger._db.execute(
            "SELECT COUNT(*) FROM outbox "
            "WHERE payload_json LIKE '%\"activity_type\":\"response\"%'"
        ).fetchone()[0]
        self.assertEqual(response_rows, 0)


class BlockerPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_blockers_exhausts_pages_and_checks_identity_consistency(self):
        client = LinearClient("/unused")
        calls: list[dict] = []

        async def graphql(_query, variables=None):
            calls.append(dict(variables or {}))
            if variables.get("after") is None:
                return {
                    "issue": {
                        "id": "issue-164",
                        "inverseRelations": {
                            "nodes": [{
                                "type": "blocks",
                                "issue": {"id": "b1", "identifier": "OPS-1", "title": "one", "state": {"name": "Todo", "type": "unstarted"}},
                            }],
                            "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                        },
                    }
                }
            return {
                "issue": {
                    "id": "issue-164",
                    "inverseRelations": {
                        "nodes": [{
                            "type": "blocks",
                            "issue": {"id": "b2", "identifier": "OPS-2", "title": "two", "state": {"name": "Todo", "type": "started"}},
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }

        client.graphql = graphql
        blockers = await client.get_open_blockers("issue-164")
        self.assertEqual([item["id"] for item in blockers], ["b1", "b2"])
        self.assertEqual(calls, [{"id": "issue-164", "after": None}, {"id": "issue-164", "after": "page-2"}])

    async def test_open_blockers_fails_closed_on_identity_change(self):
        client = LinearClient("/unused")
        client.graphql = mock.AsyncMock(side_effect=[
            {"issue": {"id": "issue-164", "inverseRelations": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}},
            {"issue": {"id": "other", "inverseRelations": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ])
        with self.assertRaisesRegex(LinearAPIError, "identity changed"):
            await client.get_open_blockers("issue-164")


if __name__ == "__main__":
    unittest.main()
