from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from linear_tools import execute_with_clients, register_outbound_tools  # noqa: E402
from mcp_client import LinearMCPToolError  # noqa: E402
from oauth_store import LinearAPIError  # noqa: E402
from outbound_ledger import OperationReservation, OutboundLedger  # noqa: E402
from outbound_policy import OutboundPolicy  # noqa: E402


class FakeContext:
    profile_name = "general"

    def __init__(self) -> None:
        self.tools = {}
        self.hooks = {}
        self.events = []

    def register_tool(self, **kwargs):
        self.events.append(("tool", kwargs["name"]))
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, callback):
        self.events.append(("hook", name))
        self.hooks[name] = callback


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.root.chmod(0o700)

    def tearDown(self):
        self.tempdir.cleanup()

    def extra(self, *, enabled=True, mutations=False, allowed_mutation_tools=None):
        if allowed_mutation_tools is None:
            allowed_mutation_tools = ["linear_save_issue", "linear_save_comment"]
        return {
            "oauth_file": str(self.root / "credential"),
            "database_path": str(self.root / "database"),
            "outbound_mcp": {
                "enabled": enabled,
                "mutations_enabled": mutations,
                "allowed_mutation_tools": allowed_mutation_tools,
                "ledger_path": str(self.root / "outbound-linear-mcp.sqlite3"),
                "endpoint": "https://mcp.linear.app/mcp",
                "expected_actor_id": "actor-1",
                "expected_organization_id": "org-1",
                "allowed_team_ids": ["ops-1"],
                "sensitive_mode": "standard",
            },
        }

    def test_disabled_registers_no_tools(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(enabled=False))
        self.assertEqual(ctx.tools, {})

    def test_read_only_registers_only_read_tools(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=False))
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})
        self.assertTrue(all(item["is_async"] for item in ctx.tools.values()))

    def test_registered_read_handler_serializes_result_for_registry_contract(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=False))
        handler = ctx.tools["linear_list_issues"]["handler"]
        graphql = mock.MagicMock()
        graphql.connect = mock.AsyncMock()
        graphql.close = mock.AsyncMock()
        mcp = mock.MagicMock()
        mcp.connect = mock.AsyncMock()
        mcp.close = mock.AsyncMock()
        expected = {"content": [{"type": "text", "text": "[]"}]}
        with (
            mock.patch("linear_tools.LinearOAuthStore"),
            mock.patch("linear_tools.LinearClient", return_value=graphql),
            mock.patch("linear_tools.LinearMCPClient", return_value=mcp),
            mock.patch("linear_tools.execute_with_clients", new=mock.AsyncMock(return_value=expected)),
        ):
            result = asyncio.run(handler({"team": "Operations"}))
        self.assertIsInstance(result, str)
        self.assertEqual(json.loads(result), expected)

    def test_mutation_flag_registers_narrow_four_tool_surface(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        self.assertEqual(
            set(ctx.tools),
            {"linear_get_issue", "linear_list_issues", "linear_save_issue", "linear_save_comment"},
        )
        issue_properties = ctx.tools["linear_save_issue"]["schema"]["parameters"]["properties"]
        self.assertEqual(issue_properties["priority"], {"type": "number"})
        self.assertEqual(
            issue_properties["project"],
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
        )
        self.assertNotIn("state", issue_properties)
        self.assertEqual(
            issue_properties["lifecycle_action"],
            {
                "type": "string",
                "enum": ["start", "complete_child", "cancel_child"],
            },
        )
        self.assertNotIn("approval_reference", issue_properties)
        self.assertNotIn(
            "approval_reference",
            ctx.tools["linear_save_comment"]["schema"]["parameters"]["properties"],
        )
        self.assertEqual(ctx.hooks, {})
        self.assertEqual(ctx.events[0], ("tool", "linear_get_issue"))

    def test_project_completion_capability_is_not_model_exposed(self):
        ctx = FakeContext()
        extra = self.extra(mutations=True)
        extra["outbound_mcp"]["allowed_mutation_tools"] = [
            "linear_complete_project"
        ]
        register_outbound_tools(ctx, extra=extra)
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_mutation_tools_require_an_explicit_per_profile_allowlist(self):
        ctx = FakeContext()
        extra = self.extra(mutations=True)
        del extra["outbound_mcp"]["allowed_mutation_tools"]
        register_outbound_tools(ctx, extra=extra)
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_malformed_or_unknown_mutation_allowlists_fail_closed(self):
        cases = (
            "linear_save_issue",
            ["linear_save_issue", 1],
            ["linear_save_issue", "linear_delete_issue"],
            ["linear_delete_issue"],
            [],
        )
        for allowed_mutation_tools in cases:
            with self.subTest(allowed_mutation_tools=allowed_mutation_tools):
                ctx = FakeContext()
                extra = self.extra(mutations=True)
                extra["outbound_mcp"]["allowed_mutation_tools"] = allowed_mutation_tools
                register_outbound_tools(ctx, extra=extra)
                self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_registration_does_not_add_global_approval_hooks(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        self.assertEqual(ctx.hooks, {})

    def test_string_false_does_not_enable_tools_or_mutations(self):
        ctx = FakeContext()
        extra = self.extra(enabled=True, mutations=False)
        extra["outbound_mcp"]["enabled"] = "false"
        register_outbound_tools(ctx, extra=extra)
        self.assertEqual(ctx.tools, {})

        ctx = FakeContext()
        extra = self.extra(enabled=True, mutations=False)
        extra["outbound_mcp"]["mutations_enabled"] = "false"
        register_outbound_tools(ctx, extra=extra)
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_profile_allowlist_can_expose_comment_without_issue_coordination(self):
        ctx = FakeContext()
        register_outbound_tools(
            ctx,
            extra=self.extra(
                mutations=True,
                allowed_mutation_tools=["linear_save_comment"],
            ),
        )
        self.assertEqual(
            set(ctx.tools),
            {"linear_get_issue", "linear_list_issues", "linear_save_comment"},
        )
        self.assertEqual(ctx.hooks, {})

    def test_tool_name_collision_registers_no_outbound_surface(self):
        ctx = FakeContext()
        with mock.patch("linear_tools._tool_names_available", return_value=False):
            register_outbound_tools(ctx, extra=self.extra(mutations=True))
        self.assertEqual(ctx.tools, {})
        self.assertEqual(ctx.hooks, {})

    def test_non_manual_global_approval_mode_does_not_disable_allowlisted_mutations(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        self.assertEqual(
            set(ctx.tools),
            {"linear_get_issue", "linear_list_issues", "linear_save_issue", "linear_save_comment"},
        )
        self.assertEqual(ctx.hooks, {})

    def test_mutations_require_distinct_outbound_ledger_path(self):
        for ledger_path in ("", "relative-outbound.sqlite3", "/tmp/database"):
            with self.subTest(ledger_path=ledger_path):
                extra = self.extra(mutations=True)
                extra["outbound_mcp"]["ledger_path"] = ledger_path
                ctx = FakeContext()
                register_outbound_tools(ctx, extra=extra)
                self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_symlink_loop_in_ledger_path_preserves_read_only_tools(self):
        loop = self.root / "ledger-loop"
        loop.symlink_to(loop)
        extra = self.extra(mutations=True)
        extra["outbound_mcp"]["ledger_path"] = str(loop)
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=extra)
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_embedded_nul_in_ledger_path_preserves_read_only_tools(self):
        extra = self.extra(mutations=True)
        extra["outbound_mcp"]["ledger_path"] = "/tmp/outbound\x00ledger.sqlite3"
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=extra)
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_mutations_require_absolute_inbound_database_path(self):
        for database_path in ("", "relative-inbound.sqlite3"):
            with self.subTest(database_path=database_path):
                extra = self.extra(mutations=True)
                extra["database_path"] = database_path
                ctx = FakeContext()
                register_outbound_tools(ctx, extra=extra)
                self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_mutations_require_private_usable_ledger_parent(self):
        cases = (
            "missing_parent",
            "public_parent",
            "unwritable_parent",
            "empty_existing_ledger",
            "unreadable_existing_ledger",
            "symlinked_existing_ledger",
        )
        for case in cases:
            with self.subTest(case=case):
                root = self.root / case
                root.mkdir(mode=0o700)
                extra = self.extra(mutations=True)
                ledger = root / "outbound.sqlite3"
                extra["outbound_mcp"]["ledger_path"] = str(ledger)
                if case == "missing_parent":
                    root.rmdir()
                elif case == "public_parent":
                    root.chmod(0o755)
                elif case == "unwritable_parent":
                    root.chmod(0o500)
                elif case == "empty_existing_ledger":
                    ledger.write_bytes(b"")
                    ledger.chmod(0o600)
                elif case == "unreadable_existing_ledger":
                    ledger.write_bytes(b"not-empty")
                    ledger.chmod(0o400)
                else:
                    target = root / "target.sqlite3"
                    target.write_bytes(b"not-empty")
                    target.chmod(0o600)
                    ledger.symlink_to(target)
                ctx = FakeContext()
                register_outbound_tools(ctx, extra=extra)
                self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})

    def test_private_parent_alias_is_canonicalized_for_registration(self):
        real_parent = self.root / "real-parent"
        real_parent.mkdir(mode=0o700)
        alias_parent = self.root / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        extra = self.extra(mutations=True)
        extra["outbound_mcp"]["ledger_path"] = str(alias_parent / "outbound.sqlite3")
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=extra)
        self.assertIn("linear_save_issue", ctx.tools)
        self.assertIn("linear_save_comment", ctx.tools)

    def test_non_official_endpoint_and_sensitive_misconfiguration_register_nothing(self):
        ctx = FakeContext()
        extra = self.extra(enabled=True)
        extra["outbound_mcp"]["endpoint"] = "https://example.invalid/mcp"
        register_outbound_tools(ctx, extra=extra)
        self.assertEqual(ctx.tools, {})

        ctx = FakeContext()
        ctx.profile_name = "health"
        register_outbound_tools(ctx, extra=self.extra(enabled=True))
        self.assertEqual(ctx.tools, {})

    def test_check_fn_rejects_symlinked_credential(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            credential = root / "credential.json"
            credential.write_text("{}", encoding="utf-8")
            credential.chmod(0o600)
            linked = root / "linked.json"
            linked.symlink_to(credential)
            extra = self.extra(enabled=True)
            extra["oauth_file"] = str(linked)
            extra["database_path"] = str(root / "db.sqlite3")
            ctx = FakeContext()
            register_outbound_tools(ctx, extra=extra)
            self.assertFalse(ctx.tools["linear_get_issue"]["check_fn"]())

    def test_ledger_permission_change_after_registration_fails_before_vendor_dispatch(self):
        ledger_parent = self.root / "runtime-change"
        ledger_parent.mkdir(mode=0o700)
        extra = self.extra(mutations=True)
        extra["outbound_mcp"]["ledger_path"] = str(ledger_parent / "outbound.sqlite3")
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=extra)
        handler = ctx.tools["linear_save_comment"]["handler"]
        ledger_parent.chmod(0o500)
        graphql = mock.MagicMock()
        graphql.close = mock.AsyncMock()
        mcp = mock.MagicMock()
        mcp.close = mock.AsyncMock()
        with (
            mock.patch("linear_tools.LinearOAuthStore"),
            mock.patch("linear_tools.LinearClient", return_value=graphql),
            mock.patch("linear_tools.LinearMCPClient", return_value=mcp),
        ):
            result = json.loads(
                asyncio.run(
                    handler(
                        {
                            "operation_key": "runtime-permission-change",
                            "target_team_id": "ops-1",
                            "issueId": "OPS-1",
                            "body": "Canary",
                        }
                    )
                )
            )
        self.assertEqual(
            result,
            {"error": "linear_tool_failed", "reason": "OutboundLedgerError"},
        )
        mcp.call_tool.assert_not_called()


class FakeGraphQL:
    actor_id = "actor-1"
    organization_id = "org-1"

    def __init__(
        self,
        issue_team="ops-1",
        issue_teams=None,
        start_contexts=None,
        child_terminal_contexts=None,
        agent_sessions=None,
        agent_session_reads=None,
        mention_users=None,
    ) -> None:
        self.issue_team = issue_team
        self.issue_teams = issue_teams or {}
        self.start_contexts = list(start_contexts or [])
        self.child_terminal_contexts = list(child_terminal_contexts or [])
        self.agent_sessions = list(agent_sessions or [])
        self.agent_session_reads = list(agent_session_reads or [])
        self.mention_users = dict(mention_users or {})

    async def get_issue_team_id(self, issue_id):
        return self.issue_teams.get(issue_id, self.issue_team)

    async def get_comment_team_id(self, _comment_id):
        return self.issue_team

    async def get_issue_agent_sessions(self, _issue_id):
        if self.agent_session_reads:
            return list(self.agent_session_reads.pop(0))
        return list(self.agent_sessions)

    async def get_user_by_url(self, url):
        return self.mention_users.get(url)

    async def get_issue_start_context(self, _issue_id):
        if not self.start_contexts:
            raise AssertionError("unexpected lifecycle context read")
        if len(self.start_contexts) == 1:
            return self.start_contexts[0]
        return self.start_contexts.pop(0)

    async def get_issue_child_terminal_context(self, _issue_id):
        if not self.child_terminal_contexts:
            raise AssertionError("unexpected child terminal context read")
        if len(self.child_terminal_contexts) == 1:
            return self.child_terminal_contexts[0]
        return self.child_terminal_contexts.pop(0)


class FakeMCP:
    def __init__(self, *, explicit_failure=False) -> None:
        self.calls = []
        self.explicit_failure = explicit_failure

    async def call_tool(self, name, arguments, *, mutation=False):
        self.calls.append((name, arguments, mutation))
        if name == "get_user":
            return {"content": [{"type": "text", "text": '{"id":"actor-1","name":"Derya"}'}]}
        if self.explicit_failure:
            raise LinearMCPToolError("vendor rejected")
        return {"content": [{"type": "text", "text": '{"id":"result-1"}'}]}


class ThreadRecordingLedger:
    def __init__(self) -> None:
        self.thread_ids = []

    def reserve(self, **_kwargs):
        self.thread_ids.append(threading.get_ident())
        return OperationReservation(True, "pending")

    def mark_success(self, _operation_key, *, result_id=None):
        self.thread_ids.append(threading.get_ident())


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger = OutboundLedger(str(Path(self.tempdir.name) / "db.sqlite3"))
        self.policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
        )

    async def asyncTearDown(self):
        self.ledger.close()
        self.tempdir.cleanup()

    @staticmethod
    def child_terminal_context() -> dict:
        return {
            "team": {"id": "ops-1"},
            "state": {"id": "progress-1", "type": "started"},
            "creator": {"id": "actor-1"},
            "delegate": {"id": "actor-1"},
            "parent": {
                "id": "parent-1",
                "state": {"id": "parent-progress", "type": "started"},
                "assignee": {"id": "human-1"},
            },
            "terminal_states": [
                {"id": "done-1", "type": "completed", "position": 40},
                {"id": "canceled-1", "type": "canceled", "position": 50},
            ],
            "open_blockers": [],
        }

    async def run_child_terminal_action(
        self,
        *,
        context: dict,
        action: str = "complete_child",
        operation_key: str,
        agent_sessions: list[dict] | None = None,
    ) -> tuple[dict, FakeMCP]:
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-106",
                "target_team_id": "ops-1",
                "operation_key": operation_key,
                "lifecycle_action": action,
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(
                child_terminal_contexts=[context],
                agent_sessions=agent_sessions,
            ),
            mcp_client=mcp,
        )
        return result, mcp

    async def test_policy_denial_never_dispatches_vendor_mutation(self):
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "operation_key": "op-1",
                "target_team_id": "other-team",
                "title": "Task",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        self.assertEqual(result, {"error": "linear_policy_denied", "reason": "team_not_allowed"})
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_mutation_classification_mismatch_fails_before_identity_or_dispatch(self):
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "operation_key": "op-mutation-classification-mismatch",
                "target_team_id": "ops-1",
                "title": "Task",
            },
            mutation=False,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {
                "error": "linear_policy_denied",
                "reason": "mutation_classification_mismatch",
            },
        )
        self.assertEqual(mcp.calls, [])

    async def test_successful_operation_key_replay_does_not_redispatch(self):
        mcp = FakeMCP()
        arguments = {
            "operation_key": "op-2",
            "target_team_id": "ops-1",
            "issueId": "OPS-1",
            "body": "Metadata status update",
        }
        first = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments=arguments,
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        second = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments=arguments,
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        self.assertEqual(first["status"], "success")
        self.assertEqual(second, {"status": "success", "replayed": True, "result_id": "result-1"})
        mutation_calls = [call for call in mcp.calls if call[0] == "save_comment"]
        self.assertEqual(len(mutation_calls), 1)
        forwarded = mutation_calls[0][1]
        self.assertNotIn("operation_key", forwarded)
        self.assertNotIn("target_team_id", forwarded)

    async def test_save_issue_forwards_explicit_project_null(self):
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "operation_key": "clear-project-null",
                "target_team_id": "ops-1",
                "team": "ops-1",
                "title": "Task",
                "project": None,
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        self.assertEqual(result["status"], "success")
        forwarded = next(call[1] for call in mcp.calls if call[0] == "save_issue")
        self.assertIn("project", forwarded)
        self.assertIsNone(forwarded["project"])

    async def test_open_same_actor_session_denies_checkpoint_comment_before_reservation(self):
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments={
                "operation_key": "op-session-checkpoint",
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": "Progress update",
                "comment_purpose": "checkpoint",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(agent_sessions=[{
                "id": "session-1", "status": "active", "app_user_id": "actor-1",
            }]),
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "session_activity_required"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_open_same_actor_session_allows_explicit_handoff_comment(self):
        target_url = "https://linear.app/mpolatcan/profiles/doruk"
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments={
                "operation_key": "op-session-handoff",
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": f"{target_url} please verify the market evidence.",
                "comment_purpose": "handoff",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(
                agent_sessions=[{
                    "id": "session-1", "status": "awaitingInput", "app_user_id": "actor-1",
                }],
                mention_users={target_url: {"id": "actor-2", "url": target_url}},
            ),
            mcp_client=mcp,
        )
        self.assertEqual(result["status"], "success")
        forwarded = [call for call in mcp.calls if call[0] == "save_comment"][0][1]
        self.assertNotIn("comment_purpose", forwarded)

    async def test_unresolved_handoff_target_is_denied_before_reservation(self):
        target_url = "https://linear.app/mpolatcan/profiles/nobody"
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments={
                "operation_key": "op-unresolved-handoff",
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": f"{target_url} please verify this.",
                "comment_purpose": "handoff",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(mention_users={}),
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "mention_target_unresolved"},
        )
        reservation = self.ledger.reserve(
            operation_key="op-unresolved-handoff",
            tool_name="save_comment",
            payload={"issueId": "OPS-1", "body": "unused"},
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )
        self.assertTrue(reservation.dispatch)
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_self_handoff_target_is_denied_before_reservation(self):
        target_url = "https://linear.app/mpolatcan/profiles/derya"
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments={
                "operation_key": "op-self-handoff",
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": f"{target_url} please verify this.",
                "comment_purpose": "handoff",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(
                mention_users={target_url: {"id": "actor-1", "url": target_url}}
            ),
            mcp_client=FakeMCP(),
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "mention_target_self"},
        )

    async def test_checkpoint_rechecks_session_after_reservation_before_dispatch(self):
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments={
                "operation_key": "op-session-race",
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": "Progress update",
                "comment_purpose": "checkpoint",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(agent_session_reads=[[], [{
                "id": "session-late", "status": "active", "app_user_id": "actor-1",
            }]]),
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "session_activity_required"},
        )
        replay = self.ledger.reserve(
            operation_key="op-session-race",
            tool_name="save_comment",
            payload={"issueId": "OPS-1", "body": "Progress update"},
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )
        self.assertFalse(replay.dispatch)
        self.assertEqual(replay.status, "failed")
        self.assertEqual(replay.error_code, "session_activity_required")
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_indeterminate_agent_sessions_fail_before_comment_mutation(self):
        class IndeterminateGraphQL(FakeGraphQL):
            async def get_issue_agent_sessions(self, _issue_id):
                raise LinearAPIError("Agent Session policy data incomplete")

        mcp = FakeMCP()
        with self.assertRaises(LinearAPIError):
            await execute_with_clients(
                profile_id="general",
                vendor_tool="save_comment",
                arguments={
                    "operation_key": "op-indeterminate-session",
                    "target_team_id": "ops-1",
                    "issueId": "OPS-1",
                    "body": "Checkpoint",
                },
                mutation=True,
                policy=self.policy,
                ledger=self.ledger,
                graphql_client=IndeterminateGraphQL(),
                mcp_client=mcp,
            )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_requires_creator_owned_child_and_reads_back(self):
        before = {
            "team": {"id": "ops-1"},
            "state": {"id": "progress-1", "type": "started"},
            "creator": {"id": "actor-1"},
            "delegate": {"id": "actor-1"},
            "parent": {
                "id": "parent-1",
                "state": {"id": "progress-parent", "type": "started"},
                "assignee": {"id": "human-1"},
            },
            "terminal_states": [
                {"id": "done-1", "type": "completed", "position": 40},
                {"id": "canceled-1", "type": "canceled", "position": 50},
            ],
            "open_blockers": [],
        }
        after = {**before, "state": {"id": "done-1", "type": "completed"}}
        graph = FakeGraphQL(
            child_terminal_contexts=[before, before, after],
            agent_sessions=[{
                "id": "session-1", "status": "complete", "app_user_id": "actor-1",
            }],
        )
        mcp = FakeMCP()

        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-106",
                "target_team_id": "ops-1",
                "operation_key": "op-complete-child",
                "lifecycle_action": "complete_child",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=graph,
            mcp_client=mcp,
        )

        self.assertEqual(result["status"], "success")
        calls = [call for call in mcp.calls if call[0] == "save_issue"]
        self.assertEqual(calls, [("save_issue", {"id": "OPS-106", "state": "done-1"}, True)])

    async def test_complete_child_denies_non_creator_before_reservation(self):
        context = {
            "team": {"id": "ops-1"},
            "state": {"id": "progress-1", "type": "started"},
            "creator": {"id": "other-agent"},
            "delegate": {"id": "actor-1"},
            "parent": {
                "id": "parent-1",
                "state": {"id": "parent-progress", "type": "started"},
                "assignee": {"id": "human-1"},
            },
            "terminal_states": [{"id": "done-1", "type": "completed", "position": 40}],
            "open_blockers": [],
        }
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-106",
                "target_team_id": "ops-1",
                "operation_key": "op-complete-non-creator",
                "lifecycle_action": "complete_child",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(child_terminal_contexts=[context]),
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "child_creator_mismatch"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_delegate_mismatch(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "other-agent"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-delegate-mismatch",
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "delegate_mismatch"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_issue_without_parent(self):
        context = self.child_terminal_context()
        context["parent"] = {}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-parent-required",
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "child_parent_required"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_requires_distinct_human_parent_assignee(self):
        for assignee_id in ("", "actor-1"):
            with self.subTest(assignee_id=assignee_id):
                context = self.child_terminal_context()
                context["parent"]["assignee"] = {"id": assignee_id}
                result, mcp = await self.run_child_terminal_action(
                    context=context,
                    operation_key=f"op-complete-human-parent-{assignee_id or 'missing'}",
                )
                self.assertEqual(
                    result,
                    {"error": "linear_policy_denied", "reason": "human_parent_required"},
                )
                self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_terminal_parent(self):
        context = self.child_terminal_context()
        context["parent"]["state"] = {"id": "parent-done", "type": "completed"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-parent-terminal",
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "parent_terminal"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_open_creator_session(self):
        context = self.child_terminal_context()
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-open-session",
            agent_sessions=[{
                "id": "session-1",
                "status": "active",
                "app_user_id": "actor-1",
            }],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "child_session_still_open"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_open_blocker(self):
        context = self.child_terminal_context()
        context["open_blockers"] = [{"id": "blocker-1"}]
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-open-blocker",
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "child_has_open_blockers"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_cancel_child_selects_canceled_state_and_allows_open_blocker(self):
        before = self.child_terminal_context()
        before["open_blockers"] = [{"id": "blocker-1"}]
        after = {**before, "state": {"id": "canceled-1", "type": "canceled"}}
        graph = FakeGraphQL(child_terminal_contexts=[before, before, after])
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-114",
                "target_team_id": "ops-1",
                "operation_key": "op-cancel-child",
                "lifecycle_action": "cancel_child",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=graph,
            mcp_client=mcp,
        )
        self.assertEqual(result["status"], "success")
        calls = [call for call in mcp.calls if call[0] == "save_issue"]
        self.assertEqual(
            calls,
            [("save_issue", {"id": "OPS-114", "state": "canceled-1"}, True)],
        )

    async def test_complete_child_is_idempotent_when_already_completed(self):
        context = self.child_terminal_context()
        context["state"] = {"id": "done-1", "type": "completed"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-already-done",
        )
        self.assertEqual(result, {"status": "already_completed", "result_id": "done-1"})
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_incomplete_parent_state(self):
        context = self.child_terminal_context()
        context["parent"]["state"] = {"id": "parent-progress"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-parent-state-incomplete",
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "parent_state_unavailable"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_terminal_noop_is_durably_idempotent_after_reopen(self):
        completed = self.child_terminal_context()
        completed["state"] = {"id": "done-custom", "type": "completed"}
        first, first_mcp = await self.run_child_terminal_action(
            context=completed,
            operation_key="op-complete-noop-reopen",
        )
        reopened = self.child_terminal_context()
        second, second_mcp = await self.run_child_terminal_action(
            context=reopened,
            operation_key="op-complete-noop-reopen",
        )
        self.assertEqual(first, {"status": "already_completed", "result_id": "done-custom"})
        self.assertEqual(
            second,
            {"status": "already_completed", "replayed": True, "result_id": "done-custom"},
        )
        self.assertEqual([call[0] for call in first_mcp.calls], ["get_user"])
        self.assertEqual([call[0] for call in second_mcp.calls], ["get_user"])

    async def test_complete_child_fails_closed_when_delegate_drifts_before_dispatch(self):
        before = self.child_terminal_context()
        drifted = self.child_terminal_context()
        drifted["delegate"] = {"id": "other-agent"}
        graph = FakeGraphQL(child_terminal_contexts=[before, drifted])
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-106",
                "target_team_id": "ops-1",
                "operation_key": "op-complete-drift",
                "lifecycle_action": "complete_child",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=graph,
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "lifecycle_pre_dispatch_changed"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_rechecks_sessions_after_final_context_read(self):
        before = self.child_terminal_context()

        class SessionOpeningGraph(FakeGraphQL):
            async def get_issue_child_terminal_context(inner_self, issue_id):
                context = await super().get_issue_child_terminal_context(issue_id)
                inner_self.context_reads = getattr(inner_self, "context_reads", 0) + 1
                if inner_self.context_reads == 2:
                    inner_self.agent_sessions = [
                        {"app_user_id": "actor-1", "status": "active"}
                    ]
                return context

        graph = SessionOpeningGraph(child_terminal_contexts=[before, before])
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-106",
                "target_team_id": "ops-1",
                "operation_key": "op-complete-session-race",
                "lifecycle_action": "complete_child",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=graph,
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "lifecycle_pre_dispatch_changed"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_reports_unknown_when_terminal_readback_misses(self):
        before = self.child_terminal_context()
        graph = FakeGraphQL(child_terminal_contexts=[before, before, before])
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-106",
                "target_team_id": "ops-1",
                "operation_key": "op-complete-readback-miss",
                "lifecycle_action": "complete_child",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=graph,
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_mutation_outcome_unknown", "reason": "lifecycle_readback_mismatch"},
        )

    async def test_semantic_start_uses_lowest_position_started_state_and_reads_back(self):
        before = {
            "team": {"id": "ops-1"},
            "state": {"id": "todo-1", "type": "unstarted"},
            "delegate": {"id": "actor-1"},
            "started_states": [
                {"id": "review-1", "name": "Review", "type": "started", "position": 30},
                {"id": "progress-1", "name": "In Progress", "type": "started", "position": 20},
            ],
        }
        after = {**before, "state": {"id": "progress-1", "type": "started"}}
        graph = FakeGraphQL(start_contexts=[before, before, after])
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-1",
                "target_team_id": "ops-1",
                "operation_key": "op-start",
                "lifecycle_action": "start",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=graph,
            mcp_client=mcp,
        )
        self.assertEqual(result["status"], "success")
        calls = [call for call in mcp.calls if call[0] == "save_issue"]
        self.assertEqual(calls, [("save_issue", {"id": "OPS-1", "state": "progress-1"}, True)])

    async def test_semantic_start_readback_mismatch_is_outcome_unknown(self):
        before = {
            "team": {"id": "ops-1"},
            "state": {"id": "todo-1", "type": "unstarted"},
            "delegate": {"id": "actor-1"},
            "started_states": [
                {"id": "progress-1", "type": "started", "position": 20}
            ],
        }
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-1",
                "target_team_id": "ops-1",
                "operation_key": "op-readback-mismatch",
                "lifecycle_action": "start",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(start_contexts=[before, before, before]),
            mcp_client=FakeMCP(),
        )
        self.assertEqual(
            result,
            {
                "error": "linear_mutation_outcome_unknown",
                "reason": "lifecycle_readback_mismatch",
            },
        )
        replay = self.ledger.reserve(
            operation_key="op-readback-mismatch",
            tool_name="save_issue",
            payload={"id": "OPS-1", "lifecycle_action": "start"},
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )
        self.assertEqual((replay.dispatch, replay.status), (False, "outcome_unknown"))

    async def test_semantic_start_revalidates_before_vendor_dispatch(self):
        before = {
            "team": {"id": "ops-1"},
            "state": {"id": "todo-1", "type": "unstarted"},
            "delegate": {"id": "actor-1"},
            "started_states": [
                {"id": "progress-1", "type": "started", "position": 20}
            ],
        }
        changed = {**before, "delegate": {"id": "other"}}
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-1",
                "target_team_id": "ops-1",
                "operation_key": "op-pre-dispatch-changed",
                "lifecycle_action": "start",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(start_contexts=[before, changed]),
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {
                "error": "linear_policy_denied",
                "reason": "lifecycle_pre_dispatch_changed",
            },
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_semantic_start_is_noop_when_already_started(self):
        context = {
            "team": {"id": "ops-1"},
            "state": {"id": "progress-1", "type": "started"},
            "delegate": {"id": "actor-1"},
            "started_states": [],
        }
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-1",
                "target_team_id": "ops-1",
                "operation_key": "op-started",
                "lifecycle_action": "start",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(start_contexts=[context]),
            mcp_client=mcp,
        )
        self.assertEqual(result, {"status": "already_started", "result_id": "progress-1"})
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_semantic_start_replays_legacy_state_hashed_ledger_entry(self):
        reservation = self.ledger.reserve(
            operation_key="op-start-legacy",
            tool_name="save_issue",
            payload={"id": "OPS-1", "state": "progress-1"},
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )
        self.assertTrue(reservation.dispatch)
        self.ledger.mark_success("op-start-legacy", result_id="OPS-1")
        context = {
            "team": {"id": "ops-1"},
            "state": {"id": "progress-1", "type": "started"},
            "delegate": {"id": "actor-1"},
            "started_states": [],
        }
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-1",
                "target_team_id": "ops-1",
                "operation_key": "op-start-legacy",
                "lifecycle_action": "start",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(start_contexts=[context]),
            mcp_client=FakeMCP(),
        )
        self.assertEqual(
            result,
            {"status": "success", "replayed": True, "result_id": "OPS-1"},
        )

    async def test_semantic_start_denials_never_dispatch_vendor_mutation(self):
        cases = (
            ("delegate_mismatch", {"id": "other"}, {"id": "todo-1", "type": "unstarted"}, True),
            ("issue_not_startable", {"id": "actor-1"}, {"id": "done-1", "type": "completed"}, True),
            ("source_state_unavailable", {"id": "actor-1"}, {"id": "", "type": "unstarted"}, True),
            ("started_state_unavailable", {"id": "actor-1"}, {"id": "todo-1", "type": "unstarted"}, False),
        )
        for reason, delegate, state, has_started_state in cases:
            with self.subTest(reason=reason):
                context = {
                    "team": {"id": "ops-1"},
                    "state": state,
                    "delegate": delegate,
                    "started_states": (
                        [{"id": "progress-1", "type": "started", "position": 20}]
                        if has_started_state
                        else []
                    ),
                }
                mcp = FakeMCP()
                result = await execute_with_clients(
                    profile_id="general",
                    vendor_tool="save_issue",
                    arguments={
                        "id": "OPS-1",
                        "target_team_id": "ops-1",
                        "operation_key": "op-" + reason,
                        "lifecycle_action": "start",
                    },
                    mutation=True,
                    policy=self.policy,
                    ledger=self.ledger,
                    graphql_client=FakeGraphQL(start_contexts=[context]),
                    mcp_client=mcp,
                )
                self.assertEqual(
                    result,
                    {"error": "linear_policy_denied", "reason": reason},
                )
                self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_claimed_allowed_team_cannot_mutate_cross_team_issue(self):
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments={
                "operation_key": "op-cross-team",
                "target_team_id": "ops-1",
                "issueId": "GAME-1",
                "body": "Metadata status update",
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(issue_team="game-1"),
            mcp_client=mcp,
        )
        self.assertEqual(result, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"})
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_get_issue_cannot_read_cross_team_issue(self):
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="assistant",
            vendor_tool="get_issue",
            arguments={"id": "GAME-1"},
            mutation=False,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(issue_team="game-1"),
            mcp_client=mcp,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_explicit_vendor_failure_is_replayed_as_outcome_unknown(self):
        mcp = FakeMCP(explicit_failure=True)
        arguments = {
            "operation_key": "op-explicit-failure",
            "target_team_id": "ops-1",
            "issueId": "OPS-1",
            "body": "Metadata status update",
        }
        first = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments=arguments,
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        second = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments=arguments,
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        self.assertEqual(first, {"error": "linear_mutation_outcome_unknown", "reason": "vendor_is_error"})
        self.assertEqual(
            second,
            {"status": "outcome_unknown", "replayed": True, "result_id": None, "error_code": "vendor_is_error"},
        )
        self.assertEqual(len([call for call in mcp.calls if call[0] == "save_comment"]), 1)

    async def test_parent_issue_cannot_cross_team_boundary(self):
        mcp = FakeMCP()
        graph = FakeGraphQL(issue_teams={"OPS-1": "ops-1", "OPS-2": "game-1"})
        result = await execute_with_clients(
            profile_id="general",
            policy=self.policy,
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-1",
                "parentId": "OPS-2",
                "target_team_id": "ops-1",
                "operation_key": "op-parent-cross-team",
            },
            mutation=True,
            mcp_client=mcp,
            graphql_client=graph,
            ledger=self.ledger,
        )
        self.assertEqual(result, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"})
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_relation_must_match_target_team_even_when_both_teams_are_allowed(self):
        mcp = FakeMCP()
        graph = FakeGraphQL(issue_teams={"OPS-1": "ops-1", "GAME-1": "game-1"})
        policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1", "game-1"},
        )
        result = await execute_with_clients(
            profile_id="general",
            policy=policy,
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-1",
                "blocks": ["GAME-1"],
                "target_team_id": "ops-1",
                "operation_key": "op-cross-allowed-teams",
            },
            mutation=True,
            mcp_client=mcp,
            graphql_client=graph,
            ledger=self.ledger,
        )
        self.assertEqual(result, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"})

    async def test_ledger_io_runs_off_the_event_loop_thread(self):
        ledger = ThreadRecordingLedger()
        main_thread = threading.get_ident()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_comment",
            arguments={
                "operation_key": "op-threaded-ledger",
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": "Metadata status update",
            },
            mutation=True,
            policy=self.policy,
            ledger=ledger,
            graphql_client=FakeGraphQL(),
            mcp_client=FakeMCP(),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(ledger.thread_ids), 2)
        self.assertTrue(all(thread_id != main_thread for thread_id in ledger.thread_ids))


if __name__ == "__main__":
    unittest.main()
