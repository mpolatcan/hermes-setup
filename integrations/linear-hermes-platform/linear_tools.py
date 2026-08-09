"""Policy-gated Hermes tools that forward to Linear's official MCP."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

try:
    from .linear_client import LinearClient
    from .mcp_client import (
        OFFICIAL_LINEAR_MCP_ENDPOINT,
        LinearMCPClient,
        LinearMCPError,
        LinearMCPToolError,
        MCPOutcomeUnknown,
    )
    from .oauth_store import LinearOAuthStore
    from .outbound_ledger import OutboundLedger, OutboundLedgerError
    from .outbound_policy import OutboundPolicy, extract_linear_profile_url
except ImportError:  # Direct module loading in standalone tests/scripts.
    from linear_client import LinearClient
    from mcp_client import (
        OFFICIAL_LINEAR_MCP_ENDPOINT,
        LinearMCPClient,
        LinearMCPError,
        LinearMCPToolError,
        MCPOutcomeUnknown,
    )
    from oauth_store import LinearOAuthStore
    from outbound_ledger import OutboundLedger, OutboundLedgerError
    from outbound_policy import OutboundPolicy, extract_linear_profile_url

WRAPPER_FIELDS = frozenset(
    {"operation_key", "target_team_id", "lifecycle_action", "comment_purpose"}
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
LIFECYCLE_NOOP_STATUSES = frozenset(
    {"already_started", "already_completed", "already_canceled"}
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
                "enum": ["start", "complete_child", "cancel_child"],
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
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    target_type = "completed" if action == "complete_child" else "canceled"
    if str((context.get("team") or {}).get("id") or "") != target_team_id:
        return None, {"error": "linear_policy_denied", "reason": "authoritative_team_mismatch"}
    if str((context.get("creator") or {}).get("id") or "") != actor_id:
        return None, {"error": "linear_policy_denied", "reason": "child_creator_mismatch"}
    if str((context.get("delegate") or {}).get("id") or "") != actor_id:
        return None, {"error": "linear_policy_denied", "reason": "delegate_mismatch"}
    parent = context.get("parent") or {}
    if not str(parent.get("id") or ""):
        return None, {"error": "linear_policy_denied", "reason": "child_parent_required"}
    parent_assignee_id = str((parent.get("assignee") or {}).get("id") or "")
    if not parent_assignee_id or parent_assignee_id == actor_id:
        return None, {"error": "linear_policy_denied", "reason": "human_parent_required"}
    parent_state_type = str((parent.get("state") or {}).get("type") or "").casefold()
    if parent_state_type not in {"backlog", "unstarted", "started"}:
        if parent_state_type in {"completed", "canceled"}:
            return None, {"error": "linear_policy_denied", "reason": "parent_terminal"}
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


async def _resolve_child_terminal_transition(
    issue_id: str,
    *,
    action: str,
    target_team_id: str,
    actor_id: str,
    graphql_client: LinearClient,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    context = await graphql_client.get_issue_child_terminal_context(issue_id)
    sessions = await graphql_client.get_issue_agent_sessions(issue_id)
    open_actor_session = any(
        str(session.get("app_user_id") or "") == actor_id
        and str(session.get("status") or "") in {"pending", "active", "awaitingInput"}
        for session in sessions
    )
    return _evaluate_child_terminal_context(
        context,
        action=action,
        target_team_id=target_team_id,
        actor_id=actor_id,
        open_actor_session=open_actor_session,
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
) -> dict[str, Any]:
    derived_mutation = vendor_tool in VENDOR_MUTATION_TOOLS
    if mutation != derived_mutation:
        return {
            "error": "linear_policy_denied",
            "reason": "mutation_classification_mismatch",
        }
    mutation = derived_mutation
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
            graphql_client=graphql_client,
        )
        if lifecycle_result is not None:
            if str(lifecycle_result.get("status") or "") in LIFECYCLE_NOOP_STATUSES:
                lifecycle_noop_result = lifecycle_result
            else:
                return lifecycle_result

    forwarded = {key: value for key, value in arguments.items() if key not in WRAPPER_FIELDS}
    if lifecycle_transition is not None:
        forwarded["state"] = lifecycle_transition["target_state_id"]
    elif lifecycle_noop_result is not None:
        forwarded["state"] = str(lifecycle_noop_result.get("result_id") or "")
    ledger_payload = dict(forwarded)
    if lifecycle_action in {"start", "complete_child", "cancel_child"}:
        ledger_payload.pop("state", None)
        ledger_payload["lifecycle_action"] = lifecycle_action
    if not mutation:
        return await mcp_client.call_tool(vendor_tool, forwarded)
    if ledger is None:
        return {"error": "linear_idempotency_rejected", "reason": "ledger_not_configured"}

    operation_key = str(arguments.get("operation_key") or "")
    team_id = str(arguments.get("target_team_id") or "")
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
        lifecycle_noop_replay = _decode_lifecycle_noop_result(reservation.result_id)
        if lifecycle_noop_replay is not None:
            noop_status, noop_result_id = lifecycle_noop_replay
            return {
                "status": noop_status,
                "replayed": True,
                "result_id": noop_result_id,
            }
        return {
            "status": reservation.status,
            "replayed": True,
            "result_id": reservation.result_id,
            **({"error_code": reservation.error_code} if reservation.error_code else {}),
        }
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

    try:
        result = await mcp_client.call_tool(vendor_tool, forwarded, mutation=True)
    except MCPOutcomeUnknown as exc:
        await asyncio.to_thread(
            ledger.mark_unknown,
            operation_key,
            error_code="mcp_outcome_unknown",
        )
        return {"error": "linear_outcome_unknown", "reason": str(exc)}
    except LinearMCPToolError:
        await asyncio.to_thread(
            ledger.mark_unknown,
            operation_key,
            error_code="vendor_is_error",
        )
        return {"error": "linear_mutation_outcome_unknown", "reason": "vendor_is_error"}
    except LinearMCPError:
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

    parsed = _extract_first_json(result)
    result_id = str(parsed.get("id") or parsed.get("identifier") or "") or None
    await asyncio.to_thread(ledger.mark_success, operation_key, result_id=result_id)
    return {"status": "success", "result_id": result_id, "result": result}


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


def register_outbound_tools(ctx, *, extra: dict[str, Any] | None = None) -> None:
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
        async def handler(args: dict[str, Any], **_kwargs) -> dict[str, Any]:
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
            try:
                if mutation:
                    ledger = await asyncio.to_thread(OutboundLedger, outbound_ledger_path)
                await graphql.connect()
                await mcp.connect()
                return await execute_with_clients(
                    profile_id=profile_id,
                    vendor_tool=vendor_tool,
                    arguments=safe_args,
                    mutation=mutation,
                    policy=policy,
                    ledger=ledger,
                    graphql_client=graphql,
                    mcp_client=mcp,
                )
            except Exception as exc:
                return {"error": "linear_tool_failed", "reason": type(exc).__name__}
            finally:
                await mcp.close()
                await graphql.close()
                if ledger is not None:
                    await asyncio.to_thread(ledger.close)

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
