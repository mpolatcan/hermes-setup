"""Hermes plugin entry point for the native Linear platform."""

from __future__ import annotations

import logging
import os


LINEAR_HOME_CHANNEL_ENV = "LINEAR_HOME_CHANNEL"
logger = logging.getLogger(__name__)
_yaml_home_channel_owned = False
_yaml_home_channel_value: str | None = None


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
            return {"error": "Linear outbound-only adapter failed to connect"}
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


def register(ctx) -> None:
    from .adapter import LinearPlatformAdapter
    from .linear_tools import register_outbound_tools

    ctx.register_platform(
        name="linear",
        label="Linear",
        adapter_factory=LinearPlatformAdapter.from_config,
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
    register_outbound_tools(ctx)
