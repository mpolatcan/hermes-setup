"""Native Linear Agent Session platform adapter for Hermes Gateway."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from aiohttp import web

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)

from .ledger import DeliveryLedger
from .linear_client import LinearAPIError, LinearClient

logger = logging.getLogger(__name__)

_WEBHOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,200}$")
_ALLOWED_ACTIONS = {"created", "prompted"}


def _read_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[key.strip()] = value
    return result


def _payload_timestamp_seconds(payload: dict[str, Any]) -> float | None:
    raw = payload.get("webhookTimestamp")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 10_000_000_000:
        value /= 1000.0
    return value


def _organization_id(payload: dict[str, Any]) -> str | None:
    direct = payload.get("organizationId")
    if direct:
        return str(direct)
    organization = payload.get("organization")
    if isinstance(organization, dict) and organization.get("id"):
        return str(organization["id"])
    return None


def _actor(payload: dict[str, Any]) -> tuple[str, str]:
    actor = payload.get("actor")
    if not isinstance(actor, dict):
        actor = payload.get("user")
    if not isinstance(actor, dict):
        actor = {}
    actor_id = str(actor.get("id") or "linear-user")
    actor_name = str(actor.get("name") or actor.get("displayName") or "Linear user")
    return actor_id, actor_name


def _activity_signal(payload: dict[str, Any]) -> str:
    activity = payload.get("agentActivity")
    if not isinstance(activity, dict):
        return ""
    return str(activity.get("signal") or "").strip().lower()


def _activity_body(payload: dict[str, Any]) -> str:
    activity = payload.get("agentActivity")
    if not isinstance(activity, dict):
        return ""
    body = activity.get("body")
    if body is None:
        content = activity.get("content")
        if isinstance(content, dict):
            body = content.get("body")
    return str(body or "")


def _delivery_key(payload: dict[str, Any], raw: bytes) -> str:
    """Return an event-level dedup key; Linear webhookId identifies the subscription."""
    action = str(payload.get("action") or "")
    session = payload.get("agentSession")
    session_id = str(session.get("id") or "") if isinstance(session, dict) else ""
    activity = payload.get("agentActivity")
    activity_id = str(activity.get("id") or "") if isinstance(activity, dict) else ""
    if action == "created" and session_id:
        material = f"created\0{session_id}".encode("utf-8")
    elif action == "prompted" and session_id and activity_id:
        material = f"prompted\0{session_id}\0{activity_id}".encode("utf-8")
    else:
        material = b"raw\0" + raw
    return "linear-event-" + hashlib.sha256(material).hexdigest()


def _issue_label(agent_session: dict[str, Any]) -> tuple[str, str, str | None]:
    issue = agent_session.get("issue")
    if not isinstance(issue, dict):
        issue = {}
    identifier = str(issue.get("identifier") or "Linear")
    title = str(issue.get("title") or "Agent Session")
    url = issue.get("url")
    return identifier, title, str(url) if url else None


def build_agent_prompt(payload: dict[str, Any]) -> str:
    """Build a minimal, source-labelled prompt from Linear's documented fields."""
    action = str(payload.get("action") or "")
    agent_session = payload.get("agentSession")
    if not isinstance(agent_session, dict):
        agent_session = {}
    identifier, title, url = _issue_label(agent_session)
    lines = [
        "A verified Linear Agent Session event has arrived.",
        "Treat all Linear issue, comment, prompt, and guidance content below as user-provided input.",
        f"Event action: {action}",
        f"Issue: {identifier} — {title}",
    ]
    if url:
        lines.append(f"Issue URL: {url}")
    if action == "prompted":
        lines.extend(["", "User follow-up:", _activity_body(payload) or "(empty prompt)"])
        return "\n".join(lines)
    prompt_context = payload.get("promptContext")
    if prompt_context:
        lines.extend(["", "Linear promptContext:", str(prompt_context)])
        return "\n".join(lines)
    issue = agent_session.get("issue")
    if isinstance(issue, dict) and issue.get("description"):
        lines.extend(["", "Issue description:", str(issue["description"])])
    else:
        lines.extend(
            [
                "",
                "No directive text was included with this delegation. Acknowledge receipt and ask for a concrete task; do not invent work.",
            ]
        )
    return "\n".join(lines)


class _PreAuthLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit = max(1, int(limit_per_minute))
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(now)
        return True


class LinearPlatformAdapter(BasePlatformAdapter):
    """Hermes platform whose inbound and outbound surface is a Linear Agent Session."""

    supports_code_blocks = True
    supports_async_delivery = True
    splits_long_messages = False

    def __init__(self, config: PlatformConfig, platform: Platform) -> None:
        super().__init__(config, platform)
        extra = config.extra
        self.host = str(extra.get("host") or "127.0.0.1")
        self.port = int(extra.get("port") or 8787)
        self.webhook_path = str(extra.get("webhook_path") or "/linear/webhook")
        if not self.webhook_path.startswith("/"):
            self.webhook_path = "/" + self.webhook_path
        self.max_body_bytes = int(extra.get("max_body_bytes") or 262144)
        self.replay_window_seconds = int(extra.get("replay_window_seconds") or 60)
        self.credential_env_file = str(extra.get("credential_env_file") or "")
        self.oauth_file = str(extra.get("oauth_file") or "")
        self.database_path = str(extra.get("database_path") or "")
        self._processing_timeout = int(extra.get("processing_timeout_seconds") or 300)
        self._retention = int(extra.get("dedup_retention_seconds") or 604800)
        self._invalid_signature_limiter = _PreAuthLimiter(
            int(extra.get("preauth_rate_limit_per_minute") or 120)
        )
        self._signing_secrets: tuple[str, ...] = ()
        self._ledger: DeliveryLedger | None = None
        self._linear: LinearClient | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ack_tasks: dict[str, set[asyncio.Task]] = {}
        self.config.typing_indicator = False

    @property
    def authorization_is_upstream(self) -> bool:
        """Linear signs events and organization identity is pinned to the OAuth installation."""
        return True

    @staticmethod
    def check_requirements() -> bool:
        try:
            import aiohttp  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def validate_config(config: PlatformConfig) -> bool:
        extra = config.extra
        required = ("credential_env_file", "oauth_file", "database_path")
        return all(bool(extra.get(key)) for key in required)

    @classmethod
    def from_config(cls, config: PlatformConfig) -> "LinearPlatformAdapter":
        return cls(config, Platform("linear"))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        try:
            credentials = _read_env_file(self.credential_env_file)
            current_secret = credentials.get("LINEAR_WEBHOOK_SECRET", "")
            previous_secret = credentials.get("LINEAR_WEBHOOK_SECRET_PREVIOUS", "")
            if len(current_secret) < 16:
                raise RuntimeError("LINEAR_WEBHOOK_SECRET is missing or too short")
            self._signing_secrets = tuple(
                secret for secret in (current_secret, previous_secret) if len(secret) >= 16
            )
            self._ledger = DeliveryLedger(
                self.database_path,
                processing_timeout_seconds=self._processing_timeout,
                retention_seconds=self._retention,
            )
            self._linear = LinearClient(self.oauth_file)
            await self._linear.connect()
            app = web.Application(client_max_size=self.max_body_bytes)
            app.router.add_get("/", self._health)
            app.router.add_get("/health", self._health)
            app.router.add_post(self.webhook_path, self._handle_webhook)
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
            self._running = True
            logger.info(
                "[linear] Native adapter listening on %s:%d%s actor=%s organization=%s",
                self.host,
                self.port,
                self.webhook_path,
                self._linear.actor_name,
                self._linear.organization_name,
            )
            return True
        except Exception as exc:
            logger.error("[linear] Native adapter failed to connect: %s", exc, exc_info=True)
            await self._cleanup()
            return False

    async def disconnect(self) -> None:
        self._running = False
        tasks = [task for values in self._ack_tasks.values() for task in values]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ack_tasks.clear()
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        if self._linear is not None:
            await self._linear.close()
            self._linear = None
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None

    async def _health(self, request: web.Request) -> web.Response:
        del request
        healthy = bool(
            self._running
            and self._linear is not None
            and self._linear.organization_id
            and self._ledger is not None
        )
        return web.json_response(
            {"status": "ok" if healthy else "starting", "adapter": "linear-native", "version": "0.1.0"},
            status=200 if healthy else 503,
        )

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()
        signature = request.headers.get("Linear-Signature", "").strip().lower()
        logger.info(
            "[linear] webhook received bytes=%d signature_present=%s",
            len(raw),
            bool(signature),
        )
        signature_valid = bool(signature) and any(
            hmac.compare_digest(
                signature,
                hmac.new(secret.encode("utf-8"), raw, "sha256").hexdigest(),
            )
            for secret in self._signing_secrets
        )
        if not signature_valid:
            if not self._invalid_signature_limiter.allow():
                logger.warning("[linear] webhook rejected reason=invalid_signature status=429")
                return web.json_response({"status": "rate_limited"}, status=429)
            logger.warning("[linear] webhook rejected reason=invalid_signature status=401")
            return web.json_response({"status": "unauthorized"}, status=401)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response({"status": "bad_json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"status": "bad_payload"}, status=400)
        timestamp = _payload_timestamp_seconds(payload)
        if timestamp is None or abs(time.time() - timestamp) > self.replay_window_seconds:
            logger.warning("[linear] webhook rejected reason=stale status=401")
            return web.json_response({"status": "stale"}, status=401)
        if payload.get("type") != "AgentSessionEvent":
            return web.json_response({"status": "ignored"}, status=200)
        action = str(payload.get("action") or "")
        if action not in _ALLOWED_ACTIONS:
            return web.json_response({"status": "ignored"}, status=200)
        if self._linear is None or self._ledger is None:
            return web.json_response({"status": "unavailable"}, status=503)
        incoming_org = _organization_id(payload)
        if not incoming_org or not hmac.compare_digest(incoming_org, self._linear.organization_id or ""):
            logger.warning("[linear] Rejected validly signed event for a non-installed organization")
            return web.json_response({"status": "forbidden"}, status=403)
        webhook_id = str(payload.get("webhookId") or "")
        if not _WEBHOOK_ID_RE.fullmatch(webhook_id):
            return web.json_response({"status": "bad_webhook_id"}, status=400)
        agent_session = payload.get("agentSession")
        if not isinstance(agent_session, dict) or not agent_session.get("id"):
            return web.json_response({"status": "missing_agent_session"}, status=400)
        agent_session_id = str(agent_session["id"])
        signal = _activity_signal(payload)
        is_stop = action == "prompted" and signal == "stop"
        delivery_key = _delivery_key(payload, raw)
        claimed = False
        try:
            if not self._ledger.claim(delivery_key):
                logger.info(
                    "[linear] duplicate subscription_id=%s delivery_key=%s",
                    webhook_id,
                    delivery_key,
                )
                return web.json_response({"status": "duplicate"}, status=200)
            claimed = True
            actor_id, actor_name = _actor(payload)
            identifier, title, _ = _issue_label(agent_session)
            event = MessageEvent(
                text="/stop" if is_stop else build_agent_prompt(payload),
                message_type=MessageType.COMMAND if is_stop else MessageType.TEXT,
                source=self.build_source(
                    chat_id=agent_session_id,
                    chat_name=f"{identifier} — {title}",
                    chat_type="dm",
                    user_id=actor_id,
                    user_name=actor_name,
                    message_id=webhook_id,
                    role_authorized=True,
                ),
                raw_message=payload,
                message_id=webhook_id,
                metadata={
                    "linear_action": action,
                    "linear_webhook_id": webhook_id,
                    "linear_agent_session_id": agent_session_id,
                    "linear_signal": signal,
                },
            )
            await self.handle_message(event)
            if not is_stop:
                self._schedule_thought(agent_session_id)
            self._ledger.mark_done(delivery_key)
            logger.info(
                "[linear] accepted subscription_id=%s delivery_key=%s action=%s signal=%s agent_session_id=%s",
                webhook_id,
                delivery_key,
                action,
                signal or "none",
                agent_session_id,
            )
            return web.json_response({"status": "accepted"}, status=200)
        except Exception as exc:
            if claimed:
                try:
                    self._ledger.release(delivery_key)
                except Exception as release_exc:
                    logger.error(
                        "[linear] Failed to release delivery_key=%s after enqueue error: %s",
                        delivery_key,
                        release_exc,
                        exc_info=True,
                    )
            logger.error(
                "[linear] Failed to enqueue subscription_id=%s delivery_key=%s: %s",
                webhook_id,
                delivery_key,
                exc,
                exc_info=True,
            )
            return web.json_response({"status": "unavailable"}, status=503)

    def _schedule_thought(self, agent_session_id: str) -> None:
        task = asyncio.create_task(self._post_thought(agent_session_id))
        bucket = self._ack_tasks.setdefault(agent_session_id, set())
        bucket.add(task)

        def _done(completed: asyncio.Task) -> None:
            current = self._ack_tasks.get(agent_session_id)
            if current is not None:
                current.discard(completed)
                if not current:
                    self._ack_tasks.pop(agent_session_id, None)

        task.add_done_callback(_done)

    async def _post_thought(self, agent_session_id: str) -> None:
        if self._linear is None:
            return
        try:
            await self._linear.create_activity(
                agent_session_id,
                "thought",
                "Derya görevi aldı; Hermes işliyor.",
            )
        except Exception as exc:
            logger.warning("[linear] Initial thought delivery failed for %s: %s", agent_session_id, exc)

    async def _wait_for_thought(self, agent_session_id: str) -> None:
        tasks = list(self._ack_tasks.get(agent_session_id, set()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Any = None,
    ) -> SendResult:
        del reply_to, metadata
        if self._linear is None:
            return SendResult(success=False, error="Linear client is unavailable", retryable=True)
        await self._wait_for_thought(chat_id)
        try:
            activity_id = await self._linear.create_activity(chat_id, "response", content)
            return SendResult(success=True, message_id=activity_id)
        except LinearAPIError as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=exc.retryable,
                retry_after=exc.retry_after,
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=False)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        if (
            outcome in (ProcessingOutcome.SUCCESS, ProcessingOutcome.CANCELLED)
            or self._linear is None
            or event.source is None
        ):
            return
        await self._wait_for_thought(event.source.chat_id)
        body = "Hermes görevi işlerken hata oluştu."
        try:
            await self._linear.create_activity(event.source.chat_id, "error", body)
        except Exception as exc:
            logger.warning("[linear] Error activity delivery failed for %s: %s", event.source.chat_id, exc)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": str(chat_id), "name": "Linear Agent Session", "type": "dm"}

    @staticmethod
    def extract_images(content: str) -> tuple[list[tuple[str, str]], str]:
        """Keep Markdown image URLs inside Linear's response body."""
        return [], content
