"""Hermes plugin entry point for the native Linear platform."""

from __future__ import annotations


def register(ctx) -> None:
    from .adapter import LinearPlatformAdapter
    from .linear_tools import register_outbound_tools

    ctx.register_platform(
        name="linear",
        label="Linear",
        adapter_factory=LinearPlatformAdapter.from_config,
        check_fn=LinearPlatformAdapter.check_requirements,
        validate_config=LinearPlatformAdapter.validate_config,
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
