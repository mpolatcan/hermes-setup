import hashlib
import json
import os
import signal
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from restart_coordinator import (
    Coordinator,
    CoordinatorStore,
    ProcessRuntime,
    RequestError,
    requester_from_ancestry,
    requester_from_home,
)


class CoordinatorStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "queue.sqlite3"
        self.artifact = self.root / "artifact.bin"
        self.artifact.write_bytes(b"release-1")
        self.rollback = self.root / "rollback.bin"
        self.rollback.write_bytes(b"release-0")

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, **overrides):
        payload = {
            "task_id": "OPS-195-a",
            "target_profile": "assistant",
            "artifact_path": str(self.artifact),
            "artifact_sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
            "expected_version": "1.2.3",
            "expected_pid": 111,
            "rollback_path": str(self.rollback),
            "rollback_sha256": hashlib.sha256(self.rollback.read_bytes()).hexdigest(),
            "health_url": "http://127.0.0.1:9999/health",
            "semantic_canary": {"path": "status", "equals": "ok"},
        }
        payload.update(overrides)
        return payload

    def test_only_general_and_coder_can_enqueue(self):
        store = CoordinatorStore(self.db)
        accepted = store.enqueue("general", self.payload())
        self.assertEqual(accepted["status"], "queued")
        with self.assertRaisesRegex(RequestError, "requester_not_allowed"):
            store.enqueue("writer", self.payload(task_id="OPS-195-b"))

    def test_database_is_owner_only_and_integrity_is_ok(self):
        store = CoordinatorStore(self.db)
        store.enqueue("coder", self.payload())
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_rejects_mutable_or_mismatched_artifacts_and_non_loopback_health(self):
        store = CoordinatorStore(self.db)
        cases = (
            self.payload(task_id="bad-hash", artifact_sha256="0" * 64),
            self.payload(task_id="bad-rollback", rollback_sha256="0" * 64),
            self.payload(task_id="bad-health", health_url="https://example.com/health"),
            self.payload(task_id="bad-pid", expected_pid=0),
        )
        for payload in cases:
            with self.subTest(task_id=payload["task_id"]):
                with self.assertRaises(RequestError):
                    store.enqueue("general", payload)

    def test_production_artifact_roots_and_immutable_coordinates_are_enforced(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        store = CoordinatorStore(self.db, allowed_artifact_roots=[allowed])
        with self.assertRaisesRegex(RequestError, "artifact_path_(outside_allowed_roots|contains_symlink)"):
            store.enqueue("general", self.payload())

        artifact = (allowed / "artifact.bin").resolve()
        rollback = (allowed / "rollback.bin").resolve()
        artifact.write_bytes(b"release")
        rollback.write_bytes(b"rollback")
        restricted = self.payload(
            artifact_path=str(artifact),
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            rollback_path=str(rollback),
            rollback_sha256=hashlib.sha256(rollback.read_bytes()).hexdigest(),
        )
        with self.assertRaisesRegex(RequestError, "artifact_not_immutable"):
            store.enqueue("general", restricted)
        artifact.chmod(0o444)
        rollback.chmod(0o444)
        accepted = store.enqueue("general", restricted)
        persisted = store.get(accepted["task_id"])["payload"]
        self.assertEqual(persisted["artifact_path"], str(artifact.resolve()))
        self.assertIn("artifact_identity", persisted)

    def test_duplicates_coalesce_and_newer_artifact_supersedes_queued_request(self):
        store = CoordinatorStore(self.db)
        first = store.enqueue("general", self.payload(task_id="a"))
        duplicate = store.enqueue("coder", self.payload(task_id="b"))
        self.assertEqual(duplicate["status"], "coalesced")
        self.assertEqual(duplicate["leader_id"], first["id"])
        store.transition("a", "succeeded", {"new_pid": 222})
        self.assertEqual(store.get("b")["status"], "succeeded")

        self.artifact.write_bytes(b"release-2")
        older = self.payload(
            task_id="old-queued",
            artifact_sha256=hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
            expected_version="1.2.4",
        )
        store.enqueue("general", older)
        self.artifact.write_bytes(b"release-3")
        newer = self.payload(
            task_id="c",
            artifact_sha256=hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
            expected_version="1.2.5",
        )
        self.assertEqual(store.enqueue("general", newer)["status"], "queued")
        self.assertEqual(store.get("old-queued")["status"], "superseded")
        self.assertEqual(store.get("a")["status"], "succeeded")

    def test_supersede_never_strands_coalesced_followers_or_dependencies(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload(task_id="leader"))
        store.enqueue("coder", self.payload(task_id="follower"))
        self.artifact.write_bytes(b"release-new")
        newer = self.payload(
            task_id="newer",
            artifact_sha256=hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
            expected_version="2.0.0",
        )
        store.enqueue("general", newer)
        self.assertEqual(store.get("leader")["status"], "queued")
        self.assertEqual(store.get("follower")["status"], "coalesced")

    def test_dependency_blocks_until_parent_succeeds(self):
        store = CoordinatorStore(self.db)
        with self.assertRaisesRegex(RequestError, "dependency_not_found"):
            store.enqueue("coder", self.payload(task_id="orphan", dependency_task_id="missing"))
        store.enqueue("general", self.payload(task_id="parent"))
        child = self.payload(
            task_id="child",
            target_profile="assistant",
            dependency_task_id="parent",
            expected_pid="dependency_new_pid",
        )
        store.enqueue("coder", child)
        self.assertEqual(store.next_ready()["task_id"], "parent")
        store.transition("parent", "succeeded", {"new_pid": 222})
        self.assertEqual(store.next_ready()["task_id"], "child")
        self.assertEqual(store.resolve_expected_pid("child"), 222)

    def test_dependency_failure_terminalizes_child_and_cross_profile_pid_inheritance_is_denied(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload(task_id="parent"))
        with self.assertRaisesRegex(RequestError, "dependency_pid_cross_profile"):
            store.enqueue(
                "coder",
                self.payload(
                    task_id="cross-profile",
                    target_profile="coder",
                    dependency_task_id="parent",
                    expected_pid="dependency_new_pid",
                ),
            )
        store.enqueue(
            "coder",
            self.payload(task_id="child", dependency_task_id="parent", expected_pid="dependency_new_pid"),
        )
        store.transition("parent", "operator_required", {"reason": "canary_failed"})
        settled = store.settle_failed_dependencies()
        self.assertEqual(settled, 1)
        self.assertEqual(store.get("child")["status"], "operator_required")

    def test_coalesce_requires_full_execution_contract_match(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload(task_id="a"))
        changed = self.payload(task_id="b", semantic_canary={"path": "platforms.telegram", "equals": "connected"})
        self.assertEqual(store.enqueue("coder", changed)["status"], "queued")

    def test_atomic_claim_prevents_later_supersede_of_selected_request(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload(task_id="a"))
        claimed = store.claim_next()
        self.assertEqual(claimed["status"], "preflight")
        self.artifact.write_bytes(b"release-2")
        newer = self.payload(task_id="b", artifact_sha256=hashlib.sha256(self.artifact.read_bytes()).hexdigest())
        store.enqueue("general", newer)
        self.assertEqual(store.get("a")["status"], "preflight")

    def test_outbox_requires_ack_after_publish(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload())
        store.transition("OPS-195-a", "succeeded", {"new_pid": 222})
        event = store.pending_outbox()[0]
        self.assertEqual(store.outbox_counts()["pending"], 1)
        store.ack_outbox(event["id"])
        self.assertEqual(store.outbox_counts(), {"pending": 0, "delivered": 1, "dead": 0})


class FakeRuntime:
    def __init__(self):
        self.pids = {"assistant": 111}
        self.restarts = []
        self.valid = True
        self.managed_ok = True
        self.health_failures = 0
        self.health_payload = {"version": "1.2.3", "status": "ok", "platforms": {"telegram": "connected"}}

    def pid(self, profile):
        return self.pids[profile]

    def validate(self, profile):
        return self.valid

    def restart(self, profile):
        self.restarts.append(profile)
        self.pids[profile] = 222

    def managed(self, pid, profile):
        return self.managed_ok

    def health(self, url):
        if self.health_failures:
            self.health_failures -= 1
            raise OSError("listener not ready")
        return self.health_payload


class CoordinatorExecutionTests(CoordinatorStoreTests):
    def test_processes_one_restart_with_pid_version_canary_and_ledger_evidence(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload())
        runtime = FakeRuntime()
        result = Coordinator(store, runtime).process_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(runtime.restarts, ["assistant"])
        self.assertEqual(result["old_pid"], 111)
        self.assertEqual(result["new_pid"], 222)
        self.assertEqual(store.integrity(), "ok")
        self.assertGreaterEqual(len(store.ledger("OPS-195-a")), 4)
        self.assertEqual(store.outbox_counts(), {"pending": 1, "delivered": 0, "dead": 0})

    def test_readiness_retries_transient_listener_startup_before_acceptance(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload())
        runtime = FakeRuntime()
        runtime.health_failures = 2
        result = Coordinator(store, runtime, readiness_attempts=3, readiness_delay=0).process_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(runtime.health_failures, 0)

    def test_late_equivalent_request_is_already_satisfied_without_second_restart(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload(task_id="first"))
        runtime = FakeRuntime()
        first = Coordinator(store, runtime).process_once()
        self.assertEqual(first["status"], "succeeded")
        store.enqueue("coder", self.payload(task_id="late", expected_pid=222))
        late = Coordinator(store, runtime).process_once()
        self.assertEqual(late["status"], "succeeded")
        self.assertEqual(late["disposition"], "already_satisfied")
        self.assertEqual(runtime.restarts, ["assistant"])
        self.assertEqual(late["old_pid"], 222)
        self.assertEqual(late["new_pid"], 222)
        store.enqueue("general", self.payload(task_id="late-stale-pid", expected_pid=111))
        stale = Coordinator(store, runtime).process_once()
        self.assertEqual(stale["disposition"], "already_satisfied")
        self.assertEqual(runtime.restarts, ["assistant"])

    def test_failed_canary_never_blind_retries_and_stops_operator_required(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload())
        runtime = FakeRuntime()
        runtime.health_payload["status"] = "degraded"
        first = Coordinator(store, runtime, readiness_attempts=1, readiness_delay=0).process_once()
        second = Coordinator(store, runtime, readiness_attempts=1, readiness_delay=0).process_once()
        self.assertEqual(first["status"], "operator_required")
        self.assertIsNone(second)
        self.assertEqual(runtime.restarts, ["assistant"])

    def test_crash_recovery_observes_changed_pid_without_second_restart(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload())
        store.transition("OPS-195-a", "restarting", {"old_pid": 111})
        runtime = FakeRuntime()
        runtime.pids["assistant"] = 222
        result = Coordinator(store, runtime).recover_inflight()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(runtime.restarts, [])

    def test_crash_recovery_with_unchanged_pid_fails_closed(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload())
        store.transition("OPS-195-a", "restarting", {"old_pid": 111})
        runtime = FakeRuntime()
        result = Coordinator(store, runtime).recover_inflight()
        self.assertEqual(result["status"], "operator_required")
        self.assertEqual(runtime.restarts, [])

    def test_missing_artifact_in_preflight_becomes_operator_required_without_loop(self):
        store = CoordinatorStore(self.db)
        store.enqueue("general", self.payload())
        self.artifact.unlink()
        runtime = FakeRuntime()
        result = Coordinator(store, runtime).process_once()
        self.assertEqual(result["status"], "operator_required")
        self.assertEqual(result["reason"], "preflight_exception")
        self.assertIsNone(Coordinator(store, runtime).process_once())
        self.assertEqual(runtime.restarts, [])


class ProcessRuntimeTests(unittest.TestCase):
    def test_requester_identity_is_derived_from_profile_home(self):
        self.assertEqual(requester_from_home("/Users/u/.hermes/profiles/general"), "general")
        self.assertEqual(requester_from_home("/Users/u/.hermes/profiles/coder/"), "coder")
        with self.assertRaisesRegex(RequestError, "requester_not_allowed"):
            requester_from_home("/Users/u/.hermes/profiles/writer")

    def test_requester_identity_requires_matching_gateway_ancestor(self):
        gateway_pids = {"general": 100, "coder": 200, "writer": 300}
        self.assertEqual(requester_from_ancestry([999, 100, 1], gateway_pids), "general")
        with self.assertRaisesRegex(RequestError, "requester_not_allowed"):
            requester_from_ancestry([999, 300, 1], gateway_pids)
        with self.assertRaisesRegex(RequestError, "gateway_ancestor_not_found"):
            requester_from_ancestry([999, 1], gateway_pids)

    def test_parses_launchd_pid_without_using_cumulative_exit_state(self):
        output = "state = running\n\tpid = 4321\n\tlast exit code = 1\n"
        self.assertEqual(ProcessRuntime.parse_launchd_pid(output), 4321)
        with self.assertRaises(RuntimeError):
            ProcessRuntime.parse_launchd_pid("state = waiting\n")

    def test_restart_requests_graceful_sigusr1_and_never_kickstarts(self):
        runtime = ProcessRuntime(restart_timeout=5)
        runtime.pid = mock.Mock(side_effect=[111, 111, 222, 222])
        with mock.patch("restart_coordinator.os.kill") as kill, mock.patch("restart_coordinator.time.sleep"):
            runtime.restart("assistant")
        kill.assert_called_once_with(111, signal.SIGUSR1)

    def test_restart_timeout_is_bounded_without_second_signal(self):
        runtime = ProcessRuntime(restart_timeout=2)
        runtime.pid = mock.Mock(return_value=111)
        clock = iter([0.0, 0.5, 1.5, 2.1])
        with mock.patch("restart_coordinator.os.kill") as kill, mock.patch(
            "restart_coordinator.time.monotonic", side_effect=lambda: next(clock)
        ), mock.patch("restart_coordinator.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "graceful_restart_readiness_timeout"):
                runtime.restart("assistant")
        kill.assert_called_once_with(111, signal.SIGUSR1)


if __name__ == "__main__":
    unittest.main()
