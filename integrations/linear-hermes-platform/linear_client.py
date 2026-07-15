"""OAuth and GraphQL client for Linear Agent Activities."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"


class LinearAPIError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class LinearClient:
    def __init__(self, oauth_file: str, *, refresh_margin_seconds: int = 300) -> None:
        self.oauth_file = Path(oauth_file)
        self.refresh_margin_seconds = refresh_margin_seconds
        self._session: aiohttp.ClientSession | None = None
        self._token_lock = asyncio.Lock()
        self.organization_id: str | None = None
        self.organization_name: str | None = None
        self.actor_id: str | None = None
        self.actor_name: str | None = None

    async def connect(self) -> None:
        timeout = aiohttp.ClientTimeout(total=8, connect=3)
        self._session = aiohttp.ClientSession(timeout=timeout)
        identity = await self.graphql(
            "query LinearNativeIdentity { viewer { id name } organization { id name } }"
        )
        viewer = identity.get("viewer") or {}
        organization = identity.get("organization") or {}
        self.actor_id = viewer.get("id")
        self.actor_name = viewer.get("name")
        self.organization_id = organization.get("id")
        self.organization_name = organization.get("name")
        if not self.actor_id or not self.organization_id:
            raise LinearAPIError("OAuth identity did not return actor and organization IDs")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _read_oauth(self) -> dict[str, Any]:
        mode = self.oauth_file.stat().st_mode & 0o777
        if mode & 0o077:
            raise LinearAPIError(f"OAuth credential file permissions are too broad: {oct(mode)}")
        data = json.loads(self.oauth_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise LinearAPIError("OAuth credential file must contain a JSON object")
        return data

    def _write_oauth(self, data: dict[str, Any]) -> None:
        tmp = self.oauth_file.with_name(f".{self.oauth_file.name}.{os.getpid()}.tmp")
        encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.oauth_file)
            os.chmod(self.oauth_file, 0o600)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    async def _refresh_locked(self, data: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise LinearAPIError("Linear client is not connected")
        refresh_token = data.get("refresh_token")
        client_id = data.get("oauth_client_id") or data.get("client_id")
        if not refresh_token or not client_id:
            raise LinearAPIError("OAuth refresh_token or client_id is missing")
        form = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            }
        )
        try:
            async with self._session.post(
                LINEAR_TOKEN_URL,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200:
                    raise LinearAPIError(f"OAuth refresh failed with HTTP {response.status}")
        except asyncio.TimeoutError as exc:
            raise LinearAPIError("OAuth refresh timed out", retryable=False) from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise LinearAPIError("OAuth refresh response did not contain access_token")
        now = int(time.time())
        updated = dict(data)
        updated["access_token"] = payload["access_token"]
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        if payload.get("token_type"):
            updated["token_type"] = payload["token_type"]
        if payload.get("scope"):
            updated["granted_scope"] = payload["scope"]
        expires_in = int(payload.get("expires_in") or 0)
        updated["expires_in"] = expires_in
        updated["obtained_at"] = now
        updated["expires_at"] = now + expires_in if expires_in else 0
        self._write_oauth(updated)
        return updated

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            data = self._read_oauth()
            expires_at = int(data.get("expires_at") or 0)
            should_refresh = force_refresh or not data.get("access_token")
            if expires_at and expires_at <= int(time.time()) + self.refresh_margin_seconds:
                should_refresh = True
            if should_refresh:
                data = await self._refresh_locked(data)
            token = data.get("access_token")
            if not token:
                raise LinearAPIError("OAuth access_token is missing")
            return str(token)

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._graphql_once(query, variables or {}, refresh_on_unauthorized=True)

    async def _graphql_once(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        refresh_on_unauthorized: bool,
    ) -> dict[str, Any]:
        if self._session is None:
            raise LinearAPIError("Linear client is not connected")
        token = await self._access_token()
        try:
            async with self._session.post(
                LINEAR_GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as response:
                status = response.status
                retry_after_raw = response.headers.get("Retry-After")
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise LinearAPIError("Linear GraphQL request timed out", retryable=False) from exc
        except aiohttp.ClientError as exc:
            raise LinearAPIError("Linear GraphQL connection failed", retryable=True) from exc
        if status == 401 and refresh_on_unauthorized:
            await self._access_token(force_refresh=True)
            return await self._graphql_once(query, variables, refresh_on_unauthorized=False)
        retry_after = None
        if retry_after_raw:
            try:
                retry_after = float(retry_after_raw)
            except ValueError:
                pass
        if status != 200:
            raise LinearAPIError(
                f"Linear GraphQL HTTP {status}",
                retryable=status == 429 or status >= 500,
                retry_after=retry_after,
            )
        if not isinstance(payload, dict):
            raise LinearAPIError("Linear GraphQL returned a non-object response")
        errors = payload.get("errors")
        if errors:
            messages = "; ".join(str(e.get("message", "GraphQL error")) for e in errors if isinstance(e, dict))
            raise LinearAPIError(messages or "Linear GraphQL error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LinearAPIError("Linear GraphQL response did not contain data")
        return data

    async def create_activity(self, agent_session_id: str, activity_type: str, body: str) -> str:
        if activity_type not in {"thought", "response", "error"}:
            raise ValueError(f"Unsupported Linear activity type: {activity_type}")
        mutation = """
mutation LinearNativeAgentActivity($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    success
    agentActivity { id }
  }
}
"""
        data = await self.graphql(
            mutation,
            {
                "input": {
                    "agentSessionId": agent_session_id,
                    "content": {"type": activity_type, "body": body},
                }
            },
        )
        result = data.get("agentActivityCreate") or {}
        activity = result.get("agentActivity") or {}
        if not result.get("success") or not activity.get("id"):
            raise LinearAPIError("agentActivityCreate did not report success")
        return str(activity["id"])
