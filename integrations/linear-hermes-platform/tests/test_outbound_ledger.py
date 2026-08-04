from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock
from typing import Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from outbound_ledger import OutboundLedger, OutboundLedgerError  # noqa: E402
from ledger import DeliveryLedger  # noqa: E402


@contextmanager
def sqlite_connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


class OutboundLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "linear.sqlite3"
        self.ledger = OutboundLedger(str(self.path))

    def tearDown(self) -> None:
        self.ledger.close()
        self.tempdir.cleanup()

    def reserve(self, key="op-1", payload=None):
        return self.ledger.reserve(
            operation_key=key,
            tool_name="save_comment",
            payload=payload or {"issueId": "OPS-1", "body": "private body"},
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )

    def test_first_reservation_dispatches_and_same_replay_does_not(self):
        first = self.reserve()
        second = self.reserve()
        self.assertTrue(first.dispatch)
        self.assertFalse(second.dispatch)
        self.assertEqual(second.status, "pending")

    def test_same_key_with_different_payload_fails_closed(self):
        self.reserve()
        with self.assertRaisesRegex(OutboundLedgerError, "different payload"):
            self.reserve(payload={"issueId": "OPS-1", "body": "other"})

    def test_terminal_status_requires_semantic_fields(self):
        self.reserve("semantic-fields")
        with self.assertRaisesRegex(OutboundLedgerError, "result_id"):
            self.ledger.mark_success("semantic-fields", result_id=None)
        with self.assertRaisesRegex(OutboundLedgerError, "error_code"):
            self.ledger.mark_unknown("semantic-fields", error_code="")
        with self.assertRaisesRegex(OutboundLedgerError, "error_code"):
            self.ledger.mark_failed("semantic-fields", error_code="")

    def test_existing_semantically_invalid_row_is_rejected(self):
        self.ledger.close()
        with sqlite_connection(self.path) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                """
                INSERT INTO linear_mcp_operations (
                    operation_key, payload_hash, tool_name, profile_id, actor_id,
                    team_id, status, result_id, error_code, created_at, updated_at
                ) VALUES (?, ?, 'save_issue', 'general', 'actor-1', 'ops-1',
                          'success', NULL, NULL, 0, 0)
                """,
                ("a" * 64, "b" * 64),
            )
        with self.assertRaisesRegex(OutboundLedgerError, "integrity|semantics"):
            OutboundLedger(str(self.path))

    def test_success_and_unknown_are_replayed_without_dispatch(self):
        self.reserve("success-1")
        self.ledger.mark_success("success-1", result_id="comment-1")
        success = self.reserve("success-1")
        self.assertEqual((success.dispatch, success.status, success.result_id), (False, "success", "comment-1"))

        self.reserve("unknown-1")
        self.ledger.mark_unknown("unknown-1", error_code="mcp_http_503")
        unknown = self.reserve("unknown-1")
        self.assertEqual((unknown.dispatch, unknown.status, unknown.error_code), (False, "outcome_unknown", "mcp_http_503"))

        self.reserve("failed-1")
        self.ledger.mark_failed("failed-1", error_code="vendor_rejected")
        failed = self.reserve("failed-1")
        self.assertEqual((failed.dispatch, failed.status, failed.error_code), (False, "failed", "vendor_rejected"))

    def test_rejects_symlink_database_path(self):
        self.ledger.close()
        target = Path(self.tempdir.name) / "target.sqlite3"
        target.write_bytes(b"")
        link = Path(self.tempdir.name) / "linked.sqlite3"
        link.symlink_to(target)
        with self.assertRaisesRegex(OutboundLedgerError, "symlink"):
            OutboundLedger(str(link))

    def test_parent_alias_is_canonicalized_before_database_use(self):
        real_parent = Path(self.tempdir.name) / "real"
        real_parent.mkdir(mode=0o700)
        alias_parent = Path(self.tempdir.name) / "alias"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        ledger = OutboundLedger(str(alias_parent / "ledger.sqlite3"))
        try:
            self.assertEqual(ledger.path, real_parent.resolve() / "ledger.sqlite3")
        finally:
            ledger.close()

    def test_missing_parent_is_not_created(self):
        missing_path = Path(self.tempdir.name) / "missing" / "ledger.sqlite3"
        with self.assertRaisesRegex(OutboundLedgerError, "parent"):
            OutboundLedger(str(missing_path))
        self.assertFalse(missing_path.parent.exists())

    def test_owner_unwritable_parent_is_rejected_before_lock_creation(self):
        parent = Path(self.tempdir.name) / "owner-unwritable"
        parent.mkdir(mode=0o700)
        parent.chmod(0o500)
        try:
            with self.assertRaisesRegex(OutboundLedgerError, "parent"):
                OutboundLedger(str(parent / "ledger.sqlite3"))
            self.assertFalse((parent / ".ledger.sqlite3.lock").exists())
        finally:
            parent.chmod(0o700)

    def test_existing_ledger_requires_exact_0600_mode(self):
        self.ledger.close()
        for mode in (0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaisesRegex(OutboundLedgerError, "private"):
                    OutboundLedger(str(self.path))
                self.path.chmod(0o600)

    def test_opened_parent_descriptor_revalidates_exact_mode(self):
        self.ledger.close()
        parent = self.path.parent
        canonical_parent = parent.resolve()
        real_open = os.open
        raced = False

        def race_parent_mode(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal raced
            if not raced and dir_fd is None and Path(path) == canonical_parent:
                raced = True
                parent.chmod(0o500)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        try:
            with mock.patch("outbound_ledger.os.open", side_effect=race_parent_mode):
                with self.assertRaisesRegex(OutboundLedgerError, "parent changed"):
                    OutboundLedger(str(self.path))
        finally:
            parent.chmod(0o700)

    def test_incompatible_existing_schema_is_rejected(self):
        self.ledger.close()
        self.path.unlink()
        with sqlite_connection(self.path) as connection:
            connection.execute("CREATE TABLE linear_mcp_operations (operation_key TEXT PRIMARY KEY)")
        self.path.chmod(0o600)
        with self.assertRaisesRegex(OutboundLedgerError, "schema"):
            OutboundLedger(str(self.path))

    def test_same_column_names_with_wrong_sqlite_contract_are_rejected(self):
        self.ledger.close()
        self.path.unlink()
        with sqlite_connection(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE linear_mcp_operations (
                    operation_key TEXT,
                    payload_hash INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','success','failed','outcome_unknown')),
                    result_id TEXT,
                    error_code TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
        self.path.chmod(0o600)
        with self.assertRaisesRegex(OutboundLedgerError, "schema"):
            OutboundLedger(str(self.path))

    def test_unexpected_sqlite_indexes_or_triggers_are_rejected(self):
        self.ledger.close()
        with sqlite_connection(self.path) as connection:
            connection.execute(
                "CREATE INDEX injected_index ON linear_mcp_operations(status)"
            )
            connection.execute(
                """
                CREATE TRIGGER injected_trigger AFTER INSERT ON linear_mcp_operations
                BEGIN SELECT 1; END
                """
            )
        with self.assertRaisesRegex(OutboundLedgerError, "schema"):
            OutboundLedger(str(self.path))

    def test_sqlite_never_opens_ledger_by_pathname(self):
        self.ledger.close()
        self.path.unlink()
        with mock.patch("outbound_ledger.sqlite3.connect", wraps=sqlite3.connect) as connect:
            ledger = OutboundLedger(str(self.path))
            try:
                self.assertTrue(connect.call_args_list)
                self.assertTrue(
                    all(call.args[0] == ":memory:" for call in connect.call_args_list)
                )
            finally:
                ledger.close()

    def test_symlink_swap_after_initialization_fails_closed(self):
        target = Path(self.tempdir.name) / "target.sqlite3"
        target.write_bytes(b"target-must-not-change")
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaisesRegex(OutboundLedgerError, "symlink"):
            self.reserve("post-init-swap")
        self.assertEqual(target.read_bytes(), b"target-must-not-change")

    def test_two_processes_serialize_without_lost_records(self):
        self.ledger.close()
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from outbound_ledger import OutboundLedger
ledger = OutboundLedger(sys.argv[2])
try:
    for index in range(20):
        ledger.reserve(
            operation_key=f"{sys.argv[3]}-{index}",
            tool_name="save_comment",
            payload={"issueId": "OPS-1", "body": "template"},
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )
finally:
    ledger.close()
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(PLUGIN_ROOT), str(self.path), prefix],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for prefix in ("alpha", "beta")
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
        with sqlite_connection(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM linear_mcp_operations").fetchone(),
                (40,),
            )
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_long_lived_inbound_wal_and_outbound_process_use_distinct_files(self):
        self.ledger.close()
        inbound_path = Path(self.tempdir.name) / "linear-inbound.sqlite3"
        inbound = DeliveryLedger(str(inbound_path))
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from outbound_ledger import OutboundLedger
ledger = OutboundLedger(sys.argv[2])
try:
    for index in range(20):
        ledger.reserve(
            operation_key=f"outbound-{index}",
            tool_name="save_comment",
            payload={"issueId": "OPS-1", "body": "template"},
            profile_id="general", actor_id="actor-1", team_id="ops-1",
        )
finally:
    ledger.close()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(PLUGIN_ROOT), str(self.path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for index in range(20):
                self.assertTrue(inbound.claim(f"inbound-{index}"))
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            inbound.close()
        self.assertNotEqual(inbound_path.resolve(), self.path.resolve())
        with sqlite_connection(inbound_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM deliveries").fetchone(), (20,))
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        with sqlite_connection(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM linear_mcp_operations").fetchone(),
                (20,),
            )
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_hidden_column_is_rejected(self):
        self.ledger.close()
        self.path.unlink()
        with sqlite_connection(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE linear_mcp_operations (
                    operation_key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','success','failed','outcome_unknown')),
                    result_id TEXT,
                    error_code TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    hidden_value TEXT GENERATED ALWAYS AS ('hidden') VIRTUAL
                )
                """
            )
        self.path.chmod(0o600)
        with self.assertRaisesRegex(OutboundLedgerError, "schema"):
            OutboundLedger(str(self.path))

    def test_database_does_not_store_payload_content(self):
        self.reserve(payload={"issueId": "OPS-1", "body": "do-not-store-this"})
        self.ledger.close()
        raw = self.path.read_bytes()
        self.assertNotIn(b"do-not-store-this", raw)
        with sqlite_connection(self.path) as connection:
            row = connection.execute(
                "SELECT payload_hash, tool_name, profile_id FROM linear_mcp_operations"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[1:], ("save_comment", "general"))
        self.assertEqual(len(row[0]), 64)

    def test_database_does_not_store_raw_operation_key(self):
        sensitive_key = "dose-change-for-person-123"
        self.reserve(key=sensitive_key)
        self.ledger.close()
        self.assertNotIn(sensitive_key.encode(), self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
