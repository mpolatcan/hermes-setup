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

    async def create_activity(
        self,
        agent_session_id: str,
        activity_type: str,
        body: str,
        *,
        activity_id: str,
    ) -> str:
        if activity_type not in {"thought", "response", "error", "elicitation"}:
            raise ValueError(f"Unsupported Linear activity type: {activity_type}")
        mutation = """
mutation LinearNativeAgentActivity($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    success
    agentActivity { id }
  }
}
"""
        try:
            data = await self.graphql(
                mutation,
                {
                    "input": {
                        "id": activity_id,
                        "agentSessionId": agent_session_id,
                        "content": {"type": activity_type, "body": body},
                    }
                },
            )
        except LinearAPIError as exc:
            # A client-generated activity ID makes an ambiguous timeout safe to retry.
            # If Linear committed the first request, replay can reconcile by that ID.
            if exc.retryable or "timed out" in str(exc).lower():
                raise LinearAPIError(str(exc), retryable=True, retry_after=exc.retry_after) from exc
            if await self.activity_exists(activity_id):
                return activity_id
            raise
        result = data.get("agentActivityCreate") or {}
        activity = result.get("agentActivity") or {}
        if not result.get("success") or not activity.get("id"):
            raise LinearAPIError("agentActivityCreate did not report success")
        return str(activity["id"])

    async def activity_exists(self, activity_id: str) -> bool:
        query = """
query LinearNativeAgentActivityById($id: String!) {
  agentActivity(id: $id) { id }
}
"""
        try:
            data = await self.graphql(query, {"id": activity_id})
        except LinearAPIError:
            return False
        activity = data.get("agentActivity") or {}
        return str(activity.get("id") or "") == activity_id

    async def get_open_blockers(self, issue_id: str) -> list[dict[str, str]]:
        """Return incomplete issues that block issue_id through inverse `blocks` relations."""
        query = """
query LinearNativeIssueBlockers($id: String!) {
  issue(id: $id) {
    inverseRelations(first: 100) {
      nodes {
        type
        issue { id identifier title state { id name type } }
      }
    }
  }
}
"""
        data = await self.graphql(query, {"id": issue_id})
        issue = data.get("issue") or {}
        relations = ((issue.get("inverseRelations") or {}).get("nodes")) or []
        blockers: list[dict[str, str]] = []
        for relation in relations:
            if str(relation.get("type") or "").casefold() != "blocks":
                continue
            blocker = relation.get("issue") or {}
            state = blocker.get("state") or {}
            if str(state.get("type") or "") in {"completed", "canceled"}:
                continue
            blocker_id = str(blocker.get("id") or "")
            if blocker_id:
                blockers.append({
                    "id": blocker_id,
                    "identifier": str(blocker.get("identifier") or blocker_id),
                    "title": str(blocker.get("title") or ""),
                    "state": str(state.get("name") or ""),
                })
        return blockers

    async def update_issue_state(
        self,
        issue_id: str,
        target_state_name: str,
        target_rank: int,
        state_ranks: dict[str, int],
    ) -> str:
        """Apply a monotonic symbolic state transition and preserve human terminal states."""
        query = """
query LinearNativeIssueState($id: String!) {
  issue(id: $id) {
    state { id name type }
    team { states { nodes { id name type } } }
  }
}
"""
        data = await self.graphql(query, {"id": issue_id})
        issue = data.get("issue") or {}
        current = issue.get("state") or {}
        current_id = str(current.get("id") or "")
        current_name = str(current.get("name") or "")
        current_type = str(current.get("type") or "")
        if current_type in {"completed", "canceled"}:
            return current_id
        normalized_ranks = {name.casefold(): int(rank) for name, rank in state_ranks.items()}
        current_rank = normalized_ranks.get(current_name.casefold(), -1)
        # Human-owned/custom workflow states are never overwritten. Backlog and
        # unstarted are safe initial states; configured bridge states are safe
        # monotonic transitions. Everything else wins over automation.
        if current_rank < 0 and current_type not in {"backlog", "unstarted"}:
            return current_id
        if current_rank >= int(target_rank):
            return current_id
        states = ((issue.get("team") or {}).get("states") or {}).get("nodes") or []
        target = next(
            (state for state in states if str(state.get("name") or "").casefold() == target_state_name.casefold()),
            None,
        )
        if not target or not target.get("id"):
            raise LinearAPIError(f"Linear workflow state not found: {target_state_name}")
        mutation = """
mutation LinearNativeIssueStateUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { id state { id name } } }
}
"""
        try:
            updated = await self.graphql(
                mutation,
                {"id": issue_id, "input": {"stateId": str(target["id"])}},
            )
        except LinearAPIError as exc:
            # Assigning the same state twice is idempotent, including after an
            # ambiguous response timeout.
            if exc.retryable or "timed out" in str(exc).lower():
                raise LinearAPIError(str(exc), retryable=True, retry_after=exc.retry_after) from exc
            raise
        result = updated.get("issueUpdate") or {}
        if not result.get("success"):
            raise LinearAPIError("issueUpdate did not report success")
        return str((((result.get("issue") or {}).get("state") or {}).get("id")) or target["id"])
