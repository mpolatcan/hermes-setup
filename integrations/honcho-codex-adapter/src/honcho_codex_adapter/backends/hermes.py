from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import openai

from ..compat import HermesContract, load_hermes_contract


@dataclass(slots=True)
class HermesBackendResult:
    final: Any
    normalized: Any


class HermesBackend:
    """The only runtime boundary allowed to execute Hermes private contracts."""

    def __init__(self, contract: HermesContract | None = None) -> None:
        self._contract = contract

    def _runtime(self) -> HermesContract:
        return self._contract or load_hermes_contract()

    def invoke(
        self,
        kwargs: dict[str, Any],
        *,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        cancel_event: threading.Event | None,
    ) -> HermesBackendResult:
        runtime = self._runtime()
        client = None
        event_stream = None
        try:
            creds = runtime.resolve_credentials(refresh_if_expiring=True)
            token = str(creds.get("api_key") or "").strip()
            base_url = str(creds.get("base_url") or "").strip().rstrip("/")
            if not token or not base_url:
                error = RuntimeError("Codex credentials are unavailable")
                error.status_code = 401  # type: ignore[attr-defined]
                raise error
            client = openai.OpenAI(
                api_key=token,
                base_url=base_url,
                default_headers=runtime.cloudflare_headers(token),
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
            event_stream = client.responses.create(**kwargs, stream=True)
            final = runtime.consume_event_stream(
                event_stream,
                model=model,
                interrupt_check=cancel_event.is_set if cancel_event is not None else None,
            )
            cancelled = cancel_event is not None and cancel_event.is_set()
            normalized = (
                runtime.responses_transport().normalize_response(final, issuer_kind="codex")
                if final and not cancelled
                else None
            )
            return HermesBackendResult(final=final, normalized=normalized)
        finally:
            if event_stream is not None:
                close = getattr(event_stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
