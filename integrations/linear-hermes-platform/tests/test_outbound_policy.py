from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from outbound_policy import OutboundPolicy  # noqa: E402


class OutboundPolicyTests(unittest.TestCase):
    def standard(self) -> OutboundPolicy:
        return OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
            sensitive_mode="standard",
        )

    def test_read_is_allowed_after_identity_pin(self):
        decision = self.standard().evaluate(
            "get_issue",
            {"id": "OPS-1"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual(decision.action, "allow")

    def test_actor_mismatch_denies_before_operation(self):
        decision = self.standard().evaluate(
            "save_issue",
            {"target_team_id": "ops-1", "title": "Task"},
            live_actor_id="actor-other",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "actor_mismatch"))

    def test_cross_team_mutation_is_denied(self):
        decision = self.standard().evaluate(
            "save_issue",
            {"target_team_id": "game-1", "title": "Task"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "team_not_allowed"))

    def test_create_team_must_match_authoritative_target(self):
        decision = self.standard().evaluate(
            "save_issue",
            {"target_team_id": "ops-1", "team": "game-1", "title": "Task"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "team_argument_mismatch"))

    def test_priority_requires_linear_integer_semantics(self):
        policy = self.standard()
        base = {"id": "OPS-1", "target_team_id": "ops-1"}
        for invalid in ("4", True, 1.5, -1, 5):
            with self.subTest(priority=invalid):
                decision = policy.evaluate(
                    "save_issue",
                    {**base, "priority": invalid},
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual((decision.action, decision.reason), ("deny", "invalid_priority"))
        allowed = policy.evaluate(
            "save_issue",
            {**base, "priority": 4},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual(allowed.action, "allow")

    def test_metadata_only_rejects_free_form_content(self):
        policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
            sensitive_mode="metadata_only",
            metadata_templates={"Metadata task", "Metadata status update"},
        )
        decision = policy.evaluate(
            "save_comment",
            {"target_team_id": "ops-1", "issueId": "OPS-1", "body": "Dose changed"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "sensitive_content"))

    def test_metadata_only_accepts_exact_template(self):
        policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
            sensitive_mode="metadata_only",
            metadata_templates={"Metadata status update"},
        )
        decision = policy.evaluate(
            "save_comment",
            {
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": "Metadata status update",
            },
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual(decision.action, "allow")

    def test_metadata_only_rejects_free_form_label_values(self):
        policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
            sensitive_mode="metadata_only",
            metadata_templates={"Metadata status update"},
        )
        decision = policy.evaluate(
            "save_issue",
            {
                "id": "OPS-1",
                "target_team_id": "ops-1",
                "labels": ["Dose 10 mg"],
            },
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "sensitive_content"))

    def test_metadata_only_rejects_free_form_read_arguments(self):
        policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
            sensitive_mode="metadata_only",
            metadata_templates={"Metadata status update"},
        )
        cases = [
            ("get_issue", {"id": "dose-10mg"}),
            ("list_issues", {"team": "ops-1", "query": "person health detail"}),
            ("list_issues", {"team": "ops-1", "state": "blood pressure 120"}),
        ]
        for tool_name, arguments in cases:
            with self.subTest(tool_name=tool_name, arguments=arguments):
                decision = policy.evaluate(
                    tool_name,
                    arguments,
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual((decision.action, decision.reason), ("deny", "sensitive_content"))

    def test_metadata_only_rejects_unclassified_or_malformed_forwarded_fields(self):
        policy = OutboundPolicy(
            expected_actor_id="actor-1",
            expected_organization_id="org-1",
            allowed_team_ids={"ops-1"},
            sensitive_mode="metadata_only",
            metadata_templates={"Metadata task"},
        )
        cases = [
            ("save_issue", {"id": "private-detail", "target_team_id": "ops-1"}),
            ("save_issue", {"id": "OPS-1", "target_team_id": "ops-1", "priority": 4}),
            ("save_issue", {"id": "OPS-1", "target_team_id": "ops-1", "estimate": 10}),
            ("save_issue", {"id": "OPS-1", "target_team_id": "ops-1", "dueDate": "2026-08-04"}),
            (
                "save_issue",
                {"id": "OPS-1", "target_team_id": "ops-1", "blocks": ["private-detail"]},
            ),
            ("save_comment", {"issueId": "private-detail", "target_team_id": "ops-1"}),
            ("list_issues", {"team": "ops-1", "limit": "private-detail"}),
        ]
        for tool_name, arguments in cases:
            with self.subTest(tool_name=tool_name, arguments=arguments):
                decision = policy.evaluate(
                    tool_name,
                    arguments,
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(
                    (decision.action, decision.reason),
                    ("deny", "sensitive_content"),
                )

    def test_list_issues_requires_allowed_team(self):
        missing = self.standard().evaluate(
            "list_issues",
            {},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        other = self.standard().evaluate(
            "list_issues",
            {"team": "game-1"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        allowed = self.standard().evaluate(
            "list_issues",
            {"team": "ops-1"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((missing.action, missing.reason), ("deny", "team_required"))
        self.assertEqual((other.action, other.reason), ("deny", "team_not_allowed"))
        self.assertEqual(allowed.action, "allow")

    def test_get_issue_relations_are_denied_until_cross_team_filtering_exists(self):
        decision = self.standard().evaluate(
            "get_issue",
            {"id": "OPS-1", "includeRelations": True},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "relations_not_allowed"))

    def test_unknown_tool_is_denied(self):
        decision = self.standard().evaluate(
            "delete_issue",
            {"target_team_id": "ops-1", "id": "OPS-1"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "tool_not_allowed"))


if __name__ == "__main__":
    unittest.main()
