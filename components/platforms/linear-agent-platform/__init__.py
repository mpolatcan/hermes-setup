"""Hermes plugin entry point for the native Linear platform."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import threading
import weakref
from typing import Any


LINEAR_HOME_CHANNEL_ENV = "LINEAR_HOME_CHANNEL"
logger = logging.getLogger(__name__)
_yaml_home_channel_owned = False
_yaml_home_channel_value: str | None = None
_progress_adapters: weakref.WeakSet[Any] = weakref.WeakSet()
_progress_lock = threading.RLock()
_progress_seen: dict[tuple[str, ...], None] = {}
_progress_routes: dict[str, dict[str, Any]] = {}
_pending_progress: list[tuple[str, str, str]] = []
_PENDING_PROGRESS_LIMIT = 32
_PROGRESS_ROUTE_LIMIT = 256
_SEMANTIC_PROGRESS_PER_TURN = 3
_SEMANTIC_PROGRESS_MAX_CHARS = 500
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?(?:key|token)|access[_-]?token|auth(?:orization)?|password|secret|token)"
    r'''\b\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)'''
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_CHANNEL_COMMAND_RE = re.compile(
    r"^\s*(?:https://linear\.app/[^/\s]+/issue/)?"
    r"(?P<issue>[A-Z][A-Z0-9]*-\d+)"
    r"(?:/[^\s]*)?\s*(?::|—|-)\s*(?P<body>\S(?:.|\n)*)$"
)


def _parse_channel_command(text: str) -> tuple[str, str] | None:
    """Parse only an explicit, leading Linear issue command."""
    match = _CHANNEL_COMMAND_RE.fullmatch(str(text or ""))
    if match is None:
        return None
    issue_ref = match.group("issue").upper()
    body = match.group("body").strip()
    return (issue_ref, body) if body else None


def _source_operation_key(
    platform: str,
    chat_id: str,
    thread_id: str,
    message_id: str,
    profile: str = "",
    relay_discriminator: str = "",
    scope_id: str = "",
    user_id: str = "",
) -> str:
    material = "\0".join(
        (
            profile,
            relay_discriminator,
            platform,
            scope_id,
            user_id,
            chat_id,
            thread_id,
            message_id,
        )
    )
    return "channel-route:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").casefold()


def _source_profile(source: Any) -> str:
    value = getattr(source, "profile", None)
    return value.strip() if isinstance(value, str) else ""


def _source_identity_value(source: Any, name: str) -> str:
    value = getattr(source, name, None)
    return str(value) if isinstance(value, (str, int)) else ""


def _linear_adapter_for_gateway(gateway: Any, source: Any) -> Any | None:
    profile = _source_profile(source)
    resolver = getattr(gateway, "_authorization_adapter", None)
    if callable(resolver) and not type(resolver).__module__.startswith("unittest.mock"):
        try:
            from gateway.config import Platform  # type: ignore[import-not-found]

            return resolver(Platform("linear"), profile or None)
        except Exception:
            logger.exception("[linear] Profile-aware adapter resolution failed")
            return None
    if profile:
        active_profile_fn = getattr(gateway, "_active_profile_name", None)
        try:
            active_profile = active_profile_fn() if callable(active_profile_fn) else None
        except Exception:
            active_profile = None
        if profile == active_profile:
            adapters = getattr(gateway, "adapters", None) or {}
        else:
            profile_maps = getattr(gateway, "_profile_adapters", None) or {}
            adapters = profile_maps.get(profile)
            if not isinstance(adapters, dict):
                return None
    else:
        adapters = getattr(gateway, "adapters", None) or {}
    for key, adapter in adapters.items():
        name = str(getattr(key, "value", key) or "").casefold()
        if name == "linear":
            return adapter
    return None


async def _send_source_message(gateway: Any, event: Any, content: str) -> bool:
    source = event.source
    adapter = gateway._adapter_for_source(source)
    if adapter is None:
        return False
    reply_to = str(event.message_id or "") or None
    metadata: dict[str, Any] = {}
    thread_id = str(getattr(source, "thread_id", None) or "")
    if thread_id:
        metadata["thread_id"] = thread_id
    platform_name = _platform_name(source)
    scope_id = _source_identity_value(source, "scope_id")
    if platform_name == "slack" and scope_id:
        metadata["slack_team_id"] = scope_id
    if platform_name == "telegram" and getattr(source, "chat_type", None) == "dm":
        metadata["telegram_dm_topic_reply_fallback"] = True
        if thread_id not in {"", "1"}:
            metadata["direct_messages_topic_id"] = thread_id
        if reply_to:
            metadata["telegram_reply_to_message_id"] = reply_to
    if getattr(source, "delivered_via_upstream_relay", False) is True:
        metadata["_relay_logical_platform"] = platform_name
        if scope_id:
            metadata["scope_id"] = scope_id
        user_id = _source_identity_value(source, "user_id")
        if user_id:
            metadata["user_id"] = user_id
    send_kwargs: dict[str, Any] = {
        "chat_id": str(source.chat_id),
        "content": content,
        "reply_to": reply_to,
        "metadata": metadata or None,
    }
    result = await adapter.send(**send_kwargs)
    return not (result is not None and getattr(result, "success", True) is False)


async def _report_unreserved_command(gateway: Any, event: Any, issue_ref: str) -> None:
    await _send_source_message(
        gateway,
        event,
        f"{issue_ref} komutu stable source message ID veya durable Linear routing state "
        "bulunmadığı için alınamadı; "
        "kanonik Linear routing başlatılmadı ve kaynak kanalda çalıştırılmadı.",
    )


def _pre_gateway_dispatch(*, event: Any, gateway: Any, **_kwargs: Any) -> dict[str, str] | None:
    parsed = _parse_channel_command(str(getattr(event, "text", None) or ""))
    if parsed is None or _platform_name(event.source) == "linear":
        return None
    try:
        if not gateway._is_user_authorized(event.source):
            return None
    except Exception:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    issue_ref, command = parsed
    source = event.source
    linear_adapter = _linear_adapter_for_gateway(gateway, source)
    message_id = str(getattr(event, "message_id", None) or "")
    reserve = getattr(linear_adapter, "reserve_channel_route", None)
    if not message_id or not callable(reserve):
        loop.create_task(_report_unreserved_command(gateway, event, issue_ref))
        return {"action": "skip", "reason": "linear_canonical_route_unavailable"}
    platform = _platform_name(source)
    chat_id = str(getattr(source, "chat_id", None) or "")
    thread_id = str(getattr(source, "thread_id", None) or "")
    profile = _source_profile(source)
    via_relay = getattr(source, "delivered_via_upstream_relay", False) is True
    scope_id = _source_identity_value(source, "scope_id")
    user_id = _source_identity_value(source, "user_id")
    operation_key = _source_operation_key(
        platform,
        chat_id,
        thread_id,
        message_id,
        profile,
        "relay" if via_relay else "native",
        scope_id,
        user_id,
    )
    try:
        reserve(
            operation_key=operation_key,
            source_platform=platform,
            source_chat_id=chat_id,
            source_thread_id=thread_id,
            source_message_id=message_id,
            source_user_id=user_id,
            source_user_name=str(getattr(source, "user_name", None) or ""),
            source_chat_type=str(getattr(source, "chat_type", None) or "dm"),
            source_profile=profile,
            source_scope_id=scope_id,
            source_via_relay=via_relay,
            issue_ref=issue_ref,
            command_text=command,
        )
    except Exception:
        logger.exception("[linear] Could not durably reserve cross-channel command")
        loop.create_task(_report_unreserved_command(gateway, event, issue_ref))
        return {"action": "skip", "reason": "linear_canonical_route_unavailable"}
    return {"action": "skip", "reason": "linear_canonical_route"}


def _apply_yaml_config(_yaml_cfg: dict, linear_cfg: dict) -> None:
    """Bridge a configured Linear home AgentSession into cron target resolution."""
    global _yaml_home_channel_owned, _yaml_home_channel_value

    current = os.environ.get(LINEAR_HOME_CHANNEL_ENV)
    if _yaml_home_channel_owned:
        if current != _yaml_home_channel_value:
            _yaml_home_channel_owned = False
            _yaml_home_channel_value = None
            if current:
                return
    elif current:
        return

    home_channel = linear_cfg.get("home_channel")
    chat_id = home_channel.get("chat_id") if isinstance(home_channel, dict) else None
    normalized = chat_id.strip() if isinstance(chat_id, str) else ""
    if normalized:
        os.environ[LINEAR_HOME_CHANNEL_ENV] = normalized
        _yaml_home_channel_owned = True
        _yaml_home_channel_value = normalized
        return

    if _yaml_home_channel_owned:
        os.environ.pop(LINEAR_HOME_CHANNEL_ENV, None)
    _yaml_home_channel_owned = False
    _yaml_home_channel_value = None


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
    caption=None,
):
    """Deliver through Linear's durable outbox without binding the webhook port."""
    del thread_id, force_document, caption
    if media_files:
        return {"error": "Linear standalone delivery does not support local media files"}

    from .adapter import LinearPlatformAdapter

    adapter = None
    try:
        adapter = LinearPlatformAdapter.from_config(pconfig)
        if not await adapter.connect_outbound_only():
            connect_error = adapter.last_connect_error
            return {
                "error": "Linear outbound-only adapter failed to connect",
                "retryable": bool(connect_error and connect_error.retryable),
            }
        result = await adapter.send(str(chat_id), str(message or ""))
        if not result.success:
            return {
                "error": result.error or "Linear send failed",
                "retryable": result.retryable,
            }
        return {
            "success": True,
            "platform": "linear",
            "chat_id": str(chat_id),
            "message_id": result.message_id,
            "note": f"Sent to linear target (chat_id: {chat_id})",
        }
    except Exception:
        return {"error": "Linear standalone send failed"}
    finally:
        if adapter is not None:
            try:
                await adapter.disconnect()
            except Exception:
                logger.warning("[linear] Standalone adapter cleanup failed")


def _linear_adapter_factory(config: Any) -> Any:
    from .adapter import LinearPlatformAdapter

    adapter = LinearPlatformAdapter.from_config(config)
    adapter._terminal_progress_callback = _fence_terminal_progress
    _progress_adapters.add(adapter)
    return adapter


def _progress_adapter(profile: str = "") -> Any | None:
    """Return the profile-matching live adapter, failing closed on ambiguity."""
    candidates = list(_progress_adapters)
    for adapter in candidates:
        runner = getattr(adapter, "gateway_runner", None)
        resolver = getattr(runner, "_authorization_adapter", None)
        platform = getattr(adapter, "platform", None)
        if callable(resolver) and platform is not None:
            try:
                resolved = resolver(platform, profile or None)
                if resolved is not None:
                    return resolved
            except Exception:
                logger.warning("[linear] Tool progress adapter resolution failed", exc_info=True)
    return candidates[0] if len(candidates) == 1 else None


def _tool_progress_label(tool_name: str) -> str:
    """Map only the trusted tool name; arguments/results are never rendered."""
    name = str(tool_name or "").strip().lower()
    if name in {"terminal", "execute_code", "process"}:
        return "Sistem kontrolü yürütülüyor"
    if name in {"read_file", "search_files", "web_extract", "web_search"}:
        return "Kaynaklar inceleniyor"
    if name in {"patch", "write_file"}:
        return "Değişiklik uygulanıyor"
    return "İşlem yürütülüyor"


def _reset_progress_state_for_tests() -> None:
    """Reset process-local observer state; production state is lifetime-bounded."""
    with _progress_lock:
        _progress_seen.clear()
        _progress_routes.clear()
        _pending_progress.clear()


def _sanitize_interim_text(text: Any) -> str:
    """Return bounded observer text with common credential forms removed."""
    if not isinstance(text, str):
        return ""
    visible = " ".join(text.replace("\x00", " ").split()).strip()
    if not visible:
        return ""
    visible = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", visible)
    visible = _BEARER_RE.sub("Bearer [REDACTED]", visible)
    from agent.redact import redact_sensitive_text

    visible = redact_sensitive_text(
        visible,
        force=True,
        redact_url_credentials=True,
    )
    if len(visible) > _SEMANTIC_PROGRESS_MAX_CHARS:
        visible = visible[: _SEMANTIC_PROGRESS_MAX_CHARS - 1].rstrip() + "…"
    return visible


def _trim_progress_state_locked() -> None:
    while len(_progress_routes) > _PROGRESS_ROUTE_LIMIT:
        oldest_session_id = next(iter(_progress_routes))
        _progress_routes.pop(oldest_session_id, None)
        _pending_progress[:] = [
            item for item in _pending_progress if item[0] != oldest_session_id
        ]
    while len(_progress_seen) > 512:
        _progress_seen.pop(next(iter(_progress_seen)))


def _bind_progress_route(
    session_id: str,
    profile: str,
    chat_id: str,
    turn_key: str,
    *,
    adapter: Any | None = None,
) -> list[tuple[str, str, str]]:
    """Bind a trusted main turn and take its hook-before-route messages."""
    open_progress_turn = getattr(adapter, "open_progress_turn", None)
    if callable(open_progress_turn):
        open_progress_turn(chat_id, turn_key)
    with _progress_lock:
        prior = _progress_routes.get(session_id)
        is_new_turn = prior is None or prior.get("turn_key") != turn_key
        _progress_routes[session_id] = {
            "profile": profile,
            "chat_id": chat_id,
            "turn_key": turn_key,
            "adapter": adapter,
            "fenced": False if is_new_turn else bool(prior.get("fenced")),
            "semantic_count": 0 if is_new_turn else int(prior.get("semantic_count", 0)),
        }
        matching = [
            item for item in _pending_progress
            if item[0] == session_id and item[1] == turn_key
        ]
        _pending_progress[:] = [
            item for item in _pending_progress if item[0] != session_id
        ]
        _trim_progress_state_locked()
        return matching


def _fence_terminal_progress(
    chat_id: str, *, turn_key: str = "", adapter: Any | None = None
) -> None:
    """Fence every route for a durably accepted terminal Linear response."""
    with _progress_lock:
        for route in _progress_routes.values():
            if route.get("chat_id") != chat_id:
                continue
            route_adapter = route.get("adapter")
            if adapter is not None and route_adapter is not None and route_adapter is not adapter:
                continue
            if turn_key and route.get("turn_key") != turn_key:
                continue
            route["fenced"] = True


def _schedule_semantic_progress(
    session_id: str, turn_key: str, text: str
) -> None:
    digest = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()
    seen_key = ("semantic", session_id, turn_key, digest)
    with _progress_lock:
        route = _progress_routes.get(session_id)
        if (
            route is None
            or route.get("turn_key") != turn_key
            or route.get("fenced") is True
            or seen_key in _progress_seen
            or int(route.get("semantic_count", 0)) >= _SEMANTIC_PROGRESS_PER_TURN
        ):
            return
        adapter = route.get("adapter") or _progress_adapter(str(route.get("profile") or ""))
        if adapter is None:
            return
        route["semantic_count"] = int(route.get("semantic_count", 0)) + 1
        _progress_seen[seen_key] = None
        _trim_progress_state_locked()
        chat_id = str(route.get("chat_id") or "")
    try:
        adapter.schedule_tool_progress(
            chat_id,
            text,
            turn_key=turn_key,
            progress_kind="semantic",
        )
    except RuntimeError:
        with _progress_lock:
            _progress_seen.pop(seen_key, None)
            current = _progress_routes.get(session_id)
            if current is route:
                current["semantic_count"] = max(
                    0, int(current.get("semantic_count", 0)) - 1
                )
        logger.warning("[linear] Semantic progress scheduling failed", exc_info=True)


def _on_interim_message(
    *,
    text: Any = None,
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    **_kwargs: Any,
) -> None:
    """Observe provider-independent visible interim text, never tool payloads."""
    normalized_session_id = str(session_id or "").strip()
    turn_key = str(turn_id or api_request_id or "").strip()
    visible = _sanitize_interim_text(text)
    if not normalized_session_id or not turn_key or not visible:
        return None
    with _progress_lock:
        route = _progress_routes.get(normalized_session_id)
        if route is None or route.get("turn_key") != turn_key:
            _pending_progress.append((normalized_session_id, turn_key, visible))
            if len(_pending_progress) > _PENDING_PROGRESS_LIMIT:
                del _pending_progress[: len(_pending_progress) - _PENDING_PROGRESS_LIMIT]
            return None
    _schedule_semantic_progress(normalized_session_id, turn_key, visible)
    return None


def _pre_tool_progress(**kwargs: Any) -> None:
    """Publish one secret-safe ephemeral thought when a Linear tool starts."""
    try:
        from gateway.session_context import get_session_env

        platform = str(
            kwargs.get("platform")
            or get_session_env("HERMES_SESSION_PLATFORM", "")
        ).strip().lower()
        if platform != "linear":
            return None
        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "")).strip()
        profile = str(get_session_env("HERMES_SESSION_PROFILE", "")).strip()
        bound_session_id = str(get_session_env("HERMES_SESSION_ID", "")).strip()
        hook_session_id = str(kwargs.get("session_id") or "").strip()
        if not bound_session_id or not hook_session_id or not hmac.compare_digest(
            bound_session_id, hook_session_id
        ):
            return None
        turn_key = str(kwargs.get("turn_id") or kwargs.get("api_request_id") or "").strip()
        adapter = _progress_adapter(profile)
        if not chat_id or not turn_key or adapter is None:
            return None
    except (ImportError, RuntimeError):
        return None

    pending = _bind_progress_route(
        hook_session_id, profile, chat_id, turn_key, adapter=adapter
    )
    with _progress_lock:
        route = _progress_routes.get(hook_session_id)
        if route is None or route.get("fenced") is True:
            return None
        label = _tool_progress_label(str(kwargs.get("tool_name") or ""))
        dedupe_key = ("tool", profile, chat_id, turn_key, label)
        if dedupe_key in _progress_seen:
            should_schedule_tool = False
        else:
            _progress_seen[dedupe_key] = None
            _trim_progress_state_locked()
            should_schedule_tool = True

    if should_schedule_tool:
        try:
            adapter.schedule_tool_progress(chat_id, label, turn_key=turn_key)
        except RuntimeError:
            with _progress_lock:
                _progress_seen.pop(dedupe_key, None)
            logger.warning("[linear] Tool progress scheduling failed", exc_info=True)
    for _pending_session_id, _pending_turn_key, pending_text in pending:
        _schedule_semantic_progress(_pending_session_id, _pending_turn_key, pending_text)
    return None


def register(ctx) -> None:
    from .adapter import LinearPlatformAdapter
    from .linear_tools import register_outbound_tools

    ctx.register_platform(
        name="linear",
        label="Linear",
        adapter_factory=_linear_adapter_factory,
        check_fn=LinearPlatformAdapter.check_requirements,
        validate_config=LinearPlatformAdapter.validate_config,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var=LINEAR_HOME_CHANNEL_ENV,
        standalone_sender_fn=_standalone_send,
        emoji="◩",
        pii_safe=True,
        allow_update_command=False,
        max_message_length=0,
        platform_hint=(
            "You are replying inside a Linear Agent Session. Put the complete user-facing result "
            "in the final response; it will be posted back to Linear as Markdown. Do not promise "
            "Telegram delivery. Do not emit local MEDIA paths; use durable links when files matter."
        ),
    )
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
        register_hook("pre_tool_call", _pre_tool_progress)
        register_hook("on_interim_message", _on_interim_message)
    register_outbound_tools(ctx)
