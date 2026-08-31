"""Policy-gated Hermes tools that forward to Linear's official MCP."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import os
import re
import stat
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from markdown_it import MarkdownIt

try:
    from .linear_client import (
        LINEAR_ISSUE_CAPACITY,
        LINEAR_ISSUE_CRITICAL_THRESHOLD,
        LinearClient,
        count_workspace_issues,
    )
    from .mcp_client import (
        OFFICIAL_LINEAR_MCP_ENDPOINT,
        LinearMCPClient,
        LinearMCPError,
        LinearMCPToolError,
        MCPOutcomeUnknown,
    )
    from .oauth_store import LinearOAuthStore
    from .ledger import DeliveryLedger
    from .outbound_ledger import (
        FleetGlobalLock,
        FleetGlobalLockError,
        OutboundLedger,
        OutboundLedgerError,
    )
    from .outbound_policy import OutboundPolicy, extract_linear_profile_url
    from .retention import RetentionInventoryReader, build_manifest, classify_inventory
except ImportError:  # Direct module loading in standalone tests/scripts.
    from linear_client import (
        LINEAR_ISSUE_CAPACITY,
        LINEAR_ISSUE_CRITICAL_THRESHOLD,
        LinearClient,
        count_workspace_issues,
    )
    from mcp_client import (
        OFFICIAL_LINEAR_MCP_ENDPOINT,
        LinearMCPClient,
        LinearMCPError,
        LinearMCPToolError,
        MCPOutcomeUnknown,
    )
    from oauth_store import LinearOAuthStore
    from ledger import DeliveryLedger
    from outbound_ledger import (
        FleetGlobalLock,
        FleetGlobalLockError,
        OutboundLedger,
        OutboundLedgerError,
    )
    from outbound_policy import OutboundPolicy, extract_linear_profile_url
    from retention import RetentionInventoryReader, build_manifest, classify_inventory

CAPACITY = LINEAR_ISSUE_CAPACITY
CRITICAL_THRESHOLD = LINEAR_ISSUE_CRITICAL_THRESHOLD

WRAPPER_FIELDS = frozenset(
    {
        "operation_key", "target_team_id", "lifecycle_action", "comment_purpose",
        "expected_updated_at",
    }
)
TOOL_MAP = {
    "linear_get_issue": ("get_issue", False),
    "linear_list_issues": ("list_issues", False),
    "linear_save_issue": ("save_issue", True),
    "linear_save_comment": ("save_comment", True),
}
VENDOR_MUTATION_TOOLS = frozenset(
    vendor_tool for vendor_tool, is_mutation in TOOL_MAP.values() if is_mutation
)
LIFECYCLE_NOOP_LEDGER_PREFIX = "lifecycle-noop:"
LEGACY_QUOTA_ADMISSION_LEDGER_PREFIX = "quota-admission:v1:"
QUOTA_ADMISSION_LEDGER_PREFIX = "quota-admission:v2:"
LIFECYCLE_NOOP_STATUSES = frozenset(
    {
        "already_started",
        "already_completed",
        "already_canceled",
        "already_enriched",
        "already_accepted",
    }
)


def _encode_lifecycle_noop_result(status: str, result_id: str) -> str:
    return f"{LIFECYCLE_NOOP_LEDGER_PREFIX}{status}:{result_id}"


def _decode_lifecycle_noop_result(value: str | None) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.startswith(LIFECYCLE_NOOP_LEDGER_PREFIX):
        return None
    payload = value[len(LIFECYCLE_NOOP_LEDGER_PREFIX):]
    status, separator, result_id = payload.partition(":")
    if not separator or status not in LIFECYCLE_NOOP_STATUSES or not result_id:
        return None
    return status, result_id


def _quota_admission(current_count: int) -> dict[str, Any]:
    projected_count = current_count + 1
    return {
        "severity": "critical",
        "current_count": current_count,
        "projected_count": projected_count,
        "capacity": CAPACITY,
        "buffer_after": CAPACITY - projected_count,
    }


async def _immediate_retention_dry_run(
    graphql_client: LinearClient,
    *,
    team_id: str,
    team_key: str,
    minimum_age_days: int,
) -> dict[str, Any]:
    """Run the canonical classifier in memory; this path has no mutation API."""
    as_of = datetime.now(timezone.utc)
    inventory = await RetentionInventoryReader(graphql_client).read_team(team_id, team_key)
    result = classify_inventory(
        inventory,
        successor_attestations={},
        minimum_age_days=minimum_age_days,
        as_of=as_of,
        team_id=team_id,
        team_key=team_key,
    )
    manifest = build_manifest(result)
    return {
        "mode": "read-only-dry-run",
        "inventory_count": result.summary["inventory_count"],
        "candidate_count": result.summary["candidate_count"],
        "protected_count": result.summary["protected_count"],
        "protected_reason_counts": dict(result.summary["protected_reason_counts"]),
        "manifest_sha256": manifest["sha256"],
        "deletion_performed": False,
    }


def _encode_quota_admission_result(
    result_id: str,
    current_count: int,
    retention_dry_run: dict[str, Any],
) -> str:
    payload = json.dumps(
        {
            "current_count": current_count,
            "result_id": result_id,
            "retention_dry_run": retention_dry_run,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{QUOTA_ADMISSION_LEDGER_PREFIX}{encoded}"


def _decode_quota_admission_result(
    value: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any] | None] | None:
    if not isinstance(value, str):
        return None
    if value.startswith(QUOTA_ADMISSION_LEDGER_PREFIX):
        encoded = value[len(QUOTA_ADMISSION_LEDGER_PREFIX):]
        try:
            padding = "=" * (-len(encoded) % 4)
            decoded = json.loads(
                base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            )
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict):
            return None
        current_count = decoded.get("current_count")
        result_id = decoded.get("result_id")
        retention_dry_run = decoded.get("retention_dry_run")
        if (
            not isinstance(current_count, int)
            or isinstance(current_count, bool)
            or not isinstance(result_id, str)
            or not result_id
            or not isinstance(retention_dry_run, dict)
        ):
            return None
        projected_count = current_count + 1
        if not CRITICAL_THRESHOLD <= projected_count < CAPACITY:
            return None
        return result_id, _quota_admission(current_count), retention_dry_run
    if not value.startswith(LEGACY_QUOTA_ADMISSION_LEDGER_PREFIX):
        return None
    payload = value[len(LEGACY_QUOTA_ADMISSION_LEDGER_PREFIX):]
    raw_count, separator, result_id = payload.partition(":")
    if not separator or not result_id:
        return None
    try:
        current_count = int(raw_count)
    except ValueError:
        return None
    projected_count = current_count + 1
    if not CRITICAL_THRESHOLD <= projected_count < CAPACITY:
        return None
    return result_id, _quota_admission(current_count), None


def _replay_response(
    reservation: Any, *, include_quota_admission: bool = False
) -> dict[str, Any]:
    lifecycle_noop_replay = _decode_lifecycle_noop_result(reservation.result_id)
    if lifecycle_noop_replay is not None:
        noop_status, noop_result_id = lifecycle_noop_replay
        return {
            "status": noop_status,
            "replayed": True,
            "result_id": noop_result_id,
        }
    quota_replay = (
        _decode_quota_admission_result(reservation.result_id)
        if include_quota_admission
        else None
    )
    if quota_replay is not None:
        result_id, admission, retention_dry_run = quota_replay
        response = {
            "status": reservation.status,
            "replayed": True,
            "result_id": result_id,
            "quota_admission": admission,
            "immediate_retention_required": True,
        }
        if retention_dry_run is not None:
            response["retention_dry_run"] = retention_dry_run
        return response
    return {
        "status": reservation.status,
        "replayed": True,
        "result_id": reservation.result_id,
        **({"error_code": reservation.error_code} if reservation.error_code else {}),
    }


READ_ISSUE_SCHEMA = {
    "name": "linear_get_issue",
    "description": "Read one Linear issue through Linear's official MCP. Never mutates Linear.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Issue identifier or UUID"},
            "includeRelations": {"type": "boolean"},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}
LIST_ISSUES_SCHEMA = {
    "name": "linear_list_issues",
    "description": "List Linear issues through Linear's official MCP. Never mutates Linear.",
    "parameters": {
        "type": "object",
        "properties": {
            "team": {"type": "string"},
            "query": {"type": "string"},
            "state": {"type": "string"},
            "assignee": {"type": "string"},
            "delegate": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    },
}
SAVE_ISSUE_SCHEMA = {
    "name": "linear_save_issue",
    "description": "Create or update an issue through Linear's official MCP after local identity, team, policy and idempotency checks.",
    "parameters": {
        "type": "object",
        "properties": {
            "operation_key": {"type": "string", "description": "Stable unique key reused for retries"},
            "target_team_id": {"type": "string", "description": "Authoritative Linear team UUID for local policy"},
            "id": {"type": "string"},
            "title": {"type": "string"},
            "team": {"type": "string"},
            "description": {"type": "string"},
            "lifecycle_action": {
                "type": "string",
                "enum": [
                    "start",
                    "complete_child",
                    "cancel_child",
                    "enrich_plan",
                    "mark_acceptance",
                ],
            },
            "expected_updated_at": {
                "type": "string",
                "description": "Exact updatedAt revision read before guarded description action",
            },
            "priority": {"type": "number"},
            "assignee": {"type": "string"},
            "delegate": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "project": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "parentId": {"type": "string"},
            "milestone": {"type": "string"},
            "cycle": {"type": "string"},
            "dueDate": {"type": "string"},
            "estimate": {"type": "number"},
            "blocks": {"type": "array", "items": {"type": "string"}},
            "blockedBy": {"type": "array", "items": {"type": "string"}},
            "relatedTo": {"type": "array", "items": {"type": "string"}},
            "removeBlocks": {"type": "array", "items": {"type": "string"}},
            "removeBlockedBy": {"type": "array", "items": {"type": "string"}},
            "removeRelatedTo": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["operation_key", "target_team_id"],
        "additionalProperties": False,
    },
}
SAVE_COMMENT_SCHEMA = {
    "name": "linear_save_comment",
    "description": "Create or update a Linear comment through the official MCP after local identity, team, content-policy and idempotency checks.",
    "parameters": {
        "type": "object",
        "properties": {
            "operation_key": {"type": "string", "description": "Stable unique key reused for retries"},
            "target_team_id": {"type": "string", "description": "Authoritative Linear team UUID for local policy"},
            "id": {"type": "string"},
            "issueId": {"type": "string"},
            "body": {"type": "string"},
            "comment_purpose": {
                "type": "string",
                "enum": ["checkpoint", "mention", "handoff"],
                "description": "Durable sessionless checkpoint, or an explicit @mention/handoff exception.",
            },
        },
        "required": ["operation_key", "target_team_id", "issueId", "body"],
        "additionalProperties": False,
    },
}
SCHEMAS = {
    "linear_get_issue": READ_ISSUE_SCHEMA,
    "linear_list_issues": LIST_ISSUES_SCHEMA,
    "linear_save_issue": SAVE_ISSUE_SCHEMA,
    "linear_save_comment": SAVE_COMMENT_SCHEMA,
}


def _extract_first_json(result: dict[str, Any]) -> dict[str, Any]:
    for content in result.get("content") or []:
        if not isinstance(content, dict) or content.get("type") != "text":
            continue
        text = content.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _authoritative_team_allowed(
    vendor_tool: str,
    arguments: dict[str, Any],
    *,
    policy: OutboundPolicy,
    graphql_client: LinearClient,
) -> bool:
    target_team_id = str(arguments.get("target_team_id") or "")
    if vendor_tool == "get_issue":
        issue_id = str(arguments.get("id") or "")
        return bool(
            issue_id
            and await graphql_client.get_issue_team_id(issue_id) in policy.allowed_team_ids
        )
    if vendor_tool == "list_issues":
        return str(arguments.get("team") or "") in policy.allowed_team_ids
    if vendor_tool == "save_comment":
        issue_id = str(arguments.get("issueId") or "")
        if not issue_id or await graphql_client.get_issue_team_id(issue_id) != target_team_id:
            return False
        comment_id = str(arguments.get("id") or "")
        if comment_id and await graphql_client.get_comment_team_id(comment_id) != target_team_id:
            return False
        return True

    if vendor_tool != "save_issue":
        return True
    issue_id = str(arguments.get("id") or "")
    if issue_id and await graphql_client.get_issue_team_id(issue_id) != target_team_id:
        return False
    parent_id = str(arguments.get("parentId") or "")
    if parent_id:
        parent_team = await graphql_client.get_issue_team_id(parent_id)
        if parent_team != target_team_id:
            return False
    for field in (
        "blocks",
        "blockedBy",
        "relatedTo",
        "removeBlocks",
        "removeBlockedBy",
        "removeRelatedTo",
    ):
        for related_issue_id in arguments.get(field) or []:
            related_team = await graphql_client.get_issue_team_id(str(related_issue_id))
            if related_team != target_team_id:
                return False
    return True


def _evaluate_start_context(
    context: dict[str, Any],
    *,
    target_team_id: str,
    actor_id: str,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    if str((context.get("team") or {}).get("id") or "") != target_team_id:
        return None, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"}
    if str((context.get("delegate") or {}).get("id") or "") != actor_id:
        return None, {"error": "linear_policy_denied", "reason": "delegate_mismatch"}
    state = context.get("state") or {}
    state_id = str(state.get("id") or "")
    state_type = str(state.get("type") or "").casefold()
    if not state_id:
        return None, {"error": "linear_policy_denied", "reason": "source_state_unavailable"}
    if state_type == "started":
        return None, {"status": "already_started", "result_id": state_id}
    if state_type not in {"backlog", "unstarted"}:
        return None, {"error": "linear_policy_denied", "reason": "issue_not_startable"}
    candidates = []
    for item in context.get("started_states") or []:
        item_id = str(item.get("id") or "")
        position = item.get("position")
        if (
            item_id
            and str(item.get("type") or "").casefold() == "started"
            and isinstance(position, (int, float))
            and not isinstance(position, bool)
        ):
            candidates.append((float(position), item_id))
    if not candidates:
        return None, {"error": "linear_policy_denied", "reason": "started_state_unavailable"}
    return {
        "source_state_id": state_id,
        "target_state_id": min(candidates)[1],
    }, None


async def _resolve_start_transition(
    issue_id: str,
    *,
    target_team_id: str,
    actor_id: str,
    graphql_client: LinearClient,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    return _evaluate_start_context(
        await graphql_client.get_issue_start_context(issue_id),
        target_team_id=target_team_id,
        actor_id=actor_id,
    )


def _evaluate_child_terminal_context(
    context: dict[str, Any],
    *,
    action: str,
    target_team_id: str,
    actor_id: str,
    open_actor_session: bool,
    delegated_completion_error: str | None = None,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    target_type = "completed" if action == "complete_child" else "canceled"
    if str((context.get("team") or {}).get("id") or "") != target_team_id:
        return None, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"}
    if str((context.get("creator") or {}).get("id") or "") != actor_id:
        return None, {"error": "linear_policy_denied", "reason": "child_creator_mismatch"}
    delegate_id = str((context.get("delegate") or {}).get("id") or "")
    delegated_completion = delegate_id != actor_id
    if delegated_completion:
        if action != "complete_child":
            return None, {"error": "linear_policy_denied", "reason": "delegate_mismatch"}
        if delegated_completion_error:
            return None, {
                "error": "linear_policy_denied",
                "reason": delegated_completion_error,
            }
    parent = context.get("parent") or {}
    if not str(parent.get("id") or ""):
        return None, {"error": "linear_policy_denied", "reason": "child_parent_required"}
    if delegated_completion:
        child_project_id = str((context.get("project") or {}).get("id") or "")
        parent_project_id = str((parent.get("project") or {}).get("id") or "")
        if not child_project_id or not parent_project_id:
            return None, {
                "error": "linear_policy_denied",
                "reason": "delegated_child_project_required",
            }
        if child_project_id != parent_project_id:
            return None, {
                "error": "linear_policy_denied",
                "reason": "child_project_mismatch",
            }
    parent_assignee = parent.get("assignee") or {}
    parent_assignee_id = str(parent_assignee.get("id") or "")
    if (
        not parent_assignee_id
        or parent_assignee_id == actor_id
        or parent_assignee.get("app") is not False
    ):
        return None, {"error": "linear_policy_denied", "reason": "human_parent_required"}
    parent_state_type = str((parent.get("state") or {}).get("type") or "").casefold()
    if parent_state_type not in {"backlog", "unstarted", "started"}:
        if parent_state_type in {"completed", "canceled"}:
            if action != "cancel_child":
                return None, {"error": "linear_policy_denied", "reason": "parent_terminal"}
        else:
            return None, {"error": "linear_policy_denied", "reason": "parent_state_unavailable"}
    if open_actor_session:
        return None, {"error": "linear_policy_denied", "reason": "child_session_still_open"}
    if action == "complete_child" and context.get("open_blockers"):
        return None, {"error": "linear_policy_denied", "reason": "child_has_open_blockers"}

    state = context.get("state") or {}
    state_id = str(state.get("id") or "")
    state_type = str(state.get("type") or "").casefold()
    if not state_id:
        return None, {"error": "linear_policy_denied", "reason": "source_state_unavailable"}
    if state_type == target_type:
        return None, {
            "status": "already_completed" if target_type == "completed" else "already_canceled",
            "result_id": state_id,
        }
    if state_type not in {"backlog", "unstarted", "started"}:
        return None, {"error": "linear_policy_denied", "reason": "child_not_terminal_actionable"}

    candidates = []
    for item in context.get("terminal_states") or []:
        item_id = str(item.get("id") or "")
        position = item.get("position")
        if (
            item_id
            and str(item.get("type") or "").casefold() == target_type
            and isinstance(position, (int, float))
            and not isinstance(position, bool)
        ):
            candidates.append((float(position), item_id))
    if not candidates:
        return None, {"error": "linear_policy_denied", "reason": "terminal_state_unavailable"}
    return {
        "action": action,
        "source_state_id": state_id,
        "target_state_id": min(candidates)[1],
        "target_state_type": target_type,
    }, None


def _deterministic_activity_uuid(item_key: str) -> str:
    """Derive a UUIDv4-shaped activity id from a stable key.

    Linear accepts client-generated activity IDs but its live validator
    requires UUIDv4-shaped values. Derive bytes from the stable item key,
    then set RFC 4122 version/variant bits to v4 (same contract as the
    adapter's `_activity_uuid`).
    """
    digest = hashlib.sha256(f"linear-hermes:{item_key}".encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


async def _release_parked_creator_session(
    issue_id: str,
    *,
    context: dict[str, Any],
    actor_id: str,
    sessions: list[dict[str, str]],
    graphql_client: LinearClient,
) -> list[dict[str, str]]:
    """Close stale native sessions blocking a creator-owned child transition.

    The inbound ``created`` handler parks a Backlog issue whose assignee is a
    planned human owner and whose delegate is the installed app. A coordinator
    child matches that shape (human assignee + agent delegate), so a stale
    native AgentSession can remain ``pending/active/awaitingInput`` even though
    the creator-agent drives the child through MCP lifecycle actions. That
    open session blocks terminal transitions (``child_session_still_open``).

    For a child whose creator is the acting agent, send a closing ``response``
    activity into each such stale session (Linear then completes the session)
    and return the refreshed session list for guard re-evaluation.
    """
    creator_id = str((context.get("creator") or {}).get("id") or "")
    if not creator_id or not hmac.compare_digest(creator_id, actor_id):
        return sessions
    stale = [
        session
        for session in sessions
        if str(session.get("app_user_id") or "") == actor_id
        and str(session.get("status") or "") in {"pending", "active", "awaitingInput"}
    ]
    if not stale:
        return sessions
    item_key = f"parked-release:{issue_id}:{creator_id}"
    for session in stale:
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        activity_id = _deterministic_activity_uuid(f"{item_key}:{session_id}")
        try:
            await graphql_client.create_activity(
                session_id,
                "response",
                "Creator-agent completed this child through the MCP lifecycle; stale planned-activation session closed.",
                activity_id=activity_id,
            )
        except Exception:
            # Best effort: a failed close must not silently pass the guard,
            # so keep the session visible and let the caller fail closed.
            continue
    return await graphql_client.get_issue_agent_sessions(issue_id)


async def _resolve_child_terminal_transition(
    issue_id: str,
    *,
    action: str,
    target_team_id: str,
    actor_id: str,
    manager_completion_allowed: bool,
    graphql_client: LinearClient,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    context = await graphql_client.get_issue_child_terminal_context(issue_id)
    sessions = await graphql_client.get_issue_agent_sessions(issue_id)
    sessions = await _release_parked_creator_session(
        issue_id,
        context=context,
        actor_id=actor_id,
        sessions=sessions,
        graphql_client=graphql_client,
    )
    delegate_id = str((context.get("delegate") or {}).get("id") or "")
    open_actor_session = any(
        str(session.get("app_user_id") or "") == actor_id
        and str(session.get("status") or "") in {"pending", "active", "awaitingInput"}
        for session in sessions
    )
    delegated_completion_error: str | None = None
    if delegate_id != actor_id:
        delegate_sessions = [
            session
            for session in sessions
            if str(session.get("app_user_id") or "") == delegate_id
        ]
        if not manager_completion_allowed:
            delegated_completion_error = "manager_completion_not_allowed"
        elif any(
            str(session.get("status") or "") in {"pending", "active", "awaitingInput"}
            for session in sessions
        ):
            delegated_completion_error = "child_delegate_session_still_open"
        elif not delegate_sessions:
            delegated_completion_error = "delegate_session_required"
        elif len(delegate_sessions) != 1:
            delegated_completion_error = "delegate_session_ambiguous"
        elif str(delegate_sessions[0].get("status") or "") != "complete":
            delegated_completion_error = "delegate_session_not_complete"
        else:
            try:
                response_count = await graphql_client.get_agent_session_terminal_response_count(
                    str(delegate_sessions[0].get("id") or "")
                )
            except Exception:
                delegated_completion_error = "delegate_response_evidence_unavailable"
            else:
                if response_count < 1:
                    delegated_completion_error = "delegate_terminal_response_required"
    return _evaluate_child_terminal_context(
        context,
        action=action,
        target_team_id=target_team_id,
        actor_id=actor_id,
        open_actor_session=open_actor_session,
        delegated_completion_error=delegated_completion_error,
    )


PLAN_REQUIRED_HEADINGS = (
    "## Amaç",
    "## Kapsam",
    "## Kapsam dışı",
    "## Uygulama planı",
    "## Bağımlılıklar ve alt işler",
    "## Kabul kriterleri",
    "## Doğrulama ve teslim kanıtı",
    "## Riskler ve geri dönüş",
)


_COMMONMARK = MarkdownIt("commonmark")


def _inline_visible_text(token: Any) -> str:
    children = getattr(token, "children", None)
    if not children:
        raw = html.unescape(str(getattr(token, "content", "") or ""))
        return "".join(
            character
            for character in unicodedata.normalize("NFKC", raw)
            if unicodedata.category(character) != "Cf"
        )
    parts: list[str] = []
    for child in children:
        if child.type in {"text", "code_inline"}:
            parts.append(html.unescape(str(child.content or "")))
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append(" ")
        elif child.type == "image":
            parts.append(html.unescape(str(child.content or "")))
    raw = "".join(parts)
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", raw)
        if unicodedata.category(character) != "Cf"
    )


def _commonmark_h2_lines(description: str) -> list[str]:
    tokens = _COMMONMARK.parse(description)
    headings: list[str] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != "h2":
            continue
        content = ""
        if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
            content = _inline_visible_text(tokens[index + 1]).strip()
        headings.append(f"## {content}" if content else "##")
    return headings


def _section_substantive(heading: str, content: str) -> bool:
    tokens = _COMMONMARK.parse(content)
    semantic_text = " ".join(
        _inline_visible_text(token) for token in tokens if token.type == "inline"
    )
    words = re.findall(r"[^\W_]+", semantic_text.lower(), flags=re.UNICODE)
    if (
        len(words) < 8
        or len(set(words)) < 5
        or len(set(words)) / len(words) < 0.55
        or sum(len(word) for word in words) < 40
    ):
        return False
    marker_groups = {
        "## Kapsam": (("issue", "iş", "kapsam", "faz", "child", "araştır"),),
        "## Kapsam dışı": (("dışı", "hariç", "onaysız", "yapılmay", "kapsamaz"),),
        "## Uygulama planı": (("test", "oku", "kur", "uygula", "yürüt", "adım"),),
        "## Bağımlılıklar ve alt işler": (("bağıml", "block", "child", "alt iş", "delegate"),),
        "## Kabul kriterleri": (("kabul", "teslim", "kanıt", "read-back", "canary", "doğrula"),),
        "## Doğrulama ve teslim kanıtı": (
            ("test", "canary", "doğrula"),
            ("kanıt", "read-back", "manifest", "çıktı"),
        ),
        "## Riskler ve geri dönüş": (
            ("risk", "drift", "fail-closed"),
            ("rollback", "geri dönüş", "geri alın"),
        ),
    }
    lowered = semantic_text.lower()
    groups = marker_groups.get(heading, ())
    if any(not any(marker in lowered for marker in group) for group in groups):
        return False
    list_items = sum(1 for token in tokens if token.type == "list_item_open")
    if heading in {"## Uygulama planı", "## Kabul kriterleri"} and list_items < 2:
        return False
    normalized_items: list[str] = []
    for index, token in enumerate(tokens):
        if token.type != "list_item_open":
            continue
        item_parts: list[str] = []
        depth = 1
        for nested in tokens[index + 1:]:
            if nested.type == "list_item_open":
                depth += 1
            elif nested.type == "list_item_close":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and nested.type == "inline":
                item_parts.append(_inline_visible_text(nested))
        normalized = " ".join(
            re.findall(r"[^\W_]+", " ".join(item_parts).lower(), flags=re.UNICODE)
        )
        if normalized:
            normalized_items.append(normalized)
    if len(normalized_items) != len(set(normalized_items)):
        return False
    return True


def _parse_plan_sections(description: str) -> dict[str, str] | None:
    if len(description.strip()) < 500:
        return None
    document_tokens = _COMMONMARK.parse(description)
    if any(
        token.type == "html_block"
        or any(child.type == "html_inline" for child in (token.children or []))
        for token in document_tokens
    ):
        return None
    lines = description.splitlines()
    if not lines or lines[0] != PLAN_REQUIRED_HEADINGS[0]:
        return None
    h2_lines = _commonmark_h2_lines(description)
    if h2_lines != list(PLAN_REQUIRED_HEADINGS):
        return None
    positions = [
        int(token.map[0])
        for token in document_tokens
        if token.type == "heading_open" and token.tag == "h2" and token.map
    ]
    if len(positions) != len(PLAN_REQUIRED_HEADINGS):
        return None
    if any(
        lines[position] != heading
        for position, heading in zip(positions, PLAN_REQUIRED_HEADINGS, strict=True)
    ):
        return None
    sections: dict[str, str] = {}
    for index, position in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(lines)
        section = "\n".join(lines[position + 1:end]).strip()
        heading = PLAN_REQUIRED_HEADINGS[index]
        if not _section_substantive(heading, section):
            return None
        sections[heading] = section
    word_counts: list[Counter[str]] = []
    for section in sections.values():
        semantic_text = " ".join(
            _inline_visible_text(token)
            for token in _COMMONMARK.parse(section)
            if token.type == "inline"
        )
        word_counts.append(Counter(re.findall(r"[^\W_]+", semantic_text.lower(), flags=re.UNICODE)))
    for index, left in enumerate(word_counts):
        for right in word_counts[index + 1:]:
            shared_count = sum((left & right).values())
            smaller = min(sum(left.values()), sum(right.values()))
            if smaller and shared_count / smaller >= 0.70:
                return None
    return sections


def _source_is_exact_fenced_block(section: str, source: str) -> bool:
    for token in _COMMONMARK.parse(section):
        if token.type != "fence":
            continue
        if token.content == source or token.content == f"{source}\n":
            return True
    return False


def _evaluate_plan_context(
    context: dict[str, Any],
    *,
    target_team_id: str,
    actor_id: str,
    expected_updated_at: str,
    description: str,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    sections = _parse_plan_sections(description)
    if sections is None:
        return None, {"error": "linear_policy_denied", "reason": "plan_template_invalid"}
    if str((context.get("team") or {}).get("id") or "") != target_team_id:
        return None, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"}
    if str((context.get("delegate") or {}).get("id") or "") != actor_id:
        return None, {"error": "linear_policy_denied", "reason": "delegate_mismatch"}
    assignee = context.get("assignee") or {}
    if not str(assignee.get("id") or "") or assignee.get("app") is not False:
        return None, {"error": "linear_policy_denied", "reason": "human_owner_required"}
    state_type = str((context.get("state") or {}).get("type") or "").casefold()
    if state_type not in {"backlog", "unstarted", "started"}:
        return None, {"error": "linear_policy_denied", "reason": "plan_state_not_active"}
    live_updated_at = str(context.get("updatedAt") or "")
    source_title = str(context.get("title") or "")
    source_description = str(context.get("description") or "")
    purpose_section = sections["## Amaç"]
    if source_title and source_title not in purpose_section:
        return None, {"error": "linear_policy_denied", "reason": "source_title_not_preserved"}
    if source_description == description:
        if not live_updated_at or live_updated_at != expected_updated_at:
            return None, {"error": "linear_policy_denied", "reason": "plan_revision_mismatch"}
        return None, {
            "status": "already_enriched",
            "result_id": str(context.get("id") or ""),
        }
    if not live_updated_at or live_updated_at != expected_updated_at:
        return None, {"error": "linear_policy_denied", "reason": "plan_revision_mismatch"}
    if source_description and source_description not in purpose_section:
        return None, {"error": "linear_policy_denied", "reason": "source_brief_not_preserved"}
    if (
        source_description
        and (len(source_description) > 500 or _commonmark_h2_lines(source_description))
        and not _source_is_exact_fenced_block(purpose_section, source_description)
    ):
        return None, {"error": "linear_policy_denied", "reason": "source_brief_not_fenced"}
    return {
        "team_id": target_team_id,
        "delegate_id": actor_id,
        "assignee_id": str(assignee.get("id") or ""),
        "state_id": str((context.get("state") or {}).get("id") or ""),
        "state_type": state_type,
        "title": str(context.get("title") or ""),
        "updated_at": live_updated_at,
    }, None


_ACCEPTANCE_CHECKBOX = re.compile(r"^([ \t]*[-+*][ \t]+)\[([ xX])\](?=[ \t]+)")


def _acceptance_checkbox_shape(description: str) -> tuple[str, tuple[bool, ...]]:
    checkbox_lines: set[int] = set()
    list_stack: list[str] = []
    for token in _COMMONMARK.parse(description):
        if token.type == "bullet_list_open":
            list_stack.append("bullet")
        elif token.type == "ordered_list_open":
            list_stack.append("ordered")
        elif token.type == "list_item_open" and list_stack and list_stack[-1] == "bullet":
            if token.map:
                checkbox_lines.add(int(token.map[0]))
        elif token.type in {"bullet_list_close", "ordered_list_close"} and list_stack:
            list_stack.pop()

    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", description)
    if lines and lines[-1] == "":
        lines.pop()
    states: list[bool] = []
    for index in sorted(checkbox_lines):
        if index >= len(lines):
            continue
        match = _ACCEPTANCE_CHECKBOX.match(lines[index])
        if match is None:
            continue
        states.append(match.group(2).casefold() == "x")
        lines[index] = (
            lines[index][: match.start(2)]
            + "?"
            + lines[index][match.end(2) :]
        )
    return "".join(lines), tuple(states)


def _evaluate_acceptance_context(
    context: dict[str, Any],
    *,
    target_team_id: str,
    actor_id: str,
    expected_updated_at: str,
    description: str,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    if str((context.get("team") or {}).get("id") or "") != target_team_id:
        return None, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"}
    if str((context.get("delegate") or {}).get("id") or "") != actor_id:
        return None, {"error": "linear_policy_denied", "reason": "delegate_mismatch"}
    assignee = context.get("assignee") or {}
    if not str(assignee.get("id") or "") or assignee.get("app") is not False:
        return None, {"error": "linear_policy_denied", "reason": "human_owner_required"}
    state_type = str((context.get("state") or {}).get("type") or "").casefold()
    if state_type not in {"backlog", "unstarted", "started"}:
        return None, {"error": "linear_policy_denied", "reason": "acceptance_state_not_active"}
    live_updated_at = str(context.get("updatedAt") or "")
    if not live_updated_at or live_updated_at != expected_updated_at:
        return None, {"error": "linear_policy_denied", "reason": "acceptance_revision_mismatch"}

    source_description = str(context.get("description") or "")
    source_shape, source_states = _acceptance_checkbox_shape(source_description)
    target_shape, target_states = _acceptance_checkbox_shape(description)
    if not source_states or len(source_states) != len(target_states) or source_shape != target_shape:
        return None, {"error": "linear_policy_denied", "reason": "acceptance_description_drift"}
    if any(source and not target for source, target in zip(source_states, target_states, strict=True)):
        return None, {"error": "linear_policy_denied", "reason": "acceptance_checkbox_regression"}
    if source_states == target_states:
        if all(source_states):
            return None, {
                "status": "already_accepted",
                "result_id": str(context.get("id") or ""),
            }
        return None, {"error": "linear_policy_denied", "reason": "acceptance_no_change"}
    if not any(not source and target for source, target in zip(source_states, target_states, strict=True)):
        return None, {"error": "linear_policy_denied", "reason": "acceptance_no_change"}
    return {
        "team_id": target_team_id,
        "delegate_id": actor_id,
        "assignee_id": str(assignee.get("id") or ""),
        "state_id": str((context.get("state") or {}).get("id") or ""),
        "state_type": state_type,
        "title": str(context.get("title") or ""),
        "updated_at": live_updated_at,
        "description": description,
    }, None


def _canonicalize_vendor_markdown(description: str) -> str:
    bullet_item_lines: set[int] = set()
    list_stack: list[str] = []
    for token in _COMMONMARK.parse(description):
        if token.type == "bullet_list_open":
            list_stack.append("bullet")
        elif token.type == "ordered_list_open":
            list_stack.append("ordered")
        elif token.type == "list_item_open" and list_stack and list_stack[-1] == "bullet":
            if token.map:
                bullet_item_lines.add(int(token.map[0]))
        elif token.type in {"bullet_list_close", "ordered_list_close"} and list_stack:
            list_stack.pop()
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", description)
    if lines and lines[-1] == "":
        lines.pop()
    for index in bullet_item_lines:
        if index >= len(lines):
            continue
        lines[index] = re.sub(
            r"^([ \t]*)[-+*](?=[ \t]+)",
            r"\1*",
            lines[index],
            count=1,
        )
        lines[index] = re.sub(
            r"^([ \t]*\*[ \t]+)\[[xX]\](?=[ \t]+)",
            r"\1[x]",
            lines[index],
            count=1,
        )
    return "".join(lines)


def _plan_readback_matches(
    context: dict[str, Any],
    *,
    snapshot: dict[str, str],
    expected_updated_at: str,
    description: str,
) -> bool:
    assignee = context.get("assignee") or {}
    state = context.get("state") or {}
    updated_at = str(context.get("updatedAt") or "")
    return bool(
        str((context.get("team") or {}).get("id") or "") == snapshot["team_id"]
        and str((context.get("delegate") or {}).get("id") or "") == snapshot["delegate_id"]
        and str(assignee.get("id") or "") == snapshot["assignee_id"]
        and assignee.get("app") is False
        and str(state.get("id") or "") == snapshot["state_id"]
        and str(state.get("type") or "").casefold() == snapshot["state_type"]
        and str(context.get("title") or "") == snapshot["title"]
        and _canonicalize_vendor_markdown(str(context.get("description") or ""))
        == _canonicalize_vendor_markdown(description)
        and updated_at
        and updated_at != expected_updated_at
    )


async def execute_with_clients(
    *,
    profile_id: str,
    vendor_tool: str,
    arguments: dict[str, Any],
    mutation: bool,
    policy: OutboundPolicy,
    ledger: OutboundLedger | None,
    graphql_client: LinearClient,
    mcp_client: LinearMCPClient,
    quota_admission_lock: FleetGlobalLock | None = None,
    quota_team_ids: frozenset[str] | None = None,
    retention_dry_run: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    _quota_admission_lock_held: bool = False,
    _quota_admission_fd: int | None = None,
    _quota_create_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    derived_mutation = vendor_tool in VENDOR_MUTATION_TOOLS
    if mutation != derived_mutation:
        return {
            "error": "linear_policy_denied",
            "reason": "mutation_classification_mismatch",
        }
    mutation = derived_mutation
    is_issue_create = vendor_tool == "save_issue" and not arguments.get("id")
    if is_issue_create:
        local_preflight = policy.preflight(vendor_tool, arguments)
        if local_preflight.action != "allow":
            return {
                "error": "linear_policy_denied",
                "reason": local_preflight.reason,
            }
    if is_issue_create and not _quota_admission_lock_held:
        if quota_admission_lock is None:
            return {
                "error": "linear_policy_denied",
                "reason": "quota_admission_lock_unavailable",
            }
        lock_fd: int | None = None
        deadline = asyncio.get_running_loop().time() + 30.0
        while lock_fd is None:
            try:
                # Nonblocking acquisition is intentionally synchronous: it performs
                # only bounded local descriptor checks and prevents a cancelled
                # to_thread worker from later acquiring an orphaned fleet lock.
                lock_fd = quota_admission_lock.acquire(blocking=False)
            except FleetGlobalLockError:
                return {
                    "error": "linear_policy_denied",
                    "reason": "quota_admission_lock_unavailable",
                }
            if lock_fd is None:
                if asyncio.get_running_loop().time() >= deadline:
                    return {
                        "error": "linear_policy_denied",
                        "reason": "quota_admission_lock_busy",
                    }
                await asyncio.sleep(0.05)
        create_context: dict[str, Any] = {
            "operation_key": str(arguments.get("operation_key") or ""),
            "profile_id": profile_id,
            "observed_current_count": None,
        }
        try:
            return await execute_with_clients(
                profile_id=profile_id,
                vendor_tool=vendor_tool,
                arguments=arguments,
                mutation=mutation,
                policy=policy,
                ledger=ledger,
                quota_admission_lock=quota_admission_lock,
                quota_team_ids=quota_team_ids,
                retention_dry_run=retention_dry_run,
                graphql_client=graphql_client,
                mcp_client=mcp_client,
                _quota_admission_lock_held=True,
                _quota_admission_fd=lock_fd,
                _quota_create_context=create_context,
            )
        finally:
            quota_admission_lock.release(lock_fd)
    mcp_identity = _extract_first_json(
        await mcp_client.call_tool("get_user", {"query": "me"})
    )
    graph_actor = str(graphql_client.actor_id or "")
    graph_org = str(graphql_client.organization_id or "")
    if str(mcp_identity.get("id") or "") != graph_actor:
        return {"error": "linear_policy_denied", "reason": "mcp_actor_mismatch"}

    decision = policy.evaluate(
        vendor_tool,
        arguments,
        live_actor_id=graph_actor,
        live_organization_id=graph_org,
    )
    if decision.action != "allow":
        return {"error": "linear_policy_denied", "reason": decision.reason}

    if not await _authoritative_team_allowed(
        vendor_tool,
        arguments,
        policy=policy,
        graphql_client=graphql_client,
    ):
        return {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"}

    if vendor_tool == "save_comment":
        issue_id = str(arguments.get("issueId") or "")
        purpose = str(arguments.get("comment_purpose") or "checkpoint")
        if purpose in {"mention", "handoff"}:
            target_url = extract_linear_profile_url(str(arguments.get("body") or ""))
            target = await graphql_client.get_user_by_url(str(target_url or ""))
            if not target:
                return {
                    "error": "linear_policy_denied",
                    "reason": "mention_target_unresolved",
                }
            if str(target.get("id") or "") == graph_actor:
                return {
                    "error": "linear_policy_denied",
                    "reason": "mention_target_self",
                }
        sessions = await graphql_client.get_issue_agent_sessions(issue_id)
        open_for_actor = any(
            str(session.get("app_user_id") or "") == graph_actor
            and str(session.get("status") or "") in {"pending", "active", "awaitingInput"}
            for session in sessions
        )
        if open_for_actor and purpose == "checkpoint":
            return {
                "error": "linear_policy_denied",
                "reason": "session_activity_required",
            }

    lifecycle_action = str(arguments.get("lifecycle_action") or "")
    lifecycle_transition: dict[str, str] | None = None
    plan_snapshot: dict[str, str] | None = None
    description_evaluator: Callable[..., Any] | None = None
    lifecycle_noop_result: dict[str, Any] | None = None
    if lifecycle_action == "start":
        lifecycle_transition, lifecycle_result = await _resolve_start_transition(
            str(arguments.get("id") or ""),
            target_team_id=str(arguments.get("target_team_id") or ""),
            actor_id=graph_actor,
            graphql_client=graphql_client,
        )
        if lifecycle_result is not None:
            if str(lifecycle_result.get("status") or "") in LIFECYCLE_NOOP_STATUSES:
                lifecycle_noop_result = lifecycle_result
            else:
                return lifecycle_result
    elif lifecycle_action in {"complete_child", "cancel_child"}:
        lifecycle_transition, lifecycle_result = await _resolve_child_terminal_transition(
            str(arguments.get("id") or ""),
            action=lifecycle_action,
            target_team_id=str(arguments.get("target_team_id") or ""),
            actor_id=graph_actor,
            manager_completion_allowed=profile_id == "general",
            graphql_client=graphql_client,
        )
        if lifecycle_result is not None:
            if str(lifecycle_result.get("status") or "") in LIFECYCLE_NOOP_STATUSES:
                lifecycle_noop_result = lifecycle_result
            else:
                return lifecycle_result
    elif lifecycle_action in {"enrich_plan", "mark_acceptance"}:
        if ledger is None:
            return {"error": "linear_idempotency_rejected", "reason": "ledger_not_configured"}
        plan_ledger_payload = {
            "id": str(arguments.get("id") or ""),
            "description": str(arguments.get("description") or ""),
            "lifecycle_action": lifecycle_action,
            "expected_updated_at": str(arguments.get("expected_updated_at") or ""),
        }
        try:
            existing = await asyncio.to_thread(
                ledger.lookup,
                operation_key=str(arguments.get("operation_key") or ""),
                tool_name=vendor_tool,
                payload=plan_ledger_payload,
                profile_id=profile_id,
                actor_id=graph_actor,
                team_id=str(arguments.get("target_team_id") or ""),
            )
        except OutboundLedgerError as exc:
            return {"error": "linear_idempotency_rejected", "reason": str(exc)}
        if existing is not None:
            if existing.status == "pending":
                try:
                    existing = await asyncio.to_thread(
                        ledger.reserve,
                        operation_key=str(arguments.get("operation_key") or ""),
                        tool_name=vendor_tool,
                        payload=plan_ledger_payload,
                        profile_id=profile_id,
                        actor_id=graph_actor,
                        team_id=str(arguments.get("target_team_id") or ""),
                    )
                except OutboundLedgerError as exc:
                    return {"error": "linear_idempotency_rejected", "reason": str(exc)}
            lifecycle_noop_replay = _decode_lifecycle_noop_result(existing.result_id)
            if lifecycle_noop_replay is not None:
                noop_status, noop_result_id = lifecycle_noop_replay
                return {
                    "status": noop_status,
                    "replayed": True,
                    "result_id": noop_result_id,
                }
            return {
                "status": existing.status,
                "replayed": True,
                "result_id": existing.result_id,
                **({"error_code": existing.error_code} if existing.error_code else {}),
            }
        context = await graphql_client.get_issue_plan_context(str(arguments.get("id") or ""))
        description_evaluator = (
            _evaluate_plan_context
            if lifecycle_action == "enrich_plan"
            else _evaluate_acceptance_context
        )
        plan_snapshot, plan_result = description_evaluator(
            context,
            target_team_id=str(arguments.get("target_team_id") or ""),
            actor_id=graph_actor,
            expected_updated_at=str(arguments.get("expected_updated_at") or ""),
            description=str(arguments.get("description") or ""),
        )
        if plan_result is not None:
            if str(plan_result.get("status") or "") in LIFECYCLE_NOOP_STATUSES:
                lifecycle_noop_result = plan_result
            else:
                return plan_result

    forwarded = {key: value for key, value in arguments.items() if key not in WRAPPER_FIELDS}
    if vendor_tool == "list_issues":
        forwarded["includeArchived"] = True
    if lifecycle_transition is not None:
        forwarded["state"] = lifecycle_transition["target_state_id"]
    elif lifecycle_noop_result is not None and lifecycle_action not in {
        "enrich_plan",
        "mark_acceptance",
    }:
        forwarded["state"] = str(lifecycle_noop_result.get("result_id") or "")
    ledger_payload = dict(forwarded)
    if lifecycle_action in {
        "start",
        "complete_child",
        "cancel_child",
        "enrich_plan",
        "mark_acceptance",
    }:
        ledger_payload.pop("state", None)
        ledger_payload["lifecycle_action"] = lifecycle_action
        if lifecycle_action in {"enrich_plan", "mark_acceptance"}:
            ledger_payload["expected_updated_at"] = str(
                arguments.get("expected_updated_at") or ""
            )
    if not mutation:
        return await mcp_client.call_tool(vendor_tool, forwarded)
    if ledger is None:
        return {"error": "linear_idempotency_rejected", "reason": "ledger_not_configured"}

    operation_key = str(arguments.get("operation_key") or "")
    team_id = str(arguments.get("target_team_id") or "")
    quota_admission: dict[str, Any] | None = None
    retention_result: dict[str, Any] | None = None
    if is_issue_create:
        try:
            existing = await asyncio.to_thread(
                ledger.lookup,
                operation_key=operation_key,
                tool_name=vendor_tool,
                payload=ledger_payload,
                profile_id=profile_id,
                actor_id=graph_actor,
                team_id=team_id,
            )
            if existing is not None:
                if existing.status == "pending":
                    existing = await asyncio.to_thread(
                        ledger.reserve,
                        operation_key=operation_key,
                        tool_name=vendor_tool,
                        payload=ledger_payload,
                        profile_id=profile_id,
                        actor_id=graph_actor,
                        team_id=team_id,
                    )
                if (
                    existing.status == "success"
                    and quota_admission_lock is not None
                    and _quota_admission_fd is not None
                ):
                    quota_admission_lock.resolve_own_create_fence(
                        _quota_admission_fd,
                        operation_key=operation_key,
                        profile_id=profile_id,
                    )
                return _replay_response(existing, include_quota_admission=True)
        except OutboundLedgerError as exc:
            return {"error": "linear_idempotency_rejected", "reason": str(exc)}

        if (
            quota_admission_lock is None
            or _quota_admission_fd is None
            or _quota_create_context is None
        ):
            return {
                "error": "linear_policy_denied",
                "reason": "quota_admission_lock_unavailable",
            }
        try:
            if quota_admission_lock.has_unresolved_create_fences(_quota_admission_fd):
                return {
                    "error": "linear_policy_denied",
                    "reason": "quota_create_outcome_unresolved",
                }
        except FleetGlobalLockError:
            return {
                "error": "linear_policy_denied",
                "reason": "quota_admission_lock_unavailable",
            }

        try:
            current_count = await count_workspace_issues(graphql_client, quota_team_ids or frozenset())
        except Exception:
            return {
                "error": "linear_policy_denied",
                "reason": "quota_count_unavailable",
            }
        _quota_create_context["observed_current_count"] = current_count
        projected_count = current_count + 1
        if projected_count >= CRITICAL_THRESHOLD:
            quota_admission = _quota_admission(current_count)
            if retention_dry_run is None:
                return {
                    "error": "linear_policy_denied",
                    "reason": "immediate_retention_dry_run_unavailable",
                    "quota_admission": quota_admission,
                }
            try:
                retention_result = await retention_dry_run()
            except Exception:
                return {
                    "error": "linear_policy_denied",
                    "reason": "immediate_retention_dry_run_unavailable",
                    "quota_admission": quota_admission,
                }
        if projected_count >= CAPACITY:
            return {
                "error": "linear_policy_denied",
                "reason": "quota_capacity_reserved_or_exhausted",
                "quota_admission": quota_admission,
                "retention_dry_run": retention_result,
            }

    try:
        reservation = await asyncio.to_thread(
            ledger.reserve,
            operation_key=operation_key,
            tool_name=vendor_tool,
            payload=ledger_payload,
            profile_id=profile_id,
            actor_id=graph_actor,
            team_id=team_id,
        )
    except OutboundLedgerError as exc:
        if lifecycle_action != "start":
            return {"error": "linear_idempotency_rejected", "reason": str(exc)}
        try:
            legacy_reservation = await asyncio.to_thread(
                ledger.reserve,
                operation_key=operation_key,
                tool_name=vendor_tool,
                payload=forwarded,
                profile_id=profile_id,
                actor_id=graph_actor,
                team_id=team_id,
            )
        except OutboundLedgerError:
            return {"error": "linear_idempotency_rejected", "reason": str(exc)}
        if legacy_reservation.dispatch:
            return {
                "error": "linear_idempotency_rejected",
                "reason": "legacy lifecycle reservation unexpectedly required dispatch",
            }
        reservation = legacy_reservation
    if not reservation.dispatch:
        return _replay_response(
            reservation,
            include_quota_admission=is_issue_create,
        )
    if lifecycle_noop_result is not None:
        noop_status = str(lifecycle_noop_result.get("status") or "")
        noop_result_id = str(lifecycle_noop_result.get("result_id") or "")
        await asyncio.to_thread(
            ledger.mark_success,
            operation_key,
            result_id=_encode_lifecycle_noop_result(noop_status, noop_result_id),
        )
        return lifecycle_noop_result

    if lifecycle_transition is not None:
        # Linear exposes no conditional issue mutation. Re-read every mutable
        # authorization input immediately before dispatch; immutable creator
        # ownership narrows child terminal authority, while parent/delegate/state
        # drift still fails closed at this boundary.
        try:
            if lifecycle_action == "start":
                confirmed, confirmation_result = _evaluate_start_context(
                    await graphql_client.get_issue_start_context(
                        str(arguments.get("id") or "")
                    ),
                    target_team_id=team_id,
                    actor_id=graph_actor,
                )
            else:
                confirmed, confirmation_result = await _resolve_child_terminal_transition(
                    str(arguments.get("id") or ""),
                    action=lifecycle_action,
                    target_team_id=team_id,
                    actor_id=graph_actor,
                    manager_completion_allowed=profile_id == "general",
                    graphql_client=graphql_client,
                )
            confirmation_matches = confirmation_result is None and confirmed == lifecycle_transition
        except Exception:
            confirmation_matches = False
        if not confirmation_matches:
            await asyncio.to_thread(
                ledger.mark_failed,
                operation_key,
                error_code="lifecycle_pre_dispatch_changed",
            )
            return {
                "error": "linear_policy_denied",
                "reason": "lifecycle_pre_dispatch_changed",
            }
    elif plan_snapshot is not None:
        if description_evaluator is None:
            raise RuntimeError("description evaluator missing")
        try:
            confirmed, confirmation_result = description_evaluator(
                await graphql_client.get_issue_plan_context(
                    str(arguments.get("id") or "")
                ),
                target_team_id=team_id,
                actor_id=graph_actor,
                expected_updated_at=str(arguments.get("expected_updated_at") or ""),
                description=str(arguments.get("description") or ""),
            )
            confirmation_matches = confirmation_result is None and confirmed == plan_snapshot
        except Exception:
            confirmation_matches = False
        if not confirmation_matches:
            await asyncio.to_thread(
                ledger.mark_failed,
                operation_key,
                error_code="lifecycle_pre_dispatch_changed",
            )
            return {
                "error": "linear_policy_denied",
                "reason": "lifecycle_pre_dispatch_changed",
            }

    if vendor_tool == "save_comment" and str(
        arguments.get("comment_purpose") or "checkpoint"
    ) == "checkpoint":
        issue_id = str(arguments.get("issueId") or "")
        try:
            sessions = await graphql_client.get_issue_agent_sessions(issue_id)
            open_for_actor = any(
                str(session.get("app_user_id") or "") == graph_actor
                and str(session.get("status") or "")
                in {"pending", "active", "awaitingInput"}
                for session in sessions
            )
        except Exception:
            open_for_actor = True
        if open_for_actor:
            await asyncio.to_thread(
                ledger.mark_failed,
                operation_key,
                error_code="session_activity_required",
            )
            return {
                "error": "linear_policy_denied",
                "reason": "session_activity_required",
            }

    if is_issue_create:
        try:
            confirmed_count = await count_workspace_issues(graphql_client, quota_team_ids or frozenset())
            quota_unchanged = bool(
                _quota_create_context is not None
                and confirmed_count == _quota_create_context["observed_current_count"]
                and confirmed_count + 1 < CAPACITY
            )
        except Exception:
            quota_unchanged = False
        if not quota_unchanged:
            await asyncio.to_thread(
                ledger.mark_failed,
                operation_key,
                error_code="quota_pre_dispatch_changed",
            )
            return {
                "error": "linear_policy_denied",
                "reason": "quota_pre_dispatch_changed",
            }

    def persist_ambiguous_create_fence() -> None:
        if (
            is_issue_create
            and quota_admission_lock is not None
            and _quota_admission_fd is not None
            and _quota_create_context is not None
        ):
            quota_admission_lock.add_unresolved_create_fence(
                _quota_admission_fd,
                operation_key=_quota_create_context["operation_key"],
                observed_current_count=_quota_create_context["observed_current_count"],
                profile_id=_quota_create_context["profile_id"],
            )

    # Persist before dispatch so process death, cancellation, or transport loss
    # cannot release fleet capacity without a durable unresolved reservation.
    persist_ambiguous_create_fence()
    try:
        result = await mcp_client.call_tool(vendor_tool, forwarded, mutation=True)
    except MCPOutcomeUnknown as exc:
        persist_ambiguous_create_fence()
        await asyncio.to_thread(
            ledger.mark_unknown,
            operation_key,
            error_code="mcp_outcome_unknown",
        )
        return {"error": "linear_outcome_unknown", "reason": str(exc)}
    except LinearMCPToolError:
        persist_ambiguous_create_fence()
        await asyncio.to_thread(
            ledger.mark_unknown,
            operation_key,
            error_code="vendor_is_error",
        )
        return {"error": "linear_mutation_outcome_unknown", "reason": "vendor_is_error"}
    except LinearMCPError:
        persist_ambiguous_create_fence()
        await asyncio.to_thread(
            ledger.mark_unknown,
            operation_key,
            error_code="mcp_protocol_error",
        )
        return {"error": "linear_outcome_unknown", "reason": "mcp_protocol_error"}

    if lifecycle_transition is not None:
        try:
            if lifecycle_action == "start":
                read_back = await graphql_client.get_issue_start_context(
                    str(arguments.get("id") or "")
                )
                read_back_state = read_back.get("state") or {}
                accepted = (
                    str((read_back.get("team") or {}).get("id") or "") == team_id
                    and str((read_back.get("delegate") or {}).get("id") or "") == graph_actor
                    and str(read_back_state.get("id") or "")
                    == lifecycle_transition["target_state_id"]
                    and str(read_back_state.get("type") or "").casefold() == "started"
                )
            else:
                _unused, terminal_result = await _resolve_child_terminal_transition(
                    str(arguments.get("id") or ""),
                    action=lifecycle_action,
                    target_team_id=team_id,
                    actor_id=graph_actor,
                    manager_completion_allowed=profile_id == "general",
                    graphql_client=graphql_client,
                )
                expected_status = (
                    "already_completed"
                    if lifecycle_action == "complete_child"
                    else "already_canceled"
                )
                accepted = bool(
                    terminal_result
                    and terminal_result.get("status") == expected_status
                    and terminal_result.get("result_id")
                    == lifecycle_transition["target_state_id"]
                )
        except Exception:
            accepted = False
        if not accepted:
            await asyncio.to_thread(
                ledger.mark_unknown,
                operation_key,
                error_code="lifecycle_readback_mismatch",
            )
            return {
                "error": "linear_mutation_outcome_unknown",
                "reason": "lifecycle_readback_mismatch",
            }
    elif plan_snapshot is not None:
        try:
            accepted = _plan_readback_matches(
                await graphql_client.get_issue_plan_context(
                    str(arguments.get("id") or "")
                ),
                snapshot=plan_snapshot,
                expected_updated_at=str(arguments.get("expected_updated_at") or ""),
                description=str(arguments.get("description") or ""),
            )
        except Exception:
            accepted = False
        if not accepted:
            await asyncio.to_thread(
                ledger.mark_unknown,
                operation_key,
                error_code="lifecycle_readback_mismatch",
            )
            return {
                "error": "linear_mutation_outcome_unknown",
                "reason": "lifecycle_readback_mismatch",
            }

    parsed = _extract_first_json(result)
    result_id = str(parsed.get("id") or parsed.get("identifier") or "") or None
    if result_id is None:
        persist_ambiguous_create_fence()
        await asyncio.to_thread(
            ledger.mark_unknown,
            operation_key,
            error_code="mutation_result_id_missing",
        )
        return {
            "error": "linear_mutation_outcome_unknown",
            "reason": "mutation_result_id_missing",
        }
    ledger_result_id = result_id
    if quota_admission is not None and result_id is not None:
        ledger_result_id = _encode_quota_admission_result(
            result_id,
            quota_admission["current_count"],
            retention_result or {},
        )
    await asyncio.to_thread(ledger.mark_success, operation_key, result_id=ledger_result_id)
    if (
        is_issue_create
        and quota_admission_lock is not None
        and _quota_admission_fd is not None
    ):
        quota_admission_lock.resolve_own_create_fence(
            _quota_admission_fd,
            operation_key=operation_key,
            profile_id=profile_id,
        )
    response = {"status": "success", "result_id": result_id, "result": result}
    if quota_admission is not None:
        response["quota_admission"] = quota_admission
        response["immediate_retention_required"] = True
        response["retention_dry_run"] = retention_result
    return response


def _load_linear_extra() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
    except Exception:
        return {}
    return (
        (((config.get("gateway") or {}).get("platforms") or {}).get("linear") or {}).get("extra")
        or {}
    )


def _direct_instruction_context(
    profile_id: str, handler_kwargs: dict[str, Any]
) -> dict[str, str] | None:
    """Read trusted task-local gateway provenance without model-supplied prose."""
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    except ImportError:
        return None
    values = {
        "source_platform": str(get_session_env("HERMES_SESSION_PLATFORM", "")).casefold(),
        "source_user_id": str(get_session_env("HERMES_SESSION_USER_ID", "")),
        "source_message_id": str(get_session_env("HERMES_SESSION_MESSAGE_ID", "")),
        "source_session_id": str(get_session_env("HERMES_SESSION_ID", "")),
        "source_profile": str(get_session_env("HERMES_SESSION_PROFILE", "")),
    }
    chat_type = str(get_session_env("HERMES_SESSION_CHAT_TYPE", "")).casefold()
    cron_session = str(get_session_env("HERMES_CRON_SESSION", ""))
    hook_session_id = str(handler_kwargs.get("session_id") or "")
    if not (
        values["source_platform"] == "telegram"
        and chat_type == "dm"
        and not cron_session
        and all(values.values())
        and hmac.compare_digest(values["source_profile"], profile_id)
        and hook_session_id
        and hmac.compare_digest(values["source_session_id"], hook_session_id)
    ):
        return None
    return values


def _policy_from_outbound(outbound: dict[str, Any]) -> OutboundPolicy:
    return OutboundPolicy(
        expected_actor_id=str(outbound.get("expected_actor_id") or ""),
        expected_organization_id=str(outbound.get("expected_organization_id") or ""),
        allowed_team_ids=outbound.get("allowed_team_ids") or [],
        sensitive_mode=str(outbound.get("sensitive_mode") or "standard"),
        metadata_templates=outbound.get("metadata_templates") or [],
    )


def _tool_names_available(names: list[str]) -> bool:
    try:
        from tools.registry import registry
    except (ImportError, AttributeError):
        return False
    try:
        return all(registry.get_entry(name) is None for name in names)
    except Exception:
        return False


def _canonical_paths_distinct(first: str, second: str) -> bool:
    try:
        return Path(first).resolve(strict=False) != Path(second).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False


def _outbound_ledger_runtime_path_safe(database_path: str) -> bool:
    supplied = Path(database_path)
    if not supplied.is_absolute() or supplied.name in {"", ".", ".."}:
        return False
    try:
        parent = supplied.parent.resolve(strict=True)
        parent_stat = parent.stat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.getuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            return False
        try:
            entry = supplied.lstat()
        except FileNotFoundError:
            return True
        return bool(
            stat.S_ISREG(entry.st_mode)
            and not stat.S_ISLNK(entry.st_mode)
            and entry.st_uid == os.getuid()
            and stat.S_IMODE(entry.st_mode) == 0o600
            and entry.st_size > 0
        )
    except (OSError, RuntimeError, ValueError):
        return False


def register_outbound_tools(
    ctx,
    *,
    extra: dict[str, Any] | None = None,
    direct_grant_bound_callback: Callable[[str, str], None] | None = None,
) -> None:
    extra = dict(extra if extra is not None else _load_linear_extra())
    outbound = dict(extra.get("outbound_mcp") or {})
    if outbound.get("enabled") is not True:
        return
    endpoint = str(outbound.get("endpoint") or OFFICIAL_LINEAR_MCP_ENDPOINT)
    if endpoint != OFFICIAL_LINEAR_MCP_ENDPOINT:
        return
    profile_id = str(getattr(ctx, "profile_name", "") or "custom")
    if profile_id in {"health", "finance"} and outbound.get("sensitive_mode") != "metadata_only":
        return
    policy = _policy_from_outbound(outbound)
    if not policy.is_configured():
        return

    oauth_file = str(extra.get("oauth_file") or "")
    inbound_database_path = str(extra.get("database_path") or "")
    outbound_ledger_path = str(outbound.get("ledger_path") or "")
    quota_admission_lock_path = str(outbound.get("quota_admission_lock_path") or "")
    raw_quota_team_ids = outbound.get("quota_team_ids")
    quota_team_ids = (
        frozenset(raw_quota_team_ids)
        if isinstance(raw_quota_team_ids, list)
        and raw_quota_team_ids
        and all(isinstance(value, str) and value for value in raw_quota_team_ids)
        and len(set(raw_quota_team_ids)) == len(raw_quota_team_ids)
        else frozenset()
    )
    retention_team_id = str(outbound.get("quota_retention_team_id") or "")
    retention_team_key = str(outbound.get("quota_retention_team_key") or "")
    raw_retention_age = outbound.get("quota_retention_minimum_age_days")
    retention_minimum_age_days = (
        raw_retention_age
        if isinstance(raw_retention_age, int)
        and not isinstance(raw_retention_age, bool)
        and raw_retention_age > 0
        else 0
    )
    ledger_path_safe = bool(
        inbound_database_path
        and Path(inbound_database_path).is_absolute()
        and outbound_ledger_path
        and Path(outbound_ledger_path).is_absolute()
        and _canonical_paths_distinct(outbound_ledger_path, inbound_database_path)
        and _outbound_ledger_runtime_path_safe(outbound_ledger_path)
    )

    def check_fn() -> bool:
        if not oauth_file or not policy.is_configured():
            return False
        path = Path(oauth_file)
        try:
            file_stat = path.lstat()
            return bool(
                stat.S_ISREG(file_stat.st_mode)
                and not stat.S_ISLNK(file_stat.st_mode)
                and file_stat.st_uid == os.getuid()
                and not (file_stat.st_mode & 0o077)
            )
        except OSError:
            return False

    def make_handler(model_tool: str, vendor_tool: str, mutation: bool):
        async def handler(args: dict[str, Any], **handler_kwargs) -> dict[str, Any]:
            safe_args = dict(args or {})
            if mutation:
                preflight = policy.preflight(vendor_tool, safe_args)
                if preflight.action != "allow":
                    return {
                        "error": "linear_policy_denied",
                        "reason": preflight.reason,
                    }
            store = LinearOAuthStore(oauth_file)
            graphql = LinearClient(oauth_store=store)
            mcp = LinearMCPClient(store, endpoint=endpoint)
            ledger: OutboundLedger | None = None
            direct_ledger: DeliveryLedger | None = None
            quota_admission_lock: FleetGlobalLock | None = None
            try:
                if mutation:
                    ledger = await asyncio.to_thread(OutboundLedger, outbound_ledger_path)
                if vendor_tool == "save_issue" and not safe_args.get("id"):
                    try:
                        quota_admission_lock = FleetGlobalLock(quota_admission_lock_path)
                    except FleetGlobalLockError:
                        quota_admission_lock = None
                await graphql.connect()
                await mcp.connect()
                direct_context = None
                if (
                    vendor_tool == "save_issue"
                    and not safe_args.get("id")
                    and not safe_args.get("parentId")
                ):
                    direct_context = _direct_instruction_context(profile_id, handler_kwargs)
                if direct_context is not None:
                    direct_ledger = await asyncio.to_thread(
                        DeliveryLedger, inbound_database_path, startup_recovery=False
                    )
                    await asyncio.to_thread(
                        direct_ledger.reserve_direct_activation_grant,
                        operation_key=str(safe_args.get("operation_key") or ""),
                        actor_id=str(graphql.actor_id or ""),
                        team_id=str(safe_args.get("target_team_id") or ""),
                        issue_fingerprint=direct_ledger.direct_issue_fingerprint(
                            str(safe_args.get("target_team_id") or ""),
                            str(safe_args.get("title") or ""),
                        ),
                        source_platform=direct_context["source_platform"],
                        source_user_id=direct_context["source_user_id"],
                        source_message_id=direct_context["source_message_id"],
                        source_session_id=direct_context["source_session_id"],
                        source_profile=direct_context["source_profile"],
                    )
                retention_runner: Callable[[], Awaitable[dict[str, Any]]] | None = None
                if retention_team_id and retention_team_key and retention_minimum_age_days:
                    async def configured_retention_runner() -> dict[str, Any]:
                        return await _immediate_retention_dry_run(
                            graphql,
                            team_id=retention_team_id,
                            team_key=retention_team_key,
                            minimum_age_days=retention_minimum_age_days,
                        )
                    retention_runner = configured_retention_runner
                result = await execute_with_clients(
                    profile_id=profile_id,
                    vendor_tool=vendor_tool,
                    arguments=safe_args,
                    mutation=mutation,
                    policy=policy,
                    ledger=ledger,
                    quota_admission_lock=quota_admission_lock,
                    quota_team_ids=quota_team_ids,
                    retention_dry_run=retention_runner,
                    graphql_client=graphql,
                    mcp_client=mcp,
                )
                if direct_context is not None and direct_ledger is not None:
                    operation_key = str(safe_args.get("operation_key") or "")
                    result_id = str(result.get("result_id") or "")
                    if result.get("status") == "success" and result_id:
                        bound = False
                        try:
                            context = await graphql.get_issue_closure_context(result_id)
                            creator_id = str((context.get("creator") or {}).get("id") or "")
                            delegate_id = str((context.get("delegate") or {}).get("id") or "")
                            team_id = str((context.get("team") or {}).get("id") or "")
                            parent_id = str((context.get("parent") or {}).get("id") or "")
                            actor_id = str(graphql.actor_id or "")
                            authoritative = bool(
                                not parent_id
                                and hmac.compare_digest(creator_id, actor_id)
                                and hmac.compare_digest(delegate_id, actor_id)
                                and hmac.compare_digest(
                                    str(context.get("title") or ""),
                                    str(safe_args.get("title") or ""),
                                )
                                and hmac.compare_digest(
                                    team_id, str(safe_args.get("target_team_id") or "")
                                )
                            )
                            if authoritative:
                                bound = await asyncio.to_thread(
                                    direct_ledger.bind_direct_activation_grant,
                                    operation_key,
                                    result_id,
                                )
                            else:
                                await asyncio.to_thread(
                                    direct_ledger.fail_direct_activation_grant,
                                    operation_key,
                                    "direct_create_readback_policy_mismatch",
                                )
                        except Exception:
                            # The issue mutation is already durably successful. Keep the
                            # reservation recoverable by an idempotent tool replay rather
                            # than misreporting the committed vendor create as failed.
                            bound = False
                        if bound and direct_grant_bound_callback is not None:
                            try:
                                direct_grant_bound_callback(profile_id, result_id)
                            except Exception:
                                pass
                    else:
                        await asyncio.to_thread(
                            direct_ledger.fail_direct_activation_grant,
                            operation_key,
                            str(result.get("reason") or result.get("error") or "create_failed"),
                        )
                return result
            except Exception as exc:
                return {"error": "linear_tool_failed", "reason": type(exc).__name__}
            finally:
                await mcp.close()
                await graphql.close()
                if ledger is not None:
                    await asyncio.to_thread(ledger.close)
                if direct_ledger is not None:
                    await asyncio.to_thread(direct_ledger.close)

        async def registry_handler(args: dict[str, Any], **kwargs) -> str:
            result = await handler(args, **kwargs)
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return registry_handler

    configured_mutation_tools = outbound.get("allowed_mutation_tools")
    allowed_mutation_tools: list[str] = []
    known_mutation_tools = {"linear_save_issue", "linear_save_comment"}
    if (
        isinstance(configured_mutation_tools, list)
        and all(isinstance(name, str) for name in configured_mutation_tools)
        and set(configured_mutation_tools) <= known_mutation_tools
    ):
        allowed_mutation_tools = [
            name
            for name in ("linear_save_issue", "linear_save_comment")
            if name in configured_mutation_tools
        ]
    mutations_enabled = (
        outbound.get("mutations_enabled") is True
        and ledger_path_safe
        and bool(allowed_mutation_tools)
    )
    names = ["linear_get_issue", "linear_list_issues"]
    if mutations_enabled:
        names.extend(allowed_mutation_tools)
    if not _tool_names_available(names):
        return
    for name in names:
        vendor_tool, mutation = TOOL_MAP[name]
        ctx.register_tool(
            name=name,
            toolset="linear",
            schema=SCHEMAS[name],
            handler=make_handler(name, vendor_tool, mutation),
            check_fn=check_fn,
            is_async=True,
            description=SCHEMAS[name]["description"],
            emoji="◩",
        )
