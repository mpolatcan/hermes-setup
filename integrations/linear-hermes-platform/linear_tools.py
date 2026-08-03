"""Policy-gated Hermes tools that forward to Linear's official MCP."""

from __future__ import annotations

import asyncio
import hashlib
import io
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
    from .outbound_policy import OutboundPolicy
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
    from outbound_policy import OutboundPolicy

WRAPPER_FIELDS = frozenset({"operation_key", "target_team_id", "approval_reference"})
TOOL_MAP = {
    "linear_get_issue": ("get_issue", False),
    "linear_list_issues": ("list_issues", False),
    "linear_save_issue": ("save_issue", True),
    "linear_save_comment": ("save_comment", True),
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
            "state": {"type": "string"},
            "priority": {"type": "number"},
            "assignee": {"type": "string"},
            "delegate": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "project": {"type": "string"},
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
            "approval_reference": {"type": "string"},
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
            "approval_reference": {"type": "string"},
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

    forwarded = {key: value for key, value in arguments.items() if key not in WRAPPER_FIELDS}
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
            payload=forwarded,
            profile_id=profile_id,
            actor_id=graph_actor,
            team_id=team_id,
        )
    except OutboundLedgerError as exc:
        return {"error": "linear_idempotency_rejected", "reason": str(exc)}
    if not reservation.dispatch:
        return {
            "status": reservation.status,
            "replayed": True,
            "result_id": reservation.result_id,
            **({"error_code": reservation.error_code} if reservation.error_code else {}),
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


def _approval_runtime_supported() -> bool:
    try:
        from hermes_cli.plugins import resolve_pre_tool_block
        from tools.approval import request_tool_approval
    except (ImportError, AttributeError):
        return False
    return callable(resolve_pre_tool_block) and callable(request_tool_approval)


def _human_approval_config_safe() -> bool:
    try:
        from hermes_cli.config import get_config_path
        from utils import fast_safe_load

        config_path = Path(get_config_path())
        path_stat = config_path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(config_path, flags)
        try:
            opened_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_uid != os.getuid()
                or opened_stat.st_mode & 0o022
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
                or opened_stat.st_size > 1_048_576
            ):
                return False
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 1_048_576:
                    return False
                chunks.append(chunk)
            final_stat = os.fstat(fd)
        finally:
            os.close(fd)
        final_path_stat = config_path.lstat()
        identity = (opened_stat.st_dev, opened_stat.st_ino)
        if (
            identity != (final_stat.st_dev, final_stat.st_ino)
            or identity != (final_path_stat.st_dev, final_path_stat.st_ino)
            or opened_stat.st_mtime_ns != final_stat.st_mtime_ns
            or opened_stat.st_size != final_stat.st_size
            or total != final_stat.st_size
        ):
            return False
        config = fast_safe_load(io.StringIO(b"".join(chunks).decode("utf-8"))) or {}
    except Exception:
        return False
    if not isinstance(config, dict):
        return False
    approvals = config.get("approvals") or {}
    if not isinstance(approvals, dict):
        return False
    if "mode" not in approvals or "cron_mode" not in approvals:
        return False
    mode = approvals.get("mode")
    cron_mode = approvals.get("cron_mode")
    return mode == "manual" and cron_mode == "deny"


def _approval_bypass_active() -> bool:
    try:
        from tools.approval import is_approval_bypass_active
    except (ImportError, AttributeError):
        return True
    try:
        return bool(is_approval_bypass_active())
    except Exception:
        return True


def _tool_names_available(names: list[str]) -> bool:
    try:
        from tools.registry import registry
    except (ImportError, AttributeError):
        return False
    try:
        return all(registry.get_entry(name) is None for name in names)
    except Exception:
        return False


def _approval_request_parts(
    *, tool_name: str, profile_id: str, args: dict[str, Any]
) -> tuple[str, str]:
    operation_key = str(args.get("operation_key") or "missing-operation-key")
    target_team_id = str(args.get("target_team_id") or "unspecified-team")
    operation_hash = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
    message = (
        f"Approve {tool_name} for profile {profile_id} and team {target_team_id}. "
        "Content is intentionally hidden."
    )
    return message, f"linear-mcp:{tool_name}:{operation_hash}"


def _request_tool_approval_sync(tool_name: str, message: str, rule_key: str) -> dict[str, Any]:
    from tools.approval import request_tool_approval

    result = request_tool_approval(tool_name, message, rule_key=rule_key)
    return result if isinstance(result, dict) else {}


async def _request_mutation_approval(
    *, tool_name: str, profile_id: str, args: dict[str, Any]
) -> bool:
    message, rule_key = _approval_request_parts(
        tool_name=tool_name,
        profile_id=profile_id,
        args=args,
    )
    try:
        result = await asyncio.to_thread(
            _request_tool_approval_sync,
            tool_name,
            message,
            rule_key,
        )
    except Exception:
        return False
    return result.get("approved") is True


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
        and Path(outbound_ledger_path).resolve(strict=False)
        != Path(inbound_database_path).resolve(strict=False)
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
                if not _human_approval_config_safe() or _approval_bypass_active():
                    return {
                        "error": "linear_policy_denied",
                        "reason": "approval_policy_unsafe",
                    }
                if not await _request_mutation_approval(
                    tool_name=model_tool,
                    profile_id=profile_id,
                    args=safe_args,
                ):
                    return {
                        "error": "linear_policy_denied",
                        "reason": "human_approval_required",
                    }
                if not _human_approval_config_safe() or _approval_bypass_active():
                    return {
                        "error": "linear_policy_denied",
                        "reason": "approval_policy_changed",
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

        return handler

    mutations_enabled = (
        outbound.get("mutations_enabled") is True
        and ledger_path_safe
        and _approval_runtime_supported()
        and _human_approval_config_safe()
    )
    names = ["linear_get_issue", "linear_list_issues"]
    if mutations_enabled:
        names.extend(["linear_save_issue", "linear_save_comment"])
    if not _tool_names_available(names):
        return
    if mutations_enabled:
        def require_mutation_approval(
            tool_name: str = "",
            args: dict[str, Any] | None = None,
            **_kwargs,
        ) -> dict[str, str] | None:
            if tool_name not in {"linear_save_issue", "linear_save_comment"}:
                return None
            if not _human_approval_config_safe():
                return {
                    "action": "block",
                    "message": "Linear mutation approval policy is not safely configured.",
                }
            if _approval_bypass_active():
                return {
                    "action": "block",
                    "message": "Linear mutations are disabled while approval bypass is active.",
                }
            return None

        # Register the gate before exposing either mutation tool. If hook
        # registration fails, no mutation tool can remain partially exposed.
        ctx.register_hook("pre_tool_call", require_mutation_approval)

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
