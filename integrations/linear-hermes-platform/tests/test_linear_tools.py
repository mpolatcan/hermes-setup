from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from linear_tools import (  # noqa: E402
    _canonicalize_vendor_markdown,
    _parse_plan_sections,
    execute_with_clients,
    register_outbound_tools,
)
from mcp_client import MCPOutcomeUnknown, LinearMCPToolError  # noqa: E402
from oauth_store import LinearAPIError  # noqa: E402
from outbound_ledger import (  # noqa: E402
    FleetGlobalLock,
    OperationReservation,
    OutboundLedger,
)
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
                "quota_admission_lock_path": str(
                    Path.home() / ".hermes" / "state" / "locks" / "linear-quota-admission.lock"
                ),
                "quota_team_id": "ops-1",
                "quota_team_key": "OPS",
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
                "enum": ["start", "complete_child", "cancel_child", "enrich_plan"],
            },
        )
        self.assertEqual(issue_properties["expected_updated_at"]["type"], "string")
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

    def test_runtime_create_handler_passes_configured_fleet_lock(self):
        extra = self.extra(mutations=True)
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=extra)
        handler = ctx.tools["linear_save_issue"]["handler"]
        graphql = mock.MagicMock(actor_id="actor-1", organization_id="org-1")
        graphql.connect = mock.AsyncMock()
        graphql.close = mock.AsyncMock()
        mcp = mock.MagicMock()
        mcp.connect = mock.AsyncMock()
        mcp.close = mock.AsyncMock()
        fleet_lock = mock.MagicMock()
        expected = {"status": "success"}
        with (
            mock.patch("linear_tools.LinearOAuthStore"),
            mock.patch("linear_tools.LinearClient", return_value=graphql),
            mock.patch("linear_tools.LinearMCPClient", return_value=mcp),
            mock.patch("linear_tools.FleetGlobalLock", return_value=fleet_lock) as lock_cls,
            mock.patch(
                "linear_tools.execute_with_clients",
                new=mock.AsyncMock(return_value=expected),
            ) as execute,
        ):
            result = json.loads(
                asyncio.run(
                    handler(
                        {
                            "operation_key": "runtime-create-lock",
                            "target_team_id": "ops-1",
                            "team": "ops-1",
                            "title": "Task",
                        }
                    )
                )
            )
        self.assertEqual(result, expected)
        lock_cls.assert_called_once_with(extra["outbound_mcp"]["quota_admission_lock_path"])
        self.assertIs(execute.await_args.kwargs["quota_admission_lock"], fleet_lock)
        self.assertEqual(execute.await_args.kwargs["quota_team_id"], "ops-1")
        self.assertEqual(execute.await_args.kwargs["quota_team_key"], "OPS")


class FakeGraphQL:
    actor_id = "actor-1"
    organization_id = "org-1"

    def __init__(
        self,
        issue_team="ops-1",
        issue_teams=None,
        start_contexts=None,
        child_terminal_contexts=None,
        plan_contexts=None,
        agent_sessions=None,
        agent_session_reads=None,
        mention_users=None,
    ) -> None:
        self.issue_team = issue_team
        self.issue_teams = issue_teams or {}
        self.start_contexts = list(start_contexts or [])
        self.child_terminal_contexts = list(child_terminal_contexts or [])
        self.plan_contexts = list(plan_contexts or [])
        self.agent_sessions = list(agent_sessions or [])
        self.agent_session_reads = list(agent_session_reads or [])
        self.last_agent_sessions = list(self.agent_sessions)
        self.mention_users = dict(mention_users or {})
        self.quota_reads = 0

    async def graphql(self, _query, variables):
        self.quota_reads += 1
        return {
            "team": {
                "id": variables["teamId"],
                "key": "OPS",
                "issues": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }

    async def get_issue_team_id(self, issue_id):
        return self.issue_teams.get(issue_id, self.issue_team)

    async def get_comment_team_id(self, _comment_id):
        return self.issue_team

    async def get_issue_agent_sessions(self, _issue_id):
        if self.agent_session_reads:
            result = list(self.agent_session_reads.pop(0))
        else:
            result = list(self.agent_sessions)
        self.last_agent_sessions = result
        return result

    async def get_agent_session_terminal_response_count(self, session_id):
        for session in self.last_agent_sessions:
            if str(session.get("id") or "") == session_id:
                return int(session.get("terminal_response_count") or 0)
        raise LinearAPIError("response evidence unavailable")

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

    async def get_issue_plan_context(self, _issue_id):
        if not self.plan_contexts:
            raise AssertionError("unexpected plan context read")
        if len(self.plan_contexts) == 1:
            return self.plan_contexts[0]
        return self.plan_contexts.pop(0)

    async def create_activity(self, agent_session_id, activity_type, body, *, activity_id, ephemeral=False):
        self.created_activities = getattr(self, "created_activities", [])
        self.created_activities.append(
            {
                "agent_session_id": agent_session_id,
                "activity_type": activity_type,
                "body": body,
                "activity_id": activity_id,
                "ephemeral": ephemeral,
            }
        )
        return activity_id


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
        root = Path(self.tempdir.name)
        self.ledger = OutboundLedger(str(root / "db.sqlite3"))
        locks_root = root / "locks"
        locks_root.mkdir(mode=0o700)
        lock_path = locks_root / "linear-quota-admission.lock"
        lock_path.touch(mode=0o600)
        self.quota_admission_lock = FleetGlobalLock(
            str(lock_path), canonical_locks_root=locks_root
        )
        self.policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
        )

    async def asyncTearDown(self):
        self.ledger.close()
        self.tempdir.cleanup()

    async def run_create(
        self,
        *,
        operation_key: str,
        current_count: int | Exception,
        ledger: OutboundLedger | None = None,
        profile_id: str = "general",
        target_team_id: str = "ops-1",
        quota_team_id: str = "ops-1",
        quota_team_key: str = "OPS",
        mcp: FakeMCP | None = None,
    ) -> tuple[dict, FakeMCP, mock.AsyncMock]:
        mcp = mcp or FakeMCP()
        counter = mock.AsyncMock(
            side_effect=current_count
            if isinstance(current_count, Exception)
            else None,
            return_value=None
            if isinstance(current_count, Exception)
            else current_count,
        )
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            result = await execute_with_clients(
                profile_id=profile_id,
                vendor_tool="save_issue",
                arguments={
                    "operation_key": operation_key,
                    "target_team_id": target_team_id,
                    "team": target_team_id,
                    "title": "Task",
                },
                mutation=True,
                policy=self.policy,
                ledger=ledger or self.ledger,
                quota_admission_lock=self.quota_admission_lock,
                quota_team_id=quota_team_id,
                quota_team_key=quota_team_key,
                graphql_client=FakeGraphQL(),
                mcp_client=mcp,
            )
        return result, mcp, counter

    async def test_matching_non_ops_quota_team_is_counted_without_hard_coding(self):
        policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"eng-1"},
        )
        counter = mock.AsyncMock(return_value=12)
        mcp = FakeMCP()
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            result = await execute_with_clients(
                profile_id="coder",
                vendor_tool="save_issue",
                arguments={
                    "operation_key": "eng-create",
                    "target_team_id": "eng-1",
                    "team": "eng-1",
                    "title": "Task",
                },
                mutation=True,
                policy=policy,
                ledger=self.ledger,
                quota_admission_lock=self.quota_admission_lock,
                quota_team_id="eng-1",
                quota_team_key="ENG",
                graphql_client=FakeGraphQL(issue_team="eng-1"),
                mcp_client=mcp,
            )
        self.assertEqual(result["status"], "success")
        counter.assert_awaited_once_with(mock.ANY, "eng-1", "ENG")

    async def test_create_target_team_mismatch_is_denied_before_lock_count_or_vendor(self):
        counter = mock.AsyncMock(side_effect=AssertionError("quota count called"))
        mcp = FakeMCP()
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            result = await execute_with_clients(
                profile_id="general",
                vendor_tool="save_issue",
                arguments={
                    "operation_key": "wrong-quota-team",
                    "target_team_id": "other-1",
                    "team": "other-1",
                    "title": "Task",
                },
                mutation=True,
                policy=self.policy,
                ledger=self.ledger,
                quota_admission_lock=self.quota_admission_lock,
                quota_team_id="ops-1",
                quota_team_key="OPS",
                graphql_client=FakeGraphQL(issue_team="other-1"),
                mcp_client=mcp,
            )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "team_not_allowed"},
        )
        counter.assert_not_awaited()
        self.assertEqual(mcp.calls, [])

    async def test_missing_or_unsafe_quota_team_config_fails_create_closed(self):
        for team_id, team_key in (("", "OPS"), ("ops-1", ""), ("ops-1", "OPS\nBAD")):
            with self.subTest(team_id=team_id, team_key=team_key):
                result, mcp, counter = await self.run_create(
                    operation_key=f"bad-config-{len(team_id)}-{len(team_key)}",
                    current_count=AssertionError("quota count called"),
                    quota_team_id=team_id,
                    quota_team_key=team_key,
                )
                self.assertEqual(
                    result,
                    {
                        "error": "linear_policy_denied",
                        "reason": "quota_team_config_invalid",
                    },
                )
                counter.assert_not_awaited()
                self.assertEqual(mcp.calls, [])

    async def test_create_without_quota_admission_lock_fails_before_count_or_mutation(self):
        mcp = FakeMCP()
        counter = mock.AsyncMock(side_effect=AssertionError("quota count called"))
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            result = await execute_with_clients(
                profile_id="general",
                vendor_tool="save_issue",
                arguments={
                    "operation_key": "missing-admission-lock",
                    "target_team_id": "ops-1",
                    "team": "ops-1",
                    "title": "Task",
                },
                mutation=True,
                policy=self.policy,
                ledger=self.ledger,
                quota_admission_lock=None,
                quota_team_id="ops-1",
                quota_team_key="OPS",
                graphql_client=FakeGraphQL(),
                mcp_client=mcp,
            )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "quota_admission_lock_unavailable"},
        )
        counter.assert_not_awaited()
        self.assertEqual(mcp.calls, [])

    async def test_create_with_unsafe_quota_admission_lock_fails_before_count_or_mutation(self):
        self.quota_admission_lock.path.chmod(0o644)
        mcp = FakeMCP()
        counter = mock.AsyncMock(side_effect=AssertionError("quota count called"))
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            result = await execute_with_clients(
                profile_id="general",
                vendor_tool="save_issue",
                arguments={
                    "operation_key": "unsafe-admission-lock",
                    "target_team_id": "ops-1",
                    "team": "ops-1",
                    "title": "Task",
                },
                mutation=True,
                policy=self.policy,
                ledger=self.ledger,
                quota_admission_lock=self.quota_admission_lock,
                quota_team_id="ops-1",
                quota_team_key="OPS",
                graphql_client=FakeGraphQL(),
                mcp_client=mcp,
            )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "quota_admission_lock_unavailable"},
        )
        counter.assert_not_awaited()
        self.assertEqual(mcp.calls, [])

    async def test_cancelled_create_waiter_does_not_orphan_fleet_lock(self):
        held_fd = self.quota_admission_lock.acquire()
        assert held_fd is not None
        counter = mock.AsyncMock(side_effect=AssertionError("count ran before lock"))
        mcp = FakeMCP()
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            waiter = asyncio.create_task(
                execute_with_clients(
                    profile_id="general",
                    vendor_tool="save_issue",
                    arguments={
                        "operation_key": "cancelled-lock-waiter",
                        "target_team_id": "ops-1",
                        "team": "ops-1",
                        "title": "Task",
                    },
                    mutation=True,
                    policy=self.policy,
                    ledger=self.ledger,
                    quota_admission_lock=self.quota_admission_lock,
                    quota_team_id="ops-1",
                    quota_team_key="OPS",
                    graphql_client=FakeGraphQL(),
                    mcp_client=mcp,
                )
            )
            await asyncio.sleep(0.06)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        self.quota_admission_lock.release(held_fd)

        recovered_fd = self.quota_admission_lock.acquire(blocking=False)
        assert recovered_fd is not None
        self.quota_admission_lock.release(recovered_fd)
        counter.assert_not_awaited()
        self.assertEqual(mcp.calls, [])
        self.assertEqual(
            self.quota_admission_lock.inspect()["unresolved_create_fences"],
            [],
        )

    async def test_create_quota_admission_boundaries(self):
        cases = (
            (238, 239, False),
            (239, 240, True),
            (248, 249, True),
        )
        for current, projected, immediate in cases:
            with self.subTest(current=current):
                result, mcp, counter = await self.run_create(
                    operation_key=f"create-at-{current}",
                    current_count=current,
                )
                self.assertEqual(result["status"], "success")
                counter.assert_awaited_once_with(mock.ANY, "ops-1", "OPS")
                self.assertEqual(
                    [call[0] for call in mcp.calls],
                    ["get_user", "save_issue"],
                )
                if immediate:
                    self.assertEqual(
                        result["quota_admission"],
                        {
                            "severity": "critical",
                            "current_count": current,
                            "projected_count": projected,
                            "capacity": 250,
                            "buffer_after": 250 - projected,
                        },
                    )
                    self.assertIs(result["immediate_retention_required"], True)
                else:
                    self.assertNotIn("quota_admission", result)
                    self.assertNotIn("immediate_retention_required", result)

    async def test_create_quota_capacity_denials_precede_reservation_and_dispatch(self):
        for current in (249, 250):
            with self.subTest(current=current):
                operation_key = f"create-denied-at-{current}"
                result, mcp, counter = await self.run_create(
                    operation_key=operation_key,
                    current_count=current,
                )
                self.assertEqual(
                    result,
                    {
                        "error": "linear_policy_denied",
                        "reason": "quota_capacity_reserved_or_exhausted",
                        "quota_admission": {
                            "severity": "critical",
                            "current_count": current,
                            "projected_count": current + 1,
                            "capacity": 250,
                            "buffer_after": 250 - (current + 1),
                        },
                    },
                )
                counter.assert_awaited_once()
                self.assertEqual([call[0] for call in mcp.calls], ["get_user"])
                self.assertIsNone(
                    self.ledger.lookup(
                        operation_key=operation_key,
                        tool_name="save_issue",
                        payload={"team": "ops-1", "title": "Task"},
                        profile_id="general",
                        actor_id="actor-1",
                        team_id="ops-1",
                    )
                )

    async def test_create_quota_count_drift_or_api_error_fails_before_mutation(self):
        for failure in (
            LinearAPIError("Operations issue inventory changed during revalidation"),
            LinearAPIError("Linear API unavailable"),
        ):
            with self.subTest(failure=str(failure)):
                operation_key = "create-count-failed-" + str(len(str(failure)))
                result, mcp, _counter = await self.run_create(
                    operation_key=operation_key,
                    current_count=failure,
                )
                self.assertEqual(
                    result,
                    {
                        "error": "linear_policy_denied",
                        "reason": "quota_count_unavailable",
                    },
                )
                self.assertEqual([call[0] for call in mcp.calls], ["get_user"])
                self.assertIsNone(
                    self.ledger.lookup(
                        operation_key=operation_key,
                        tool_name="save_issue",
                        payload={"team": "ops-1", "title": "Task"},
                        profile_id="general",
                        actor_id="actor-1",
                        team_id="ops-1",
                    )
                )

    async def test_create_replay_bypasses_fresh_quota_count_and_preserves_signal(self):
        first, first_mcp, first_counter = await self.run_create(
            operation_key="create-critical-replay",
            current_count=239,
        )
        replay, replay_mcp, replay_counter = await self.run_create(
            operation_key="create-critical-replay",
            current_count=LinearAPIError("fresh count must not run"),
        )

        self.assertEqual(first["status"], "success")
        first_counter.assert_awaited_once()
        self.assertEqual(replay["status"], "success")
        self.assertIs(replay["replayed"], True)
        self.assertEqual(replay["result_id"], first["result_id"])
        self.assertEqual(replay["quota_admission"], first["quota_admission"])
        self.assertIs(replay["immediate_retention_required"], True)
        replay_counter.assert_not_awaited()
        self.assertEqual([call[0] for call in first_mcp.calls], ["get_user", "save_issue"])
        self.assertEqual([call[0] for call in replay_mcp.calls], ["get_user"])

    async def test_two_profile_ledgers_serialize_248_plus_two_creates_without_overshoot(self):
        root = Path(self.tempdir.name)
        second_ledger = OutboundLedger(str(root / "second-profile.sqlite3"))
        shared = {"count": 248}

        class CountingMCP(FakeMCP):
            async def call_tool(inner_self, name, arguments, *, mutation=False):
                result = await super().call_tool(name, arguments, mutation=mutation)
                if name == "save_issue":
                    shared["count"] += 1
                return result

        async def counter(*_args):
            await asyncio.sleep(0.01)
            return shared["count"]

        async def create(profile_id, operation_key, ledger):
            return await execute_with_clients(
                profile_id=profile_id,
                vendor_tool="save_issue",
                arguments={
                    "operation_key": operation_key,
                    "target_team_id": "ops-1",
                    "team": "ops-1",
                    "title": operation_key,
                },
                mutation=True,
                policy=self.policy,
                ledger=ledger,
                quota_admission_lock=self.quota_admission_lock,
                quota_team_id="ops-1",
                quota_team_key="OPS",
                graphql_client=FakeGraphQL(),
                mcp_client=CountingMCP(),
            )

        try:
            with mock.patch("linear_tools.count_operations_issues", new=counter):
                results = await asyncio.gather(
                    create("general", "fleet-create-general", self.ledger),
                    create("coder", "fleet-create-coder", second_ledger),
                )
        finally:
            second_ledger.close()

        self.assertEqual(shared["count"], 249)
        self.assertEqual(sum(result.get("status") == "success" for result in results), 1)
        self.assertEqual(
            sum(result.get("reason") == "quota_capacity_reserved_or_exhausted" for result in results),
            1,
        )

    async def test_create_pending_and_unknown_replays_bypass_fresh_quota_count(self):
        arguments = {
            "operation_key": "create-state-replay",
            "target_team_id": "ops-1",
            "team": "ops-1",
            "title": "Task",
        }
        payload = {"team": "ops-1", "title": "Task"}
        for status in ("pending", "outcome_unknown"):
            with self.subTest(status=status):
                operation_key = f"create-{status}-replay"
                self.ledger.reserve(
                    operation_key=operation_key,
                    tool_name="save_issue",
                    payload=payload,
                    profile_id="general",
                    actor_id="actor-1",
                    team_id="ops-1",
                )
                if status == "outcome_unknown":
                    self.ledger.mark_unknown(operation_key, error_code="mcp_outcome_unknown")
                counter = mock.AsyncMock(side_effect=AssertionError("fresh count called"))
                mcp = FakeMCP()
                with mock.patch("linear_tools.count_operations_issues", new=counter):
                    result = await execute_with_clients(
                        profile_id="general",
                        vendor_tool="save_issue",
                        arguments={**arguments, "operation_key": operation_key},
                        mutation=True,
                        policy=self.policy,
                        ledger=self.ledger,
                        quota_admission_lock=self.quota_admission_lock,
                        quota_team_id="ops-1",
                        quota_team_key="OPS",
                        graphql_client=FakeGraphQL(),
                        mcp_client=mcp,
                    )
                self.assertEqual(result["status"], status)
                self.assertIs(result["replayed"], True)
                counter.assert_not_awaited()
                self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_ambiguous_create_writes_global_fence_and_blocks_distinct_profile(self):
        class AmbiguousMCP(FakeMCP):
            async def call_tool(inner_self, name, arguments, *, mutation=False):
                if name == "save_issue":
                    inner_self.calls.append((name, arguments, mutation))
                    raise MCPOutcomeUnknown("session lost after dispatch")
                return await super().call_tool(name, arguments, mutation=mutation)

        first, _mcp, first_counter = await self.run_create(
            operation_key="ambiguous-create-secret",
            current_count=23,
            mcp=AmbiguousMCP(),
        )
        self.assertEqual(first["error"], "linear_outcome_unknown")
        first_counter.assert_awaited_once()
        state = self.quota_admission_lock.inspect()
        self.assertEqual(len(state["unresolved_create_fences"]), 1)
        fence = state["unresolved_create_fences"][0]
        self.assertEqual(fence["observed_current_count"], 23)
        self.assertEqual(fence["profile_id"], "general")
        self.assertNotIn(
            "ambiguous-create-secret",
            self.quota_admission_lock.state_path.read_text(),
        )

        second_ledger = OutboundLedger(
            str(Path(self.tempdir.name) / "second-fenced-profile.sqlite3")
        )
        try:
            second, second_mcp, second_counter = await self.run_create(
                operation_key="distinct-second-profile",
                current_count=0,
                profile_id="coder",
                ledger=second_ledger,
            )
        finally:
            second_ledger.close()
        self.assertEqual(
            second,
            {
                "error": "linear_policy_denied",
                "reason": "quota_create_outcome_unresolved",
            },
        )
        second_counter.assert_not_awaited()
        self.assertEqual([call[0] for call in second_mcp.calls], ["get_user"])

    async def test_same_operation_replay_bypasses_existing_global_fence_and_count(self):
        operation_key = "same-operation-fenced-replay"
        payload = {"team": "ops-1", "title": "Task"}
        self.ledger.reserve(
            operation_key=operation_key,
            tool_name="save_issue",
            payload=payload,
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )
        self.ledger.mark_unknown(operation_key, error_code="mcp_outcome_unknown")
        fd = self.quota_admission_lock.acquire()
        self.quota_admission_lock.add_unresolved_create_fence(
            fd,
            operation_key=operation_key,
            observed_current_count=15,
            profile_id="general",
            timestamp=1_787_000_001,
        )
        self.quota_admission_lock.release(fd)

        result, mcp, counter = await self.run_create(
            operation_key=operation_key,
            current_count=AssertionError("fresh count called"),
        )
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIs(result["replayed"], True)
        counter.assert_not_awaited()
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_cancellation_after_reservation_persists_fence_before_unlock(self):
        dispatched = asyncio.Event()

        class BlockingMCP(FakeMCP):
            async def call_tool(inner_self, name, arguments, *, mutation=False):
                if name == "save_issue":
                    inner_self.calls.append((name, arguments, mutation))
                    dispatched.set()
                    await asyncio.Future()
                return await super().call_tool(name, arguments, mutation=mutation)

        counter = mock.AsyncMock(return_value=31)
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            task = asyncio.create_task(
                execute_with_clients(
                    profile_id="general",
                    vendor_tool="save_issue",
                    arguments={
                        "operation_key": "cancel-after-reservation",
                        "target_team_id": "ops-1",
                        "team": "ops-1",
                        "title": "Task",
                    },
                    mutation=True,
                    policy=self.policy,
                    ledger=self.ledger,
                    quota_admission_lock=self.quota_admission_lock,
                    quota_team_id="ops-1",
                    quota_team_key="OPS",
                    graphql_client=FakeGraphQL(),
                    mcp_client=BlockingMCP(),
                )
            )
            await asyncio.wait_for(dispatched.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        fences = self.quota_admission_lock.inspect()["unresolved_create_fences"]
        self.assertEqual(len(fences), 1)
        self.assertEqual(fences[0]["observed_current_count"], 31)
        self.assertEqual(fences[0]["profile_id"], "general")

    async def test_save_issue_update_is_unaffected_by_create_quota_gate(self):
        mcp = FakeMCP()
        counter = mock.AsyncMock(side_effect=AssertionError("create counter called"))
        with mock.patch("linear_tools.count_operations_issues", new=counter):
            result = await execute_with_clients(
                profile_id="general",
                vendor_tool="save_issue",
                arguments={
                    "operation_key": "update-unaffected",
                    "target_team_id": "ops-1",
                    "id": "OPS-1",
                    "priority": 2,
                },
                mutation=True,
                policy=self.policy,
                ledger=self.ledger,
                graphql_client=FakeGraphQL(),
                mcp_client=mcp,
            )
        self.assertEqual(result["status"], "success")
        counter.assert_not_awaited()
        self.assertEqual([call[0] for call in mcp.calls], ["get_user", "save_issue"])

    @staticmethod
    def plan_context(*, updated_at="2026-08-09T18:00:00.000Z", description="Short brief"):
        return {
            "id": "issue-1",
            "title": "Short brief",
            "updatedAt": updated_at,
            "description": description,
            "team": {"id": "ops-1"},
            "state": {"id": "todo-1", "type": "unstarted"},
            "assignee": {"id": "human-1", "app": False},
            "delegate": {"id": "actor-1"},
        }

    @staticmethod
    def child_terminal_context() -> dict:
        return {
            "team": {"id": "ops-1"},
            "state": {"id": "progress-1", "type": "started"},
            "creator": {"id": "actor-1"},
            "delegate": {"id": "actor-1"},
            "project": {"id": "project-1"},
            "parent": {
                "id": "parent-1",
                "state": {"id": "parent-progress", "type": "started"},
                "assignee": {"id": "human-1", "app": False},
                "project": {"id": "project-1"},
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
        profile_id: str = "general",
        action: str = "complete_child",
        operation_key: str,
        agent_sessions: list[dict] | None = None,
        agent_session_reads: list[list[dict]] | None = None,
        after_context: dict | None = None,
    ) -> tuple[dict, FakeMCP]:
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id=profile_id,
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
                child_terminal_contexts=(
                    [context, context, after_context]
                    if after_context is not None
                    else [context]
                ),
                agent_sessions=agent_sessions,
                agent_session_reads=agent_session_reads,
            ),
            mcp_client=mcp,
        )
        return result, mcp

    @staticmethod
    def plan_description() -> str:
        return """## Amaç
Kısa brief'i doğrulanabilir bir şirket sonucuna dönüştürmek ve task sahibinin yürütme planını görünür kılmak. Kaynak brief: Short brief

## Kapsam
Issue bağlamını araştırmak, işi fazlara ayırmak, gerekli uzman child'ları ve gerçek bağımlılıkları Linear'da kurmak.

## Kapsam dışı
Onaysız credential kapsamı değişikliği, harcama, yayın ve geri döndürülemez production işlemleri kapsam dışıdır.

## Uygulama planı
1. Canlı bağlamı ve mevcut artefaktları oku.
2. Child/dependency modelini kur.
3. İşi test-first yürüt ve kanıtları issue'ya bağla.

## Bağımlılıklar ve alt işler
Şimdilik açık blocker yok. Araştırma bağımsız teslim gerektirirse child issue açılıp doğru uzmana delegate edilecek.

## Kabul kriterleri
- [ ] Plan issue description'ında authoritative read-back ile görünür.
- [ ] Teslim test ve canlı canary kanıtı taşır.

## Doğrulama ve teslim kanıtı
Test çıktıları, vendor read-back, commit/manifest ve gerekiyorsa kanonik Notion bağlantısı Linear issue üzerinde bulunur.

## Riskler ve geri dönüş
Stale human edit körlemesine ezilmez. Drift durumunda mutation fail-closed olur; deployment atomik rollback ile geri alınır."""

    async def run_plan_action(
        self,
        *,
        operation_key: str,
        contexts: list[dict],
        description: str | None = None,
    ) -> tuple[dict, FakeMCP]:
        mcp = FakeMCP()
        result = await execute_with_clients(
            profile_id="general",
            vendor_tool="save_issue",
            arguments={
                "id": "OPS-105",
                "target_team_id": "ops-1",
                "operation_key": operation_key,
                "lifecycle_action": "enrich_plan",
                "expected_updated_at": "2026-08-09T18:00:00.000Z",
                "description": description or self.plan_description(),
            },
            mutation=True,
            policy=self.policy,
            ledger=self.ledger,
            graphql_client=FakeGraphQL(plan_contexts=contexts),
            mcp_client=mcp,
        )
        return result, mcp

    async def test_enrich_plan_updates_same_issue_with_conflict_guard(self):
        before = self.plan_context()
        after = self.plan_context(
            updated_at="2026-08-09T18:01:00.000Z",
            description=self.plan_description(),
        )
        result, mcp = await self.run_plan_action(
            operation_key="enrich-plan-positive",
            contexts=[before, before, after],
        )
        self.assertEqual(result["status"], "success")
        save_calls = [call for call in mcp.calls if call[0] == "save_issue"]
        self.assertEqual(
            save_calls,
            [("save_issue", {"id": "OPS-105", "description": self.plan_description()}, True)],
        )
        replay, replay_mcp = await self.run_plan_action(
            operation_key="enrich-plan-positive",
            contexts=[after],
        )
        self.assertEqual(replay["status"], "success")
        self.assertTrue(replay["replayed"])
        self.assertEqual([call[0] for call in replay_mcp.calls], ["get_user"])

    async def test_enrich_plan_accepts_vendor_normalized_unordered_markers(self):
        before = self.plan_context()
        vendor_description = "\n".join(
            f"* {line[2:]}" if line.startswith("- ") else line
            for line in self.plan_description().splitlines()
        )
        after = self.plan_context(
            updated_at="2026-08-09T18:01:00.000Z",
            description=vendor_description,
        )
        result, mcp = await self.run_plan_action(
            operation_key="enrich-plan-vendor-bullet-normalization",
            contexts=[before, before, after],
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            len([call for call in mcp.calls if call[0] == "save_issue"]),
            1,
        )

    def test_vendor_markdown_canonicalization_is_limited_to_real_bullet_items(self):
        self.assertEqual(
            _canonicalize_vendor_markdown("- item\n  - nested\n"),
            _canonicalize_vendor_markdown("* item\n  + nested\n"),
        )
        self.assertNotEqual(
            _canonicalize_vendor_markdown("- - -\n"),
            _canonicalize_vendor_markdown("* - -\n"),
        )
        self.assertNotEqual(
            _canonicalize_vendor_markdown("    - code\n"),
            _canonicalize_vendor_markdown("    * code\n"),
        )
        self.assertNotEqual(
            _canonicalize_vendor_markdown("- item\r\n"),
            _canonicalize_vendor_markdown("* item\n"),
        )
        for separator in ("\v", "\f", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                self.assertNotEqual(
                    _canonicalize_vendor_markdown(
                        f"{separator}- protected paragraph bytes\n- actual item\n"
                    ),
                    _canonicalize_vendor_markdown(
                        f"{separator}+ protected paragraph bytes\n- actual item\n"
                    ),
                )

    async def test_enrich_plan_post_dispatch_drift_reports_unknown(self):
        before = self.plan_context()
        for drift in ("description", "owner", "state"):
            with self.subTest(drift=drift):
                after = self.plan_context(
                    updated_at="2026-08-09T18:01:00.000Z",
                    description=self.plan_description(),
                )
                if drift == "description":
                    after["description"] = "Concurrent human edit"
                elif drift == "owner":
                    after["assignee"] = {"id": "other-agent", "app": True}
                else:
                    after["state"] = {"id": "done-1", "type": "completed"}
                result, mcp = await self.run_plan_action(
                    operation_key=f"enrich-plan-post-{drift}",
                    contexts=[before, before, after],
                )
                self.assertEqual(
                    result,
                    {
                        "error": "linear_mutation_outcome_unknown",
                        "reason": "lifecycle_readback_mismatch",
                    },
                )
                self.assertEqual(
                    len([call for call in mcp.calls if call[0] == "save_issue"]),
                    1,
                )

    async def test_enrich_plan_rejects_stale_human_revision_before_dispatch(self):
        before = self.plan_context()
        drift = self.plan_context(updated_at="2026-08-09T18:00:30.000Z")
        result, mcp = await self.run_plan_action(
            operation_key="enrich-plan-stale",
            contexts=[before, drift],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "lifecycle_pre_dispatch_changed"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_enrich_plan_preserves_structured_source_in_fenced_block(self):
        detailed_source = """## Amaç
Existing detailed human plan remains authoritative.

## Kabul kriterleri
- Preserve this exact structured source.
- Keep the human revision conflict guard.
"""
        description = self.plan_description().replace(
            "Kaynak brief: Short brief",
            f"Short brief\n\nKaynak brief verbatim korunmuştur:\n\n```markdown\n{detailed_source}```",
            1,
        )
        before = self.plan_context(description=detailed_source)
        after = self.plan_context(
            updated_at="2026-08-09T18:01:00.000Z",
            description=description,
        )
        result, mcp = await self.run_plan_action(
            operation_key="enrich-plan-structured-source",
            contexts=[before, before, after],
            description=description,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            [call for call in mcp.calls if call[0] == "save_issue"],
            [("save_issue", {"id": "OPS-105", "description": description}, True)],
        )

    async def test_enrich_plan_rejects_unfenced_non_sparse_source(self):
        detailed_source = "Detailed source " + " ".join(
            f"evidence{index}" for index in range(100)
        )
        description = self.plan_description().replace(
            "Kaynak brief: Short brief",
            f"Short brief\n\nKaynak brief verbatim korunmuştur:\n\n{detailed_source}",
            1,
        )
        result, mcp = await self.run_plan_action(
            operation_key="enrich-plan-unfenced-nonsparse-source",
            contexts=[self.plan_context(description=detailed_source)],
            description=description,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "source_brief_not_fenced"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_enrich_plan_accepts_tilde_fence_around_source_with_backticks(self):
        detailed_source = """## Amaç
Existing source includes its own fenced example.

```text
payload
```
"""
        description = self.plan_description().replace(
            "Kaynak brief: Short brief",
            f"Short brief\n\nKaynak brief verbatim korunmuştur:\n\n~~~~markdown\n{detailed_source}~~~~",
            1,
        )
        before = self.plan_context(description=detailed_source)
        after = self.plan_context(
            updated_at="2026-08-09T18:01:00.000Z",
            description=description,
        )
        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-tilde-fenced-source",
            contexts=[before, before, after],
            description=description,
        )
        self.assertEqual(result["status"], "success")

    async def test_enrich_plan_requires_preserved_source_and_structured_sections(self):
        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-drops-brief",
            contexts=[self.plan_context(description="Original human brief")],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "source_brief_not_preserved"},
        )

        title_only = self.plan_context(description="")
        title_only["title"] = "Title-only human intent"
        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-drops-title",
            contexts=[title_only],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "source_title_not_preserved"},
        )

        duplicate_heading = self.plan_description().replace(
            "## Kapsam\n",
            "## Amaç\nDuplicate purpose section with substantive filler.\n\n## Kapsam\n",
            1,
        )
        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-duplicate-heading",
            contexts=[self.plan_context()],
            description=duplicate_heading,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "plan_template_invalid"},
        )

        extra_heading = self.plan_description() + (
            "\n\n## Extra\nThis additional heading must invalidate the exact section contract."
        )
        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-extra-heading",
            contexts=[self.plan_context()],
            description=extra_heading,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "plan_template_invalid"},
        )

        for suffix, operation_key in (
            ("\n\n##\tTab-separated extra heading", "enrich-plan-tab-h2"),
            ("\n\n##\nBare extra heading", "enrich-plan-bare-h2"),
            ("\n\nExtra setext heading\n---", "enrich-plan-setext-h2"),
            (
                "\n\n```bad`info\n## Extra real H2\n```",
                "enrich-plan-invalid-backtick-fence",
            ),
            ("\n\n> ## Nested blockquote H2", "enrich-plan-nested-quote-h2"),
            ("\n\n> Nested setext H2\n> ---", "enrich-plan-nested-setext-h2"),
            ("\n\n- ## Nested list H2", "enrich-plan-nested-list-h2"),
            ("\n\n## Closing ATX H2 ##", "enrich-plan-closing-atx-h2"),
        ):
            result, _mcp = await self.run_plan_action(
                operation_key=operation_key,
                contexts=[self.plan_context()],
                description=self.plan_description() + suffix,
            )
            self.assertEqual(
                result,
                {"error": "linear_policy_denied", "reason": "plan_template_invalid"},
            )

        punctuation_section = self.plan_description().replace(
            "Şimdilik açık blocker yok. Araştırma bağımsız teslim gerektirirse child issue açılıp doğru uzmana delegate edilecek.",
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        )
        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-punctuation-section",
            contexts=[self.plan_context()],
            description=punctuation_section,
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "plan_template_invalid"},
        )

        fenced_example = self.plan_description().replace(
            "Test çıktıları, vendor read-back, commit/manifest ve gerekiyorsa kanonik Notion bağlantısı Linear issue üzerinde bulunur.",
            "Test çıktıları ve vendor read-back kanıtı bulunur.\n\n```markdown\n## This is code, not a heading\n```",
        )
        self.assertIsNotNone(_parse_plan_sections(fenced_example))

        generic_filler = "\n\n".join(
            f"{heading}\nplaceholder placeholder placeholder placeholder placeholder placeholder placeholder placeholder"
            for heading in (
                "## Amaç",
                "## Kapsam",
                "## Kapsam dışı",
                "## Uygulama planı",
                "## Bağımlılıklar ve alt işler",
                "## Kabul kriterleri",
                "## Doğrulama ve teslim kanıtı",
                "## Riskler ve geri dönüş",
            )
        )
        self.assertIsNone(_parse_plan_sections(generic_filler))

        common = "Generic reusable planning prose repeats context without concrete section-specific operational detail or evidence"
        stuffed_sections = {
            "## Amaç": f"{common} Short brief issue objective outcome source intent preserved visibly",
            "## Kapsam": f"{common} issue scope work phase research",
            "## Kapsam dışı": f"{common} excluded outside onaysız hariç",
            "## Uygulama planı": f"1. {common} test step.\n2. {common} apply step.",
            "## Bağımlılıklar ve alt işler": f"{common} child dependency blocker delegate",
            "## Kabul kriterleri": f"- [ ] {common} kabul evidence.\n- [ ] {common} teslim proof.",
            "## Doğrulama ve teslim kanıtı": f"{common} test canary evidence read-back manifest",
            "## Riskler ve geri dönüş": f"{common} risk drift rollback geri dönüş",
        }
        keyword_stuffed = "\n\n".join(
            f"{heading}\n{stuffed_sections[heading]}" for heading in stuffed_sections
        )
        self.assertIsNone(_parse_plan_sections(keyword_stuffed))

        repeated = "generic reusable planning prose repeats " * 4
        repeated_stuffed = "\n\n".join((
            f"## Amaç\n{repeated} objective purpose Short brief preserved source intent",
            f"## Kapsam\n{repeated} issue scope research phase artifact",
            f"## Kapsam dışı\n{repeated} hariç onaysız boundary excluded action",
            f"## Uygulama planı\n1. {repeated} test apply action alpha beta\n2. {repeated} test apply action gamma delta",
            f"## Bağımlılıklar ve alt işler\n{repeated} child blocker delegate dependency evidence",
            f"## Kabul kriterleri\n- [ ] {repeated} kabul teslim evidence alpha\n- [ ] {repeated} kabul teslim evidence beta",
            f"## Doğrulama ve teslim kanıtı\n{repeated} test canary kanıt manifest read-back",
            f"## Riskler ve geri dönüş\n{repeated} risk drift rollback geri dönüş safeguard",
        ))
        self.assertIsNone(_parse_plan_sections(repeated_stuffed))

        duplicate_plan_items = self.plan_description().replace(
            "2. Child/dependency modelini kur.",
            "2. Canlı bağlamı ve mevcut artefaktları oku.",
        )
        self.assertIsNone(_parse_plan_sections(duplicate_plan_items))

        for duplicate_variant in (
            "[Canlı bağlamı ve mevcut artefaktları oku.](https://example.invalid)",
            "Canlı bağlamı ve mevcut artefaktları&#32;oku.",
            "*Canlı bağlamı ve mevcut artefaktları oku.*",
            "<span>Canlı bağlamı ve mevcut artefaktları oku.</span>",
            "<div>Canlı bağlamı ve mevcut artefaktları oku.</div>",
            "<p>Canlı bağlamı ve mevcut artefaktları oku.</p>",
            "<table><tr><td>Canlı bağlamı ve mevcut artefaktları oku.</td></tr></table>",
            unicodedata.normalize("NFD", "Canlı bağlamı ve mevcut artefaktları oku."),
            "Canlı bağlamı ve mevcut artefaktları o\u200dku.",
        ):
            rendered_duplicate = self.plan_description().replace(
                "2. Child/dependency modelini kur.",
                f"2. {duplicate_variant}",
            )
            self.assertIsNone(_parse_plan_sections(rendered_duplicate))

        closing_required = self.plan_description().replace(
            "## Kapsam\n",
            "## Kapsam ##\n",
            1,
        )
        self.assertIsNone(_parse_plan_sections(closing_required))

        for replacement in (" ## Kapsam", "## Kapsam "):
            whitespace_heading = self.plan_description().replace(
                "## Kapsam\n",
                f"{replacement}\n",
                1,
            )
            self.assertIsNone(_parse_plan_sections(whitespace_heading))

        fenced_pseudo_lists = self.plan_description().replace(
            "1. Canlı bağlamı ve mevcut artefaktları oku.\n2. Child/dependency modelini kur.\n3. İşi test-first yürüt ve kanıtları issue'ya bağla.",
            "Uygulama bağlamı somut artefaktlar üzerinden ayrıntılı şekilde ele alınır.\n```text\n1. pseudo item\n2. pseudo item\n```",
        )
        self.assertIsNone(_parse_plan_sections(fenced_pseudo_lists))

        fenced_marker = self.plan_description().replace(
            "1. Canlı bağlamı ve mevcut artefaktları oku.\n2. Child/dependency modelini kur.\n3. İşi test-first yürüt ve kanıtları issue'ya bağla.",
            "1. Canlı bağlam ve mevcut artefaktlar ayrıntılı biçimde incelenir.\n2. Sonuçlar kontrollü aşamalarla teslim edilir.\n```text\ntest oku kur uygula yürüt adım\n```",
        )
        self.assertIsNone(_parse_plan_sections(fenced_marker))

        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-verbatim-whitespace",
            contexts=[self.plan_context(description="Short brief ")],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "source_brief_not_preserved"},
        )

        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-stale-identical-fresh-key",
            contexts=[self.plan_context(
                updated_at="2026-08-09T18:01:00.000Z",
                description=self.plan_description(),
            )],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "plan_revision_mismatch"},
        )

        result, _mcp = await self.run_plan_action(
            operation_key="enrich-plan-nested-source",
            contexts=[self.plan_context(description="> ## Existing detailed plan")],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "source_brief_not_preserved"},
        )

    async def test_enrich_plan_stale_pending_replay_becomes_unknown(self):
        operation_key = "enrich-plan-stale-pending"
        description = self.plan_description()
        self.ledger.pending_timeout_seconds = -1
        self.ledger.reserve(
            operation_key=operation_key,
            tool_name="save_issue",
            payload={
                "id": "OPS-105",
                "description": description,
                "lifecycle_action": "enrich_plan",
                "expected_updated_at": "2026-08-09T18:00:00.000Z",
            },
            profile_id="general",
            actor_id="actor-1",
            team_id="ops-1",
        )

        result, mcp = await self.run_plan_action(
            operation_key=operation_key,
            contexts=[],
            description=description,
        )
        self.assertEqual(
            result,
            {
                "status": "outcome_unknown",
                "replayed": True,
                "result_id": None,
                "error_code": "stale_pending",
            },
        )
        self.assertFalse(any(call[0] == "save_issue" for call in mcp.calls))

    async def test_enrich_plan_requires_detailed_template(self):
        result, mcp = await self.run_plan_action(
            operation_key="enrich-plan-short",
            contexts=[self.plan_context()],
            description="## Amaç\nToo short",
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "plan_template_invalid"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_enrich_plan_requires_human_owned_agent_delegated_issue(self):
        context = self.plan_context()
        context["assignee"] = {"id": "other-agent", "app": True}
        result, mcp = await self.run_plan_action(
            operation_key="enrich-plan-app-owner",
            contexts=[context],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "human_owner_required"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

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
            quota_admission_lock=self.quota_admission_lock,
            graphql_client=FakeGraphQL(),
            mcp_client=mcp,
        )
        self.assertEqual(result, {"error": "linear_policy_denied", "reason": "team_not_allowed"})
        self.assertEqual([call[0] for call in mcp.calls], [])

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
            quota_admission_lock=self.quota_admission_lock,
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
        with mock.patch(
            "linear_tools.count_operations_issues",
            new=mock.AsyncMock(return_value=0),
        ):
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
                quota_admission_lock=self.quota_admission_lock,
                quota_team_id="ops-1",
                quota_team_key="OPS",
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
                "assignee": {"id": "human-1", "app": False},
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
                "assignee": {"id": "human-1", "app": False},
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

    async def test_complete_child_accepts_creator_managed_specialist_delivery(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "specialist-1"}
        sessions = [
            {
                "id": "specialist-session-1",
                "status": "complete",
                "app_user_id": "specialist-1",
                "terminal_response_count": 1,
            }
        ]
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="child-specialist-complete",
            agent_sessions=sessions,
            after_context={
                **context,
                "state": {"id": "done-1", "type": "completed"},
            },
        )
        self.assertEqual(result.get("status"), "success", result)
        self.assertEqual(
            len([call for call in mcp.calls if call[0] == "save_issue"]),
            1,
        )

    async def test_specialist_completion_authority_is_general_manager_only(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "specialist-1"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            profile_id="researcher",
            operation_key="specialist-manager-profile-denied",
            agent_sessions=[{
                "id": "specialist-session-1",
                "status": "complete",
                "app_user_id": "specialist-1",
                "terminal_response_count": 1,
            }],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "manager_completion_not_allowed"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_delegate_mismatch_without_specialist_delivery(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "other-agent"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="child-delegate-mismatch",
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "delegate_session_required"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_child_outside_parent_project(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "specialist-1"}
        context["project"] = {"id": "other-project"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="child-project-mismatch",
            agent_sessions=[{
                "id": "specialist-session-1",
                "status": "complete",
                "app_user_id": "specialist-1",
                "terminal_response_count": 1,
            }],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "child_project_mismatch"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_complete_child_denies_invalid_specialist_session_evidence(self):
        cases = (
            ("required", [], "delegate_session_required"),
            (
                "response",
                [{
                    "id": "specialist-session-1",
                    "status": "complete",
                    "app_user_id": "specialist-1",
                    "terminal_response_count": 0,
                }],
                "delegate_terminal_response_required",
            ),
            (
                "ambiguous",
                [
                    {
                        "id": "specialist-session-1",
                        "status": "complete",
                        "app_user_id": "specialist-1",
                        "terminal_response_count": 1,
                    },
                    {
                        "id": "specialist-session-2",
                        "status": "complete",
                        "app_user_id": "specialist-1",
                        "terminal_response_count": 1,
                    },
                ],
                "delegate_session_ambiguous",
            ),
            (
                "open",
                [{
                    "id": "specialist-session-1",
                    "status": "active",
                    "app_user_id": "specialist-1",
                    "terminal_response_count": 0,
                }],
                "child_delegate_session_still_open",
            ),
            (
                "error",
                [{
                    "id": "specialist-session-1",
                    "status": "error",
                    "app_user_id": "specialist-1",
                    "terminal_response_count": 0,
                }],
                "delegate_session_not_complete",
            ),
        )
        for name, sessions, reason in cases:
            with self.subTest(name=name):
                context = self.child_terminal_context()
                context["delegate"] = {"id": "specialist-1"}
                result, mcp = await self.run_child_terminal_action(
                    context=context,
                    operation_key=f"specialist-evidence-{name}",
                    agent_sessions=sessions,
                )
                self.assertEqual(
                    result,
                    {"error": "linear_policy_denied", "reason": reason},
                )
                self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_cancel_child_never_uses_specialist_completion_authority(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "specialist-1"}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            action="cancel_child",
            operation_key="specialist-cancel-denied",
            agent_sessions=[{
                "id": "specialist-session-1",
                "status": "complete",
                "app_user_id": "specialist-1",
                "terminal_response_count": 1,
            }],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "delegate_mismatch"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_specialist_session_drift_before_dispatch_fails_closed(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "specialist-1"}
        complete_session = [{
            "id": "specialist-session-1",
            "status": "complete",
            "app_user_id": "specialist-1",
            "terminal_response_count": 1,
        }]
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="specialist-session-drift",
            agent_session_reads=[complete_session, []],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "lifecycle_pre_dispatch_changed"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_specialist_context_drift_after_dispatch_reports_unknown(self):
        complete_session = [{
            "id": "specialist-session-1",
            "status": "complete",
            "app_user_id": "specialist-1",
            "terminal_response_count": 1,
        }]
        for drift in ("parent_assignee", "project", "session"):
            with self.subTest(drift=drift):
                context = self.child_terminal_context()
                context["delegate"] = {"id": "specialist-1"}
                after = json.loads(json.dumps(context))
                after["state"] = {"id": "done-1", "type": "completed"}
                session_reads = None
                if drift == "parent_assignee":
                    after["parent"]["assignee"] = {
                        "id": "specialist-1",
                        "app": True,
                    }
                elif drift == "project":
                    after["project"] = {"id": "other-project"}
                else:
                    session_reads = [
                        complete_session,
                        complete_session,
                        [],
                    ]
                result, mcp = await self.run_child_terminal_action(
                    context=context,
                    operation_key=f"specialist-post-dispatch-{drift}",
                    agent_sessions=complete_session,
                    agent_session_reads=session_reads,
                    after_context=after,
                )
                self.assertEqual(
                    result,
                    {
                        "error": "linear_mutation_outcome_unknown",
                        "reason": "lifecycle_readback_mismatch",
                    },
                )
                self.assertEqual(
                    len([call for call in mcp.calls if call[0] == "save_issue"]),
                    1,
                )

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

    async def test_specialist_completion_rejects_app_user_parent_assignee(self):
        context = self.child_terminal_context()
        context["delegate"] = {"id": "specialist-1"}
        context["parent"]["assignee"] = {"id": "specialist-1", "app": True}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="specialist-parent-assignee-app",
            agent_sessions=[{
                "id": "specialist-session-1",
                "status": "complete",
                "app_user_id": "specialist-1",
                "terminal_response_count": 1,
            }],
        )
        self.assertEqual(
            result,
            {"error": "linear_policy_denied", "reason": "human_parent_required"},
        )
        self.assertEqual([call[0] for call in mcp.calls], ["get_user"])

    async def test_self_delegated_child_rejects_app_user_parent_for_all_terminal_actions(self):
        for action in ("complete_child", "cancel_child"):
            with self.subTest(action=action):
                context = self.child_terminal_context()
                context["parent"]["assignee"] = {"id": "other-app", "app": True}
                result, mcp = await self.run_child_terminal_action(
                    context=context,
                    action=action,
                    operation_key=f"self-parent-app-{action}",
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

    async def test_cancel_creator_child_reconciles_after_parent_is_terminal(self):
        for parent_type in ("completed", "canceled"):
            with self.subTest(parent_type=parent_type):
                before = self.child_terminal_context()
                before["parent"]["state"] = {
                    "id": f"parent-{parent_type}",
                    "type": parent_type,
                }
                after = {**before, "state": {"id": "canceled-1", "type": "canceled"}}
                graph = FakeGraphQL(child_terminal_contexts=[before, before, after])
                mcp = FakeMCP()
                result = await execute_with_clients(
                    profile_id="general",
                    vendor_tool="save_issue",
                    arguments={
                        "id": "OPS-138",
                        "target_team_id": "ops-1",
                        "operation_key": f"op-cancel-stale-child-{parent_type}",
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
                    [("save_issue", {"id": "OPS-138", "state": "canceled-1"}, True)],
                )

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

    async def test_complete_child_closes_stale_creator_parked_session_then_succeeds(self):
        context = self.child_terminal_context()
        after = {**context, "state": {"id": "done-1", "type": "completed"}}
        result, mcp = await self.run_child_terminal_action(
            context=context,
            operation_key="op-complete-stale-parked-session",
            agent_sessions=[{
                "id": "session-1",
                "status": "active",
                "app_user_id": "actor-1",
            }],
            agent_session_reads=[
                [{
                    "id": "session-1",
                    "status": "active",
                    "app_user_id": "actor-1",
                }],
                [],
                [{
                    "id": "session-1",
                    "status": "active",
                    "app_user_id": "actor-1",
                }],
                [],
                [{
                    "id": "session-1",
                    "status": "active",
                    "app_user_id": "actor-1",
                }],
                [],
            ],
            after_context=after,
        )
        self.assertIn("status", result, f"unexpected result: {result}")
        self.assertEqual(result["status"], "success")
        calls = [call for call in mcp.calls if call[0] == "save_issue"]
        self.assertEqual(
            calls,
            [("save_issue", {"id": "OPS-106", "state": "done-1"}, True)],
        )

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
