from __future__ import annotations

import asyncio
import hashlib
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

from linear_tools import (  # noqa: E402
    _human_approval_config_safe,
    _request_mutation_approval,
    execute_with_clients,
    register_outbound_tools,
)
from mcp_client import LinearMCPToolError  # noqa: E402
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
        self.approval_config = mock.patch(
            "linear_tools._human_approval_config_safe",
            create=True,
            return_value=True,
        )
        self.approval_config.start()
        self.approval_bypass = mock.patch(
            "linear_tools._approval_bypass_active",
            create=True,
            return_value=False,
        )
        self.approval_bypass.start()

    def tearDown(self):
        self.approval_bypass.stop()
        self.approval_config.stop()

    def extra(self, *, enabled=True, mutations=False):
        return {
            "oauth_file": "/tmp/credential",
            "database_path": "/tmp/database",
            "outbound_mcp": {
                "enabled": enabled,
                "mutations_enabled": mutations,
                "ledger_path": "/tmp/outbound-linear-mcp.sqlite3",
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
        self.assertEqual(
            ctx.tools["linear_save_issue"]["schema"]["parameters"]["properties"]["priority"],
            {"type": "number"},
        )
        directive = ctx.hooks["pre_tool_call"](
            tool_name="linear_save_comment",
            args={
                "operation_key": "op-approval-1",
                "target_team_id": "ops-1",
                "body": "must-not-appear-in-approval",
            },
        )
        self.assertIsNone(directive)
        self.assertEqual(ctx.events[0], ("hook", "pre_tool_call"))

    def test_read_tool_does_not_request_approval(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        directive = ctx.hooks["pre_tool_call"](
            tool_name="linear_get_issue",
            args={"id": "OPS-1"},
        )
        self.assertIsNone(directive)

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

    def test_unsupported_approval_runtime_never_registers_mutations(self):
        ctx = FakeContext()
        with mock.patch("linear_tools._approval_runtime_supported", return_value=False):
            register_outbound_tools(ctx, extra=self.extra(mutations=True))
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})
        self.assertEqual(ctx.hooks, {})

    def test_tool_name_collision_registers_no_outbound_surface(self):
        ctx = FakeContext()
        with mock.patch("linear_tools._tool_names_available", return_value=False):
            register_outbound_tools(ctx, extra=self.extra(mutations=True))
        self.assertEqual(ctx.tools, {})
        self.assertEqual(ctx.hooks, {})

    def test_non_manual_approval_config_never_registers_mutations(self):
        ctx = FakeContext()
        with mock.patch("linear_tools._human_approval_config_safe", return_value=False):
            register_outbound_tools(ctx, extra=self.extra(mutations=True))
        self.assertEqual(set(ctx.tools), {"linear_get_issue", "linear_list_issues"})
        self.assertEqual(ctx.hooks, {})

    def test_approval_config_requires_explicit_manual_and_cron_deny(self):
        unsafe = [
            {},
            {"approvals": {}},
            {"approvals": {"mode": "manual"}},
            {"approvals": {"cron_mode": "deny"}},
            {"approvals": {"mode": "smart", "cron_mode": "deny"}},
            {"approvals": {"mode": "manual", "cron_mode": "approve"}},
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            for config in unsafe:
                config_path.write_text(json.dumps(config), encoding="utf-8")
                config_path.chmod(0o600)
                with (
                    self.subTest(config=config),
                    mock.patch("hermes_cli.config.get_config_path", return_value=config_path),
                ):
                    self.assertFalse(_human_approval_config_safe())
            config_path.write_text(
                json.dumps({"approvals": {"mode": "manual", "cron_mode": "deny"}}),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            with mock.patch("hermes_cli.config.get_config_path", return_value=config_path):
                self.assertTrue(_human_approval_config_safe())

    def test_registered_hook_blocks_live_config_or_yolo_bypass(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        hook = ctx.hooks["pre_tool_call"]
        with mock.patch("linear_tools._human_approval_config_safe", return_value=False):
            self.assertEqual(
                hook(tool_name="linear_save_issue", args={}),
                {
                    "action": "block",
                    "message": "Linear mutation approval policy is not safely configured.",
                },
            )
        with mock.patch("linear_tools._approval_bypass_active", return_value=True):
            self.assertEqual(
                hook(tool_name="linear_save_issue", args={}),
                {
                    "action": "block",
                    "message": "Linear mutations are disabled while approval bypass is active.",
                },
            )

    def test_mutation_handler_rechecks_approval_policy_before_client_creation(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        handler = ctx.tools["linear_save_issue"]["handler"]
        with (
            mock.patch("linear_tools._human_approval_config_safe", return_value=False),
            mock.patch("linear_tools.LinearOAuthStore", side_effect=AssertionError("client created")),
        ):
            result = asyncio.run(
                handler(
                    {
                        "operation_key": "op-handler-gate",
                        "target_team_id": "ops-1",
                        "id": "OPS-1",
                    }
                )
            )
        self.assertEqual(
            json.loads(result),
            {"error": "linear_policy_denied", "reason": "approval_policy_unsafe"},
        )

    def test_handler_native_approval_uses_hashed_operation_key_and_hides_content(self):
        with mock.patch(
            "linear_tools._request_tool_approval_sync",
            return_value={"approved": True},
        ) as approval:
            allowed = asyncio.run(
                _request_mutation_approval(
                    tool_name="linear_save_comment",
                    profile_id="general",
                    args={
                        "operation_key": "op-approval-1",
                        "target_team_id": "ops-1",
                        "body": "must-not-appear-in-approval",
                    },
                )
            )
        self.assertTrue(allowed)
        tool_name, message, rule_key = approval.call_args.args
        self.assertEqual(tool_name, "linear_save_comment")
        self.assertNotIn("must-not-appear", message)
        self.assertEqual(
            rule_key,
            "linear-mcp:linear_save_comment:"
            + hashlib.sha256(b"op-approval-1").hexdigest(),
        )

    def test_handler_denies_when_native_approval_is_not_attested(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        handler = ctx.tools["linear_save_issue"]["handler"]
        with (
            mock.patch("linear_tools._request_mutation_approval", return_value=False),
            mock.patch("linear_tools.LinearOAuthStore", side_effect=AssertionError("client created")),
        ):
            result = asyncio.run(
                handler(
                    {
                        "operation_key": "op-handler-gate",
                        "target_team_id": "ops-1",
                        "id": "OPS-1",
                    }
                )
            )
        self.assertEqual(
            json.loads(result),
            {"error": "linear_policy_denied", "reason": "human_approval_required"},
        )

    def test_handler_preflights_team_before_approval_prompt(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        handler = ctx.tools["linear_save_issue"]["handler"]
        with mock.patch(
            "linear_tools._request_mutation_approval",
            side_effect=AssertionError("approval requested"),
        ):
            result = asyncio.run(
                handler(
                    {
                        "operation_key": "op-preflight",
                        "target_team_id": "private-health-text",
                        "id": "OPS-1",
                    }
                )
            )
        self.assertEqual(
            json.loads(result),
            {"error": "linear_policy_denied", "reason": "team_not_allowed"},
        )

    def test_handler_rechecks_config_after_native_approval(self):
        ctx = FakeContext()
        register_outbound_tools(ctx, extra=self.extra(mutations=True))
        handler = ctx.tools["linear_save_issue"]["handler"]
        with (
            mock.patch(
                "linear_tools._human_approval_config_safe",
                side_effect=[True, False],
            ),
            mock.patch("linear_tools._request_mutation_approval", return_value=True),
            mock.patch("linear_tools.LinearOAuthStore", side_effect=AssertionError("client created")),
        ):
            result = asyncio.run(
                handler(
                    {
                        "operation_key": "op-post-approval",
                        "target_team_id": "ops-1",
                        "id": "OPS-1",
                    }
                )
            )
        self.assertEqual(
            json.loads(result),
            {"error": "linear_policy_denied", "reason": "approval_policy_changed"},
        )

    def test_approval_config_ignores_cached_raw_config(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            config_path.write_text(
                "approvals:\n  mode: smart\n  cron_mode: approve\n",
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            with (
                mock.patch("hermes_cli.config.get_config_path", return_value=config_path),
                mock.patch(
                    "hermes_cli.config.read_raw_config",
                    return_value={"approvals": {"mode": "manual", "cron_mode": "deny"}},
                ),
            ):
                self.assertFalse(_human_approval_config_safe())

    def test_mutations_require_distinct_outbound_ledger_path(self):
        for ledger_path in ("", "relative-outbound.sqlite3", "/tmp/database"):
            with self.subTest(ledger_path=ledger_path):
                extra = self.extra(mutations=True)
                extra["outbound_mcp"]["ledger_path"] = ledger_path
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


class FakeGraphQL:
    actor_id = "actor-1"
    organization_id = "org-1"

    def __init__(self, issue_team="ops-1", issue_teams=None) -> None:
        self.issue_team = issue_team
        self.issue_teams = issue_teams or {}

    async def get_issue_team_id(self, issue_id):
        return self.issue_teams.get(issue_id, self.issue_team)

    async def get_comment_team_id(self, _comment_id):
        return self.issue_team


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
