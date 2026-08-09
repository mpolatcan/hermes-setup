"""Pure fail-closed policy for outbound Linear MCP operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

READ_TOOLS = frozenset({"get_issue", "list_issues"})
MUTATION_TOOLS = frozenset({"save_issue", "save_comment"})
ALLOWED_TOOLS = READ_TOOLS | MUTATION_TOOLS
GET_ISSUE_FIELDS = frozenset({"id", "includeRelations"})
LIST_ISSUE_FIELDS = frozenset({"team", "query", "state", "assignee", "delegate", "limit"})

SAVE_ISSUE_FIELDS = frozenset(
    {
        "id",
        "title",
        "team",
        "description",
        "lifecycle_action",
        "state",
        "priority",
        "assignee",
        "delegate",
        "labels",
        "label",
        "project",
        "parentId",
        "milestone",
        "cycle",
        "dueDate",
        "estimate",
        "blocks",
        "blockedBy",
        "relatedTo",
        "removeBlocks",
        "removeBlockedBy",
        "removeRelatedTo",
        "target_team_id",
        "operation_key",
    }
)
SAVE_COMMENT_FIELDS = frozenset(
    {"id", "issueId", "body", "comment_purpose", "target_team_id", "operation_key"}
)
SENSITIVE_TEXT_FIELDS = frozenset({"title", "description", "body", "comment"})
METADATA_UUID_ONLY_FIELDS = frozenset(
    {"state", "assignee", "delegate", "labels", "label", "project", "milestone", "cycle"}
)
METADATA_ISSUE_REF_FIELDS = frozenset(
    {
        "parentId", "blocks", "blockedBy", "relatedTo",
        "removeBlocks", "removeBlockedBy", "removeRelatedTo",
    }
)
METADATA_DENIED_FIELDS = frozenset({"priority", "estimate", "dueDate"})
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
ISSUE_REF_RE = re.compile(
    r"^(?:[A-Z][A-Z0-9]{0,15}-[1-9][0-9]*|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$"
)
LINEAR_PROFILE_URL_RE = re.compile(
    r"^\s*(https://linear\.app/[a-z0-9][a-z0-9-]{0,62}/profiles/"
    r"[a-z0-9][a-z0-9-]{0,63})(?=$|[\s,;:!?])",
    re.ASCII,
)


def extract_linear_profile_url(body: str) -> str | None:
    """Return a canonical API mention URL only when it is the first visible token."""
    match = LINEAR_PROFILE_URL_RE.search(str(body or ""))
    return match.group(1) if match else None


def _contains_explicit_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_explicit_null(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_explicit_null(item) for item in value)
    return False


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str


class OutboundPolicy:
    def __init__(
        self,
        *,
        expected_actor_id: str,
        expected_organization_id: str,
        allowed_team_ids: Iterable[str],
        sensitive_mode: str = "standard",
        metadata_templates: Iterable[str] = (),
    ) -> None:
        self.expected_actor_id = str(expected_actor_id or "")
        self.expected_organization_id = str(expected_organization_id or "")
        self.allowed_team_ids = frozenset(str(value) for value in allowed_team_ids if value)
        self.sensitive_mode = str(sensitive_mode or "standard")
        self.metadata_templates = frozenset(str(value) for value in metadata_templates)

    def is_configured(self) -> bool:
        return bool(
            self.expected_actor_id
            and self.expected_organization_id
            and self.allowed_team_ids
            and self.sensitive_mode in {"standard", "metadata_only"}
        )

    def preflight(self, tool_name: str, args: dict[str, Any]) -> PolicyDecision:
        """Run all config/local argument checks without claiming a live vendor identity."""
        return self.evaluate(
            tool_name,
            args,
            live_actor_id=self.expected_actor_id,
            live_organization_id=self.expected_organization_id,
        )

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        live_actor_id: str,
        live_organization_id: str,
    ) -> PolicyDecision:
        if not self.is_configured():
            return PolicyDecision("deny", "policy_not_configured")
        if str(live_actor_id or "") != self.expected_actor_id:
            return PolicyDecision("deny", "actor_mismatch")
        if str(live_organization_id or "") != self.expected_organization_id:
            return PolicyDecision("deny", "organization_mismatch")
        if tool_name not in ALLOWED_TOOLS:
            return PolicyDecision("deny", "tool_not_allowed")
        if tool_name == "get_issue":
            if set(arguments) - GET_ISSUE_FIELDS:
                return PolicyDecision("deny", "field_not_allowed")
            if any(_contains_explicit_null(value) for value in arguments.values()):
                return PolicyDecision("deny", "invalid_null")
            if arguments.get("includeRelations") not in (None, False):
                return PolicyDecision("deny", "relations_not_allowed")
            if self.sensitive_mode == "metadata_only" and not ISSUE_REF_RE.fullmatch(
                str(arguments.get("id") or "")
            ):
                return PolicyDecision("deny", "sensitive_content")
            return PolicyDecision("allow", "read_allowed")
        if tool_name == "list_issues":
            if set(arguments) - LIST_ISSUE_FIELDS:
                return PolicyDecision("deny", "field_not_allowed")
            if any(_contains_explicit_null(value) for value in arguments.values()):
                return PolicyDecision("deny", "invalid_null")
            team_id = str(arguments.get("team") or "")
            if not team_id:
                return PolicyDecision("deny", "team_required")
            if team_id not in self.allowed_team_ids:
                return PolicyDecision("deny", "team_not_allowed")
            if self.sensitive_mode == "metadata_only":
                if arguments.get("query") not in (None, ""):
                    return PolicyDecision("deny", "sensitive_content")
                for field in ("state", "assignee", "delegate"):
                    value = arguments.get(field)
                    if value not in (None, "") and not UUID_RE.fullmatch(str(value)):
                        return PolicyDecision("deny", "sensitive_content")
                limit = arguments.get("limit")
                if limit is not None and (
                    isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250
                ):
                    return PolicyDecision("deny", "sensitive_content")
            return PolicyDecision("allow", "read_allowed")

        allowed_fields = SAVE_ISSUE_FIELDS if tool_name == "save_issue" else SAVE_COMMENT_FIELDS
        if set(arguments) - allowed_fields:
            return PolicyDecision("deny", "field_not_allowed")
        if any(
            _contains_explicit_null(value)
            and not (tool_name == "save_issue" and field == "project" and value is None)
            for field, value in arguments.items()
        ):
            return PolicyDecision("deny", "invalid_null")
        target_team_id = str(arguments.get("target_team_id") or "")
        if not target_team_id:
            return PolicyDecision("deny", "team_required")
        if target_team_id not in self.allowed_team_ids:
            return PolicyDecision("deny", "team_not_allowed")
        if tool_name == "save_issue":
            if "state" in arguments:
                return PolicyDecision("deny", "state_transition_not_allowed")
            if "lifecycle_action" in arguments:
                lifecycle_action = arguments.get("lifecycle_action")
                if not isinstance(lifecycle_action, str) or lifecycle_action not in {
                    "start",
                    "complete_child",
                    "cancel_child",
                }:
                    return PolicyDecision("deny", "invalid_lifecycle_action")
                if not arguments.get("id"):
                    return PolicyDecision("deny", "lifecycle_issue_required")
                lifecycle_fields = {
                    "operation_key", "target_team_id", "id", "lifecycle_action"
                }
                if set(arguments) - lifecycle_fields:
                    return PolicyDecision("deny", "lifecycle_fields_not_allowed")
            requested_team = str(arguments.get("team") or "")
            if not arguments.get("id") and not requested_team:
                return PolicyDecision("deny", "team_argument_required")
            if requested_team and requested_team != target_team_id:
                return PolicyDecision("deny", "team_argument_mismatch")
            priority = arguments.get("priority")
            if priority is not None and (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or not 0 <= priority <= 4
            ):
                return PolicyDecision("deny", "invalid_priority")
        else:
            purpose = str(arguments.get("comment_purpose") or "checkpoint")
            if purpose not in {"checkpoint", "mention", "handoff"}:
                return PolicyDecision("deny", "invalid_comment_purpose")
            if purpose in {"mention", "handoff"}:
                if arguments.get("id"):
                    return PolicyDecision("deny", "comment_update_handoff_not_allowed")
                if not extract_linear_profile_url(str(arguments.get("body") or "")):
                    return PolicyDecision("deny", "explicit_mention_required")

        if self.sensitive_mode == "metadata_only":
            if any(field in arguments for field in METADATA_DENIED_FIELDS):
                return PolicyDecision("deny", "sensitive_content")
            for field in SENSITIVE_TEXT_FIELDS:
                if field not in arguments:
                    continue
                value = str(arguments.get(field) or "")
                if value not in self.metadata_templates:
                    return PolicyDecision("deny", "sensitive_content")
            for field in METADATA_UUID_ONLY_FIELDS:
                raw_value = arguments.get(field)
                if raw_value in (None, ""):
                    continue
                values = raw_value if isinstance(raw_value, list) else [raw_value]
                if any(not UUID_RE.fullmatch(str(value)) for value in values):
                    return PolicyDecision("deny", "sensitive_content")
            if tool_name == "save_issue":
                issue_id = arguments.get("id")
                if issue_id not in (None, "") and not ISSUE_REF_RE.fullmatch(str(issue_id)):
                    return PolicyDecision("deny", "sensitive_content")
                for field in METADATA_ISSUE_REF_FIELDS:
                    raw_value = arguments.get(field)
                    if raw_value in (None, ""):
                        continue
                    values = raw_value if isinstance(raw_value, list) else [raw_value]
                    if any(not ISSUE_REF_RE.fullmatch(str(value)) for value in values):
                        return PolicyDecision("deny", "sensitive_content")
            else:
                comment_id = arguments.get("id")
                if comment_id not in (None, "") and not UUID_RE.fullmatch(str(comment_id)):
                    return PolicyDecision("deny", "sensitive_content")
                issue_id = arguments.get("issueId")
                if not ISSUE_REF_RE.fullmatch(str(issue_id or "")):
                    return PolicyDecision("deny", "sensitive_content")

        return PolicyDecision("allow", "policy_allowed")
