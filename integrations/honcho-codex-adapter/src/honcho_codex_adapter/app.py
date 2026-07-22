from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .driver import AdapterError, DriverResult, HermesCodexDriver, MODEL_ALIASES
from .models import ChatCompletionRequest
from .scheduler import WeightedPriorityScheduler


class Driver(Protocol):
    def complete(
        self,
        request: ChatCompletionRequest,
        cancel_event: threading.Event | None = None,
    ) -> DriverResult: ...


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": code, "code": code}})


def _stream_payloads(result: DriverResult, include_usage: bool):
    sse_end = chr(10) + chr(10)
    base = {"id": result.id, "object": "chat.completion.chunk", "created": result.created, "model": result.model}
    first = {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    yield f"data: {json.dumps(first, separators=(',', ':'))}{sse_end}"
    delta: dict[str, Any] = {}
    if result.content is not None:
        delta["content"] = result.content
    if result.tool_calls:
        delta["tool_calls"] = [dict(index=i, **call) for i, call in enumerate(result.tool_calls)]
    if delta:
        body = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        yield f"data: {json.dumps(body, separators=(',', ':'))}{sse_end}"
    final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": result.finish_reason}]}
    if include_usage:
        final["usage"] = result.usage
    yield f"data: {json.dumps(final, separators=(',', ':'))}{sse_end}"
    yield f"data: [DONE]{sse_end}"


def create_app(
    driver: Driver | None = None,
    api_key: str | None = None,
    queue_capacity: int | None = None,
    queue_aging_seconds: float | None = None,
) -> FastAPI:
    resolved_key = api_key if api_key is not None else os.getenv("HONCHO_CODEX_ADAPTER_API_KEY", "")
    if not resolved_key:
        raise RuntimeError("HONCHO_CODEX_ADAPTER_API_KEY is required")
    if queue_capacity is None:
        try:
            queue_capacity = int(os.getenv("HONCHO_CODEX_QUEUE_CAPACITY", "8"))
        except ValueError as exc:
            raise RuntimeError("HONCHO_CODEX_QUEUE_CAPACITY must be an integer") from exc
    if queue_capacity < 1:
        raise RuntimeError("HONCHO_CODEX_QUEUE_CAPACITY must be at least 1")
    if queue_aging_seconds is None:
        try:
            queue_aging_seconds = float(os.getenv("HONCHO_CODEX_QUEUE_AGING_SECONDS", "30"))
        except ValueError as exc:
            raise RuntimeError("HONCHO_CODEX_QUEUE_AGING_SECONDS must be a number") from exc
    if queue_aging_seconds <= 0:
        raise RuntimeError("HONCHO_CODEX_QUEUE_AGING_SECONDS must be greater than 0")
    app = FastAPI(title="Honcho Codex Adapter", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.driver = driver or HermesCodexDriver()
    app.state.scheduler = WeightedPriorityScheduler(
        queue_capacity,
        aging_seconds=queue_aging_seconds,
    )

    async def require_auth(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {resolved_key}":
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models", dependencies=[Depends(require_auth)])
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "honcho-codex-adapter"} for model in MODEL_ALIASES]}

    @app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])
    async def completions(payload: ChatCompletionRequest, http_request: Request):
        lease = await app.state.scheduler.try_acquire(payload.model)
        if lease is None:
            return _error("adapter queue is full", "queue_full", 503)
        cancel_event = threading.Event()
        try:
            async with lease:
                try:
                    completion_task = asyncio.create_task(
                        asyncio.to_thread(app.state.driver.complete, payload, cancel_event)
                    )
                    try:
                        while not completion_task.done():
                            await asyncio.sleep(0.1)
                            if await http_request.is_disconnected():
                                cancel_event.set()
                                break
                        result = await asyncio.shield(completion_task)
                    except asyncio.CancelledError:
                        cancel_event.set()
                        try:
                            await asyncio.shield(completion_task)
                        except (Exception, asyncio.CancelledError):
                            pass
                        raise
                except AdapterError as exc:
                    return _error(str(exc), exc.code, exc.status_code)
                except Exception:
                    return _error("internal adapter error", "internal_error", 500)
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        if not payload.stream:
            return result.as_chat_completion()
        include_usage = bool((payload.stream_options or {}).get("include_usage"))
        return StreamingResponse(_stream_payloads(result, include_usage), media_type="text/event-stream")

    return app
