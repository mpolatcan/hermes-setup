"""Hermes plugin entry point for the native Linear platform."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import weakref
from typing import Any


LINEAR_HOME_CHANNEL_ENV = "LINEAR_HOME_CHANNEL"
logger = logging.getLogger(__name__)
_yaml_home_channel_owned = False
_yaml_home_channel_value: str | None = None
_progress_adapters: weakref.WeakSet[Any] = weakref.WeakSet()
_progress_seen: dict[tuple[str, str, str, str], None] = {}
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
        if bound_session_id and hook_session_id and not hmac.compare_digest(
            bound_session_id, hook_session_id
        ):
            return None
        turn_key = str(kwargs.get("turn_id") or kwargs.get("api_request_id") or "").strip()
        adapter = _progress_adapter(profile)
        if not chat_id or not turn_key or adapter is None:
            return None
    except (ImportError, RuntimeError):
        return None

    label = _tool_progress_label(str(kwargs.get("tool_name") or ""))
    dedupe_key = (profile, chat_id, turn_key, label)
    if dedupe_key in _progress_seen:
        return None

    try:
        adapter.schedule_tool_progress(chat_id, label, turn_key=turn_key)
    except RuntimeError:
        logger.warning("[linear] Tool progress scheduling failed", exc_info=True)
        return None
    _progress_seen[dedupe_key] = None
    while len(_progress_seen) > 256:
        _progress_seen.pop(next(iter(_progress_seen)))
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
    register_outbound_tools(ctx)
