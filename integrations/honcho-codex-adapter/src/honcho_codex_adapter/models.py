from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    max_completion_tokens: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    stop: str | list[str] | None = None

    @model_validator(mode="after")
    def reject_unsupported(self) -> "ChatCompletionRequest":
        if self.stop not in (None, [], ""):
            raise ValueError("stop is not supported by the Codex backend")
        return self
