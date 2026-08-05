"""Native Linear Agent Session platform adapter for Hermes Gateway."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
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
_CLOSURE_TIMESTAMP_TOLERANCE_SECONDS = 5.0
_CONTROL_EVENT_TYPES = {
    "Issue",
    "IssueRelation",
    "PermissionChange",
    "AppUserNotification",
    "OAuthApp",
}
_CONTEXT_EVENT_TYPES = {
    "Comment",
    "IssueLabel",
    "Project",
    "ProjectUpdate",
    "ProjectLabel",
    # Linear's UI/docs use human labels while payload model names can vary
    # across webhook generations. Accept canonical names and known aliases.
    "Attachment",
    "IssueAttachment",
    "Reaction",
    "CommentReaction",
    "EmojiReaction",
}
_DATA_EVENT_TYPES = _CONTROL_EVENT_TYPES | _CONTEXT_EVENT_TYPES


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


def _read_webhook_credentials(path: str) -> dict[str, str]:
    credentials = _read_env_file(path) if path else {}
    for name in ("LINEAR_WEBHOOK_SECRET", "LINEAR_WEBHOOK_SECRET_PREVIOUS"):
        value = os.environ.get(name)
        if value:
            credentials[name] = value
    return credentials


def _payload_timestamp_seconds(payload: dict[str, Any]) -> float | None:
    raw = payload.get("webhookTimestamp")
    if not isinstance(raw, bool) and raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value > 10_000_000_000:
            value /= 1000.0
        return value
    created_at = payload.get("createdAt")
    if isinstance(created_at, str) and created_at:
        try:
            return dt.datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _iso_timestamp(value: str) -> float:
    if not value:
        raise ValueError("missing timestamp")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


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
        event_type = str(payload.get("type") or "")
        data = payload.get("data")
        data_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        revision = ""
        if isinstance(data, dict):
            revision = str(data.get("updatedAt") or data.get("createdAt") or "")
        if event_type and data_id and revision:
            material = f"{event_type}\0{action}\0{data_id}\0{revision}".encode("utf-8")
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


def build_agent_prompt(payload: dict[str, Any], *, dependency_resume: bool = False) -> str:
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
    if dependency_resume:
        lines.extend(
            [
                "",
                "Adapter-verified current dependency state:",
                "All blocking issues are complete. Resume the delegated task now.",
                "Linear promptContext is a frozen creation snapshot and may still show stale blocked-by state; do not use that stale state to wait again.",
            ]
        )
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
        self._outbox_poll_seconds = float(extra.get("outbox_poll_seconds") or 1.0)
        self._outbox_base_delay = float(extra.get("outbox_base_delay_seconds") or 2.0)
        self._outbox_max_delay = float(extra.get("outbox_max_delay_seconds") or 300.0)
        self._status_writeback_enabled = extra.get("issue_status_writeback_enabled") is True
        self._data_change_events_enabled = extra.get("data_change_events_enabled") is True
        self._dependency_wait_enabled = extra.get("dependency_wait_enabled") is True
        self._dependency_poll_seconds = max(5.0, float(extra.get("dependency_poll_seconds") or 60.0))
        self._closure_reconciliation_enabled = extra.get("closure_reconciliation_enabled") is True
        allowed_closure_teams = extra.get("closure_allowed_team_ids")
        if not isinstance(allowed_closure_teams, list):
            allowed_closure_teams = []
        self._closure_allowed_team_ids = {
            str(team_id) for team_id in allowed_closure_teams
            if isinstance(team_id, str) and team_id
        }
        configured_states = extra.get("issue_status_mapping") or {}
        self._status_mapping = {
            "queued": str(configured_states.get("queued") or "Todo"),
            "running": str(configured_states.get("running") or "In Progress"),
            "blocked": str(configured_states.get("blocked") or "Blocked"),
        }
        self._status_ranks = {"queued": 10, "blocked": 15, "running": 20}
        self._invalid_signature_limiter = _PreAuthLimiter(
            int(extra.get("preauth_rate_limit_per_minute") or 120)
        )
        self._signing_secrets: tuple[str, ...] = ()
        self._ledger: DeliveryLedger | None = None
        self._linear: LinearClient | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ack_tasks: dict[str, set[asyncio.Task]] = {}
        self._outbox_task: asyncio.Task | None = None
        self._dependency_task: asyncio.Task | None = None
        self._oauth_revoked = False
        self._outbox_wakeup = asyncio.Event()
        self._outbox_drain_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._issue_locks: dict[str, asyncio.Lock] = {}
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
        required = ("oauth_file", "database_path")
        boolean_keys = (
            "issue_status_writeback_enabled",
            "data_change_events_enabled",
            "dependency_wait_enabled",
            "closure_reconciliation_enabled",
        )
        if any(key in extra and type(extra[key]) is not bool for key in boolean_keys):
            return False
        allowed = extra.get("closure_allowed_team_ids", [])
        if not (
            isinstance(allowed, list)
            and all(isinstance(team_id, str) and team_id for team_id in allowed)
        ):
            return False
        has_secret_source = bool(
            extra.get("credential_env_file") or os.environ.get("LINEAR_WEBHOOK_SECRET")
        )
        if extra.get("closure_reconciliation_enabled") is True:
            closure_safe = bool(
                extra.get("data_change_events_enabled") is True
                and extra.get("issue_status_writeback_enabled") is not True
                and isinstance(allowed, list)
                and allowed
                and all(isinstance(team_id, str) and team_id for team_id in allowed)
            )
            if not closure_safe:
                return False
        return all(bool(extra.get(key)) for key in required) and has_secret_source

    @classmethod
    def from_config(cls, config: PlatformConfig) -> "LinearPlatformAdapter":
        return cls(config, Platform("linear"))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        try:
            credentials = _read_webhook_credentials(self.credential_env_file)
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
                outbox_claim_timeout_seconds=max(30, int(self._outbox_max_delay)),
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
            self._outbox_task = asyncio.create_task(self._outbox_loop())
            if self._dependency_wait_enabled:
                self._dependency_task = asyncio.create_task(self._dependency_loop())
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
        if self._dependency_task is not None:
            self._dependency_task.cancel()
            await asyncio.gather(self._dependency_task, return_exceptions=True)
            self._dependency_task = None
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            await asyncio.gather(self._outbox_task, return_exceptions=True)
            self._outbox_task = None
        tasks = [task for values in self._ack_tasks.values() for task in values]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ack_tasks.clear()
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._dependency_task is not None:
            self._dependency_task.cancel()
            await asyncio.gather(self._dependency_task, return_exceptions=True)
            self._dependency_task = None
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            await asyncio.gather(self._outbox_task, return_exceptions=True)
            self._outbox_task = None
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
        outbox = self._ledger.outbox_counts() if self._ledger is not None else {}
        waiting = self._ledger.waiting_counts() if self._ledger is not None else {}
        closures = self._ledger.closure_counts() if self._ledger is not None else {}
        degraded = (
            int(outbox.get("dead", 0))
            or int(waiting.get("failed", 0))
            or int(closures.get("failed", 0))
            or int(closures.get("pending_session_binding", 0))
            or self._oauth_revoked
        )
        status = "degraded" if healthy and degraded else ("ok" if healthy else "starting")
        return web.json_response(
            {
                "status": status,
                "adapter": "linear-native",
                "version": "0.7.0",
                "features": {
                    "data_change_events": self._data_change_events_enabled,
                    "data_event_types": sorted(_DATA_EVENT_TYPES),
                    "dependency_wait": self._dependency_wait_enabled,
                    "status_writeback": self._status_writeback_enabled,
                    "closure_reconciliation": self._closure_reconciliation_enabled,
                },
                "outbox": outbox,
                "waiting": waiting,
                "closures": closures,
                "oauth_revoked": self._oauth_revoked,
            },
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
        if self._linear is None or self._ledger is None:
            return web.json_response({"status": "unavailable"}, status=503)
        incoming_org = _organization_id(payload)
        if not incoming_org or not hmac.compare_digest(incoming_org, self._linear.organization_id or ""):
            logger.warning("[linear] Rejected validly signed event for a non-installed organization")
            return web.json_response({"status": "forbidden"}, status=403)
        webhook_id = str(payload.get("webhookId") or "")
        if not webhook_id and payload.get("type") == "AppUserNotification":
            webhook_id = "notification-" + hashlib.sha256(raw).hexdigest()[:32]
        if not _WEBHOOK_ID_RE.fullmatch(webhook_id):
            return web.json_response({"status": "bad_webhook_id"}, status=400)
        event_type = str(payload.get("type") or "")
        if event_type != "AgentSessionEvent":
            if self._data_change_events_enabled and event_type in _DATA_EVENT_TYPES:
                return await self._handle_data_event(payload, raw, webhook_id)
            return web.json_response({"status": "ignored"}, status=200)
        action = str(payload.get("action") or "")
        if action not in _ALLOWED_ACTIONS:
            return web.json_response({"status": "ignored"}, status=200)
        agent_session = payload.get("agentSession")
        if not isinstance(agent_session, dict) or not agent_session.get("id"):
            return web.json_response({"status": "missing_agent_session"}, status=400)
        agent_session_id = str(agent_session["id"])
        issue = agent_session.get("issue")
        issue_id = str(issue.get("id") or "") if isinstance(issue, dict) else ""
        signal = _activity_signal(payload)
        is_stop = action == "prompted" and signal == "stop"
        delivery_key = _delivery_key(payload, raw)
        claimed = False
        dispatch_lock: asyncio.Lock | None = None
        dispatch_lock_held = False
        try:
            if not self._ledger.claim(delivery_key):
                logger.info(
                    "[linear] duplicate subscription_id=%s delivery_key=%s",
                    webhook_id,
                    delivery_key,
                )
                return web.json_response({"status": "duplicate"}, status=200)
            claimed = True
            if action == "created" and issue_id:
                async with self._issue_lock(issue_id):
                    self._ledger.bind_issue_session(issue_id, agent_session_id)
                    pending_closure = self._ledger.get_pending_closure_event(issue_id)
                    if pending_closure is not None:
                        closure_status = await self._reconcile_human_completion(
                            pending_closure["event"], issue_id, _issue_locked=True
                        )
                        if closure_status in {"closure_queued", "closure_duplicate"}:
                            self._ledger.mark_done(delivery_key)
                            return web.json_response({"status": closure_status}, status=200)
                        if closure_status == "closure_obsolete":
                            self._ledger.clear_pending_closure_event(
                                issue_id, pending_closure["event_revision"]
                            )
                        else:
                            self._ledger.release(delivery_key)
                            claimed = False
                            return web.json_response(
                                {"status": "closure_deferred"}, status=503
                            )
            if is_stop:
                self._ledger.cancel_wait(agent_session_id)
            if action == "created" and self._dependency_wait_enabled and issue_id:
                blockers = await self._linear.get_open_blockers(issue_id)
                if blockers:
                    self._ledger.put_wait(
                        agent_session_id,
                        issue_id,
                        delivery_key,
                        payload,
                        blockers,
                    )
                    labels = ", ".join(str(item.get("identifier") or item.get("id")) for item in blockers)
                    self._enqueue_activity(
                        agent_session_id,
                        "elicitation",
                        f"Waiting for blocking issue(s): {labels}. I will resume automatically when they are completed.",
                        item_key=f"waiting:{delivery_key}",
                    )
                    self._enqueue_status(agent_session_id, issue_id, "blocked", delivery_key)
                    self._ledger.mark_done(delivery_key)
                    # Close the race where the last blocker completed between the
                    # initial query and the durable wait commit.
                    resumed = await self._reconcile_wait(agent_session_id)
                    return web.json_response(
                        {"status": "accepted" if resumed else "awaiting_input"}, status=200
                    )
            dispatch_lock = self._session_lock(agent_session_id)
            await dispatch_lock.acquire()
            dispatch_lock_held = True
            if not is_stop and self._ledger.has_session_closure(agent_session_id):
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "closure_reconciled"}, status=200)
            event = self._message_event(payload, delivery_key, webhook_id)
            if not is_stop:
                self._schedule_thought(
                    agent_session_id,
                    issue_id,
                    delivery_key,
                    include_queued=action == "created",
                )
            await self.handle_message(event)
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
        finally:
            if dispatch_lock_held and dispatch_lock is not None:
                dispatch_lock.release()

    def _message_event(
        self,
        payload: dict[str, Any],
        delivery_key: str,
        webhook_id: str,
        *,
        dependency_resume: bool = False,
    ) -> MessageEvent:
        action = str(payload.get("action") or "")
        agent_session = payload.get("agentSession")
        if not isinstance(agent_session, dict):
            raise ValueError("Agent Session payload is missing")
        agent_session_id = str(agent_session.get("id") or "")
        issue = agent_session.get("issue")
        issue_id = str(issue.get("id") or "") if isinstance(issue, dict) else ""
        signal = _activity_signal(payload)
        is_stop = action == "prompted" and signal == "stop"
        actor_id, actor_name = _actor(payload)
        identifier, title, _ = _issue_label(agent_session)
        return MessageEvent(
            text="/stop" if is_stop else build_agent_prompt(payload, dependency_resume=dependency_resume),
            message_type=MessageType.COMMAND if is_stop else MessageType.TEXT,
            source=self.build_source(
                chat_id=agent_session_id,
                chat_name=f"{identifier} — {title}",
                chat_type="dm",
                user_id=actor_id,
                user_name=actor_name,
                message_id=delivery_key,
                role_authorized=True,
            ),
            raw_message=payload,
            message_id=delivery_key,
            metadata={
                "linear_action": action,
                "linear_webhook_id": webhook_id,
                "linear_delivery_key": delivery_key,
                "linear_agent_session_id": agent_session_id,
                "linear_issue_id": issue_id,
                "linear_signal": signal,
                "linear_dependency_resume": dependency_resume,
            },
        )

    async def _reconcile_human_completion(
        self,
        payload: dict[str, Any],
        issue_id: str,
        *,
        _issue_locked: bool = False,
        _session_locked: bool = False,
    ) -> str | None:
        """Normalize one verified human started→completed transition into a durable response."""
        if (
            not self._closure_reconciliation_enabled
            or not self._closure_allowed_team_ids
            or self._linear is None
            or self._ledger is None
        ):
            return None
        previous = payload.get("updatedFrom")
        if not isinstance(previous, dict):
            return None
        previous_state = previous.get("state")
        previous_state_id = str(
            previous.get("stateId")
            or (previous_state.get("id") if isinstance(previous_state, dict) else "")
            or ""
        )
        if not previous_state_id:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        event_state = data.get("state")
        event_state_id = str(
            data.get("stateId")
            or (event_state.get("id") if isinstance(event_state, dict) else "")
            or ""
        )
        event_state_type = str(
            event_state.get("type") if isinstance(event_state, dict) else ""
        ).casefold()
        if not event_state_id or event_state_type != "completed":
            logger.warning(
                "[linear] closure rejected issue=%s reason=invalid_terminal_evidence",
                issue_id,
            )
            return "closure_rejected"
        actor = payload.get("actor")
        if not isinstance(actor, dict):
            actor = payload.get("user")
        actor_id = str(actor.get("id") or "") if isinstance(actor, dict) else ""
        if not actor_id:
            logger.warning("[linear] closure rejected issue=%s reason=missing_actor", issue_id)
            return "closure_rejected"
        event_updated_at = str(data.get("updatedAt") or "")
        try:
            event_updated_ts = _iso_timestamp(event_updated_at)
        except ValueError:
            logger.warning("[linear] closure rejected issue=%s reason=invalid_revision", issue_id)
            return "closure_rejected"
        if not _issue_locked:
            async with self._issue_lock(issue_id):
                return await self._reconcile_human_completion(
                    payload,
                    issue_id,
                    _issue_locked=True,
                    _session_locked=_session_locked,
                )
        session_id = self._ledger.get_issue_session(issue_id) or ""
        if session_id and not _session_locked:
            async with self._session_lock(session_id):
                return await self._reconcile_human_completion(
                    payload,
                    issue_id,
                    _issue_locked=True,
                    _session_locked=True,
                )
        context = await self._linear.get_issue_closure_context(issue_id)
        state = context.get("state") or {}
        if str(state.get("type") or "").casefold() != "completed":
            return "closure_obsolete"
        current_state_id = str(state.get("id") or "")
        if event_state_id and not hmac.compare_digest(event_state_id, current_state_id):
            logger.warning("[linear] closure rejected issue=%s reason=state_mismatch", issue_id)
            return "closure_rejected"
        team_id = str((context.get("team") or {}).get("id") or "")
        assignee = context.get("assignee") or {}
        delegate = context.get("delegate") or {}
        assignee_id = str(assignee.get("id") or "")
        delegate_id = str(delegate.get("id") or "")
        live_updated_at = str(context.get("updated_at") or "")
        completed_at = str(context.get("completed_at") or "")
        previous_live = next(
            (
                item for item in context.get("team_states") or []
                if hmac.compare_digest(str(item.get("id") or ""), previous_state_id)
            ),
            None,
        )
        try:
            completion_matches = abs(
                _iso_timestamp(completed_at) - event_updated_ts
            ) <= _CLOSURE_TIMESTAMP_TOLERANCE_SECONDS
        except ValueError:
            completion_matches = False
        authoritative = bool(
            team_id in self._closure_allowed_team_ids
            and assignee_id
            and hmac.compare_digest(actor_id, assignee_id)
            and self._linear.actor_id
            and delegate_id
            and hmac.compare_digest(delegate_id, self._linear.actor_id)
            and live_updated_at
            and hmac.compare_digest(event_updated_at, live_updated_at)
            and previous_live
            and str(previous_live.get("type") or "").casefold() == "started"
            and current_state_id
            and completion_matches
        )
        if not authoritative:
            logger.warning("[linear] closure rejected issue=%s reason=authoritative_policy", issue_id)
            return "closure_rejected"
        assert previous_live is not None
        if not session_id:
            self._ledger.stage_pending_closure_event(
                issue_id,
                event_updated_ts,
                {
                    "actor": {"id": actor_id},
                    "data": {
                        "id": issue_id,
                        "updatedAt": event_updated_at,
                        "state": {"id": current_state_id, "type": "completed"},
                    },
                    "updatedFrom": {"stateId": previous_state_id},
                },
            )
            logger.info("[linear] closure deferred issue=%s reason=session_unbound", issue_id)
            return "closure_deferred"
        material = "\0".join(
            (
                issue_id,
                live_updated_at,
                completed_at,
                current_state_id,
                actor_id,
                delegate_id,
                team_id,
            )
        )
        closure_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        actor_name = str(assignee.get("name") or actor.get("name") or "Human assignee")
        delegate_name = str(delegate.get("name") or self._linear.actor_name or "Hermes")
        previous_name = str(previous_live.get("name") or "Started")
        current_name = str(state.get("name") or "Completed")
        body = "\n".join(
            (
                "Closure reconciliation complete.",
                "",
                f"- Human assignee: {actor_name}",
                f"- Verified transition: {previous_name} (`started`) → {current_name} (`completed`)",
                f"- Delegate: {delegate_name}",
                "- Main deliverable: not rerun",
                "- Terminal issue state: preserved",
            )
        )
        evidence = {
            "issue_id": issue_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "assignee_id": assignee_id,
            "delegate_id": delegate_id,
            "team_id": team_id,
            "previous_state_id": previous_state_id,
            "current_state_id": current_state_id,
            "event_updated_at": event_updated_at,
            "live_updated_at": live_updated_at,
            "verification_source": "signed_webhook_plus_live_readback",
            "completed_at": completed_at,
        }
        async with self._outbox_drain_lock:
            inserted = self._ledger.enqueue_closure_activity(
                closure_key,
                issue_id,
                session_id,
                self._activity_uuid(f"closure:{closure_key}"),
                body,
                evidence,
            )
        self._outbox_wakeup.set()
        if not inserted:
            logger.info("[linear] closure duplicate issue=%s key=%s", issue_id, closure_key)
            return "closure_duplicate"
        status = "closure_queued"
        logger.info(
            "[linear] closure %s issue=%s session=%s key=%s",
            status,
            issue_id,
            session_id,
            closure_key,
        )
        return status

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _issue_lock(self, issue_id: str) -> asyncio.Lock:
        lock = self._issue_locks.get(issue_id)
        if lock is None:
            lock = asyncio.Lock()
            self._issue_locks[issue_id] = lock
        return lock

    async def _handle_data_event(
        self,
        payload: dict[str, Any],
        raw: bytes,
        webhook_id: str,
    ) -> web.Response:
        """Observe control/context changes without turning them into LLM runs."""
        if self._linear is None or self._ledger is None:
            return web.json_response({"status": "unavailable"}, status=503)
        delivery_key = _delivery_key(payload, raw)
        if not self._ledger.claim(delivery_key):
            return web.json_response({"status": "duplicate"}, status=200)
        claimed = True
        try:
            actor_id, _ = _actor(payload)
            if self._linear.actor_id and hmac.compare_digest(actor_id, self._linear.actor_id):
                self._ledger.mark_done(delivery_key)
                return web.json_response({"status": "ignored_self"}, status=200)
            event_type = str(payload.get("type") or "")
            action = str(payload.get("action") or "")
            data = payload.get("data")
            if not isinstance(data, dict):
                data = {}
            notification = payload.get("notification")
            if not isinstance(notification, dict):
                notification = {}
            entity_id = str(data.get("id") or "")
            closure_status: str | None = None
            if event_type == "Issue" and action == "update" and entity_id:
                closure_status = await self._reconcile_human_completion(payload, entity_id)
                previous = payload.get("updatedFrom")
                if not isinstance(previous, dict):
                    previous = {}
                delegate_changed = any(key in previous for key in ("delegate", "delegateId", "delegateMetadata"))
                if delegate_changed and not (data.get("delegate") or data.get("delegateId")):
                    self._ledger.cancel_waits_for_issue(entity_id)
            if event_type == "AppUserNotification" and action == "issueUnassignedFromYou":
                notification_issue = notification.get("issue")
                if not isinstance(notification_issue, dict):
                    notification_issue = {}
                notified_issue_id = str(notification.get("issueId") or notification_issue.get("id") or "")
                if notified_issue_id:
                    self._ledger.cancel_waits_for_issue(notified_issue_id)
            if event_type == "OAuthApp" and action == "revoked":
                self._oauth_revoked = True
                logger.error("[linear] OAuth application access was revoked")
            target_ids = {entity_id} if entity_id else set()
            if event_type == "IssueRelation":
                for key in ("issueId", "relatedIssueId"):
                    if data.get(key):
                        target_ids.add(str(data[key]))
            candidates: dict[str, dict[str, Any]] = {}
            if self._dependency_wait_enabled:
                for target_id in target_ids:
                    for wait in self._ledger.find_waiting_by_blocker(target_id):
                        candidates[wait["session_id"]] = wait
                    for wait in self._ledger.list_waiting():
                        if wait["issue_id"] == target_id:
                            candidates[wait["session_id"]] = wait
            resumed = 0
            for session_id in candidates:
                if await self._reconcile_wait(session_id):
                    resumed += 1
            self._ledger.mark_done(delivery_key)
            logger.info(
                "[linear] observed data event type=%s action=%s entity=%s resumed=%d closure=%s subscription=%s",
                event_type,
                action,
                entity_id or "none",
                resumed,
                closure_status or "none",
                webhook_id,
            )
            return web.json_response(
                {"status": closure_status or "observed", "resumed": resumed}, status=200
            )
        except Exception as exc:
            if claimed:
                self._ledger.release(delivery_key)
            logger.exception("[linear] Data event reconciliation failed: %s", exc)
            return web.json_response({"status": "unavailable"}, status=503)

    async def _reconcile_wait(self, session_id: str) -> bool:
        if self._linear is None or self._ledger is None:
            return False
        wait = self._ledger.get_wait(session_id)
        if not wait or wait["state"] != "waiting":
            return False
        blockers = await self._linear.get_open_blockers(wait["issue_id"])
        if blockers:
            self._ledger.update_wait_blockers(session_id, blockers)
            return False
        if not self._ledger.claim_wait(session_id):
            return False
        try:
            async with self._session_lock(session_id):
                if self._ledger.has_session_closure(session_id):
                    return False
                payload = wait["prompt"]
                event = self._message_event(
                    payload,
                    wait["delivery_key"],
                    "dependency-resume",
                    dependency_resume=True,
                )
                self._schedule_thought(
                    session_id,
                    wait["issue_id"],
                    wait["delivery_key"],
                    include_queued=False,
                    body="All blocking issues are complete; Hermes resumed the task automatically.",
                )
                if self._ledger.has_session_closure(session_id):
                    return False
                await self.handle_message(event)
                self._ledger.mark_wait_resumed(session_id)
            logger.info("[linear] resumed waiting session=%s issue=%s", session_id, wait["issue_id"])
            return True
        except Exception as exc:
            self._ledger.fail_wait(session_id, str(exc))
            logger.exception("[linear] Failed to resume waiting session=%s: %s", session_id, exc)
            return False

    async def _dependency_loop(self) -> None:
        """Low-frequency recovery path; webhook events remain the primary wake-up."""
        while self._running:
            try:
                if self._ledger is not None:
                    for wait in self._ledger.list_waiting():
                        await self._reconcile_wait(wait["session_id"])
                await asyncio.sleep(self._dependency_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[linear] Dependency recovery loop failed: %s", exc)
                await asyncio.sleep(self._dependency_poll_seconds)

    def _schedule_thought(
        self,
        agent_session_id: str,
        issue_id: str,
        delivery_key: str,
        *,
        include_queued: bool,
        body: str | None = None,
    ) -> None:
        if body is None:
            actor_name = getattr(self._linear, "actor_name", None) or "Hermes"
            body = f"{actor_name} accepted the task; Hermes is processing it."
        self._enqueue_activity(
            agent_session_id,
            "thought",
            body,
            item_key=f"thought:{delivery_key}",
        )
        if include_queued:
            self._enqueue_status(agent_session_id, issue_id, "queued", delivery_key)
        self._enqueue_status(agent_session_id, issue_id, "running", delivery_key)
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

    @staticmethod
    def _activity_uuid(item_key: str) -> str:
        # Linear accepts client-generated activity IDs but its live validator
        # requires UUIDv4-shaped values. Derive the bytes from the stable item
        # key, then set RFC 4122 version/variant bits to v4. This preserves
        # deterministic replay without sending a UUIDv5 value.
        digest = hashlib.sha256(f"linear-hermes:{item_key}".encode()).digest()[:16]
        return str(uuid.UUID(bytes=digest, version=4))

    def _enqueue_activity(
        self,
        agent_session_id: str,
        activity_type: str,
        body: str,
        *,
        item_key: str | None = None,
    ) -> str:
        if self._ledger is None:
            raise RuntimeError("Linear outbox is unavailable")
        item_key = item_key or f"response:{uuid.uuid4()}"
        activity_id = self._activity_uuid(item_key)
        if self._ledger.has_session_closure(agent_session_id):
            logger.info(
                "[linear] suppressed post-closure activity session=%s key=%s",
                agent_session_id,
                item_key,
            )
            return activity_id
        self._ledger.enqueue_outbox(
            f"activity:{item_key}",
            agent_session_id,
            "activity.create",
            {
                "activity_id": activity_id,
                "agent_session_id": agent_session_id,
                "activity_type": activity_type,
                "body": body,
            },
        )
        self._outbox_wakeup.set()
        return activity_id

    def _enqueue_status(
        self,
        agent_session_id: str,
        issue_id: str,
        execution_state: str,
        delivery_key: str,
    ) -> None:
        if (
            not self._status_writeback_enabled
            or not issue_id
            or self._ledger is None
            or self._ledger.has_session_closure(agent_session_id)
        ):
            return
        self._ledger.enqueue_outbox(
            f"status:{delivery_key}:{execution_state}",
            agent_session_id,
            "issue.state.update",
            {
                "issue_id": issue_id,
                "execution_state": execution_state,
                "state_name": self._status_mapping[execution_state],
                "state_rank": self._status_ranks[execution_state],
                "state_ranks": {
                    self._status_mapping[state]: rank for state, rank in self._status_ranks.items()
                },
            },
        )
        self._outbox_wakeup.set()

    async def _post_thought(self, agent_session_id: str) -> None:
        try:
            # Drain the currently due sequence immediately; the background loop
            # owns any delayed retry after this fast acknowledgement attempt.
            while await self._drain_outbox_once():
                pass
        except Exception as exc:
            logger.warning("[linear] Initial thought enqueue failed for %s: %s", agent_session_id, exc)

    async def _wait_for_thought(self, agent_session_id: str) -> None:
        tasks = list(self._ack_tasks.get(agent_session_id, set()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _outbox_loop(self) -> None:
        while self._running:
            try:
                delivered = await self._drain_outbox_once()
                if delivered:
                    continue
                self._outbox_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._outbox_wakeup.wait(), timeout=self._outbox_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[linear] Outbox worker failed: %s", exc)
                await asyncio.sleep(self._outbox_poll_seconds)

    async def _drain_outbox_once(self) -> bool:
        if self._ledger is None or self._linear is None:
            return False
        async with self._outbox_drain_lock:
            item = self._ledger.claim_due_outbox()
            if item is None:
                return False
            if self._closure_reconciliation_enabled and item.operation == "issue.state.update":
                self._ledger.mark_outbox_delivered(item.id)
                logger.info(
                    "[linear] suppressed state writeback in closure-safe mode item=%s",
                    item.id,
                )
                return True
            if (
                self._ledger.has_session_closure(item.aggregate_key)
                and not item.id.startswith("activity:closure:")
            ):
                self._ledger.mark_outbox_delivered(item.id)
                logger.info(
                    "[linear] suppressed claimed post-closure outbox item=%s session=%s",
                    item.id,
                    item.aggregate_key,
                )
                return True
            try:
                if item.operation == "activity.create":
                    await self._linear.create_activity(
                        item.payload["agent_session_id"],
                        item.payload["activity_type"],
                        item.payload["body"],
                        activity_id=item.payload["activity_id"],
                    )
                elif item.operation == "issue.state.update":
                    await self._linear.update_issue_state(
                        item.payload["issue_id"],
                        item.payload["state_name"],
                        int(item.payload["state_rank"]),
                        item.payload["state_ranks"],
                    )
                else:
                    raise LinearAPIError(f"Unknown outbox operation: {item.operation}")
            except LinearAPIError as exc:
                if exc.retryable:
                    delay = exc.retry_after
                    if delay is None:
                        exponent = min(max(item.attempts - 1, 0), 16)
                        delay = min(self._outbox_max_delay, self._outbox_base_delay * (2**exponent))
                    self._ledger.reschedule_outbox(item.id, str(exc), float(delay))
                    logger.warning(
                        "[linear] Outbox retry id=%s attempts=%d delay=%.1fs: %s",
                        item.id,
                        item.attempts,
                        delay,
                        exc,
                    )
                else:
                    self._ledger.dead_letter_outbox(item.id, str(exc))
                    logger.error("[linear] Outbox dead letter id=%s: %s", item.id, exc)
            except Exception as exc:
                self._ledger.dead_letter_outbox(item.id, str(exc))
                logger.exception("[linear] Outbox dead letter id=%s: %s", item.id, exc)
            else:
                self._ledger.mark_outbox_delivered(item.id)
            return True

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Any = None,
    ) -> SendResult:
        del reply_to, metadata
        if self._linear is None or self._ledger is None:
            return SendResult(success=False, error="Linear outbox is unavailable", retryable=True)
        await self._wait_for_thought(chat_id)
        if self._ledger.has_session_closure(chat_id):
            return SendResult(
                success=True,
                message_id=self._activity_uuid(f"suppressed:closure:{chat_id}"),
            )
        try:
            activity_id = self._enqueue_activity(chat_id, "response", content)
            await self._drain_outbox_once()
            # Success means durably accepted. The outbox owns transport retries.
            return SendResult(success=True, message_id=activity_id)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=False)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        if self._ledger is None or event.source is None:
            return
        await self._wait_for_thought(event.source.chat_id)
        if self._ledger.has_session_closure(event.source.chat_id):
            logger.info(
                "[linear] suppressed processing completion after human closure session=%s",
                event.source.chat_id,
            )
            return
        delivery_key = str(event.metadata.get("linear_delivery_key") or event.message_id or uuid.uuid4())
        if outcome == ProcessingOutcome.FAILURE:
            self._enqueue_activity(
                event.source.chat_id,
                "error",
                "Hermes encountered an error while processing the task. The issue state was preserved for retry or human triage.",
                item_key=f"error:{delivery_key}",
            )
        # SUCCESS preserves the issue state for the human final-acceptance gate.
        # The durable response activity carries the evidence Mutlu reviews before
        # moving the issue to Done/Completed. FAILURE and CANCELLED also preserve
        # the current state; neither is evidence for a terminal transition.
        await self._drain_outbox_once()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": str(chat_id), "name": "Linear Agent Session", "type": "dm"}

    @staticmethod
    def extract_images(content: str) -> tuple[list[tuple[str, str]], str]:
        """Keep Markdown image URLs inside Linear's response body."""
        return [], content
