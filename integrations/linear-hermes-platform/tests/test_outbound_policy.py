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

    def test_only_project_accepts_explicit_null_on_model_surface(self):
        policy = self.standard()
        allowed = policy.evaluate(
            "save_issue",
            {"target_team_id": "ops-1", "team": "ops-1", "title": "Task", "project": None},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual(allowed.action, "allow")

        cases = (
            ("get_issue", {"id": "OPS-1", "includeRelations": None}),
            ("list_issues", {"team": "ops-1", "query": None}),
            ("save_issue", {"target_team_id": "ops-1", "team": "ops-1", "title": None}),
            (
                "save_comment",
                {
                    "target_team_id": "ops-1",
                    "issueId": "OPS-1",
                    "body": None,
                    "comment_purpose": "checkpoint",
                },
            ),
        )
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name):
                denied = policy.evaluate(
                    tool_name,
                    arguments,
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual((denied.action, denied.reason), ("deny", "invalid_null"))

    def test_nested_explicit_null_is_rejected(self):
        decision = self.standard().evaluate(
            "save_issue",
            {
                "target_team_id": "ops-1",
                "team": "ops-1",
                "title": "Task",
                "labels": [None],
            },
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((decision.action, decision.reason), ("deny", "invalid_null"))

    def test_comment_handoff_requires_canonical_linear_profile_url_first(self):
        policy = self.standard()
        for body in (
            "@Doruk please verify this.",
            "Please ask https://linear.app/mpolatcan/profiles/doruk to verify this.",
            "https://example.com/mpolatcan/profiles/doruk please verify this.",
        ):
            with self.subTest(body=body):
                decision = policy.evaluate(
                    "save_comment",
                    {
                        "target_team_id": "ops-1",
                        "issueId": "OPS-1",
                        "body": body,
                        "comment_purpose": "handoff",
                    },
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(
                    (decision.action, decision.reason),
                    ("deny", "explicit_mention_required"),
                )

        allowed = policy.evaluate(
            "save_comment",
            {
                "target_team_id": "ops-1",
                "issueId": "OPS-1",
                "body": "https://linear.app/mpolatcan/profiles/doruk please verify this.",
                "comment_purpose": "handoff",
            },
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual(allowed.action, "allow")

    def test_comment_update_cannot_claim_mention_or_handoff_exception(self):
        for purpose in ("mention", "handoff"):
            with self.subTest(purpose=purpose):
                decision = self.standard().evaluate(
                    "save_comment",
                    {
                        "id": "comment-1",
                        "target_team_id": "ops-1",
                        "issueId": "OPS-1",
                        "body": "https://linear.app/mpolatcan/profiles/doruk ping",
                        "comment_purpose": purpose,
                    },
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(
                    (decision.action, decision.reason),
                    ("deny", "comment_update_handoff_not_allowed"),
                )

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

    def test_model_facing_issue_tool_cannot_write_raw_workflow_state(self):
        for state in ("Done", "Completed", "In Progress", "state-uuid"):
            with self.subTest(state=state):
                decision = self.standard().evaluate(
                    "save_issue",
                    {"id": "OPS-1", "target_team_id": "ops-1", "state": state},
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(
                    (decision.action, decision.reason),
                    ("deny", "state_transition_not_allowed"),
                )

    def test_model_facing_issue_tool_allows_only_narrow_semantic_lifecycle_actions(self):
        policy = self.standard()
        for action in ("start", "complete_child", "cancel_child"):
            with self.subTest(action=action):
                allowed = policy.evaluate(
                    "save_issue",
                    {
                        "id": "OPS-1",
                        "target_team_id": "ops-1",
                        "lifecycle_action": action,
                    },
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(allowed.action, "allow")
        for action in ("complete", "cancel", "done", "In Progress", "", [], {}):
            with self.subTest(action=action):
                denied = policy.evaluate(
                    "save_issue",
                    {"id": "OPS-1", "target_team_id": "ops-1", "lifecycle_action": action},
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(
                    (denied.action, denied.reason),
                    ("deny", "invalid_lifecycle_action"),
                )

    def test_semantic_start_requires_existing_issue_and_forbids_raw_state(self):
        policy = self.standard()
        missing = policy.evaluate(
            "save_issue",
            {"target_team_id": "ops-1", "lifecycle_action": "start"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        mixed = policy.evaluate(
            "save_issue",
            {
                "id": "OPS-1",
                "target_team_id": "ops-1",
                "lifecycle_action": "start",
                "state": "state-uuid",
            },
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((missing.action, missing.reason), ("deny", "lifecycle_issue_required"))
        self.assertEqual((mixed.action, mixed.reason), ("deny", "state_transition_not_allowed"))

    def test_semantic_start_cannot_bundle_other_issue_mutations(self):
        policy = self.standard()
        for field, value in (("delegate", "other"), ("title", "renamed"), ("priority", 1)):
            with self.subTest(field=field):
                decision = policy.evaluate(
                    "save_issue",
                    {
                        "id": "OPS-1",
                        "target_team_id": "ops-1",
                        "lifecycle_action": "start",
                        field: value,
                    },
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(
                    (decision.action, decision.reason),
                    ("deny", "lifecycle_fields_not_allowed"),
                )

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

    def test_comment_purpose_requires_declared_exception_and_explicit_mention(self):
        policy = self.standard()
        base = {
            "target_team_id": "ops-1",
            "issueId": "OPS-1",
        }
        invalid = policy.evaluate(
            "save_comment",
            {**base, "body": "Please verify this.", "comment_purpose": "handoff"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        unknown = policy.evaluate(
            "save_comment",
            {**base, "body": "@Doruk verify this.", "comment_purpose": "progress"},
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        allowed = policy.evaluate(
            "save_comment",
            {
                **base,
                "body": "https://linear.app/mpolatcan/profiles/doruk verify this.",
                "comment_purpose": "mention",
            },
            live_actor_id="actor-1",
            live_organization_id="org-1",
        )
        self.assertEqual((invalid.action, invalid.reason), ("deny", "explicit_mention_required"))
        self.assertEqual((unknown.action, unknown.reason), ("deny", "invalid_comment_purpose"))
        self.assertEqual(allowed.action, "allow")

    def test_comment_exception_rejects_non_target_at_signs(self):
        policy = self.standard()
        for body in (
            "@",
            "mail@example.com please verify",
            r"\@Doruk please verify",
            "`@Doruk` please verify",
            "``@Doruk`` please verify",
            "```text\n@Doruk\n``` please verify",
            "~~~text\n@Doruk\n~~~ please verify",
            "https://example.com/@Doruk please verify",
            "Please ask @Doruk to verify",
        ):
            with self.subTest(body=body):
                decision = policy.evaluate(
                    "save_comment",
                    {
                        "target_team_id": "ops-1",
                        "issueId": "OPS-1",
                        "body": body,
                        "comment_purpose": "handoff",
                    },
                    live_actor_id="actor-1",
                    live_organization_id="org-1",
                )
                self.assertEqual(
                    (decision.action, decision.reason),
                    ("deny", "explicit_mention_required"),
                )

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
