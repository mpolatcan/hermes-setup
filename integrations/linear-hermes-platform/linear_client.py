"""OAuth and GraphQL client for Linear Agent Activities."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

try:
    from .oauth_store import LINEAR_TOKEN_URL, LinearAPIError, LinearOAuthStore
except ImportError:  # Direct module loading in standalone tests/scripts.
    from oauth_store import LINEAR_TOKEN_URL, LinearAPIError, LinearOAuthStore

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
AGENT_SESSION_STATUSES = frozenset(
    {"pending", "active", "complete", "awaitingInput", "error", "stale"}
)
MAX_AGENT_SESSION_PAGES = 100
MAX_USER_PAGES = 100


class LinearClient:
    def __init__(
        self,
        oauth_file: str | None = None,
        *,
        oauth_store: LinearOAuthStore | None = None,
        refresh_margin_seconds: int = 300,
    ) -> None:
        if oauth_store is None:
            if not oauth_file:
                raise ValueError("oauth_file or oauth_store is required")
            oauth_store = LinearOAuthStore(
                oauth_file,
                refresh_margin_seconds=refresh_margin_seconds,
                token_url=LINEAR_TOKEN_URL,
            )
        self.oauth_store = oauth_store
        self.oauth_file = getattr(oauth_store, "oauth_file", None)
        self._session: aiohttp.ClientSession | None = None
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

    async def get_issue_team_id(self, issue_id: str) -> str:
        data = await self.graphql(
            "query LinearPolicyIssueTeam($id: String!) { issue(id: $id) { team { id } } }",
            {"id": issue_id},
        )
        team_id = str((((data.get("issue") or {}).get("team") or {}).get("id") or ""))
        if not team_id:
            raise LinearAPIError("Issue team could not be resolved for policy")
        return team_id

    async def get_user_by_url(self, user_url: str) -> dict[str, str] | None:
        """Resolve one exact organization user URL for a native Markdown mention."""
        query = """
query LinearPolicyMentionUsers($after: String) {
  users(first: 50, after: $after) {
    nodes { id url }
    pageInfo { hasNextPage endCursor }
  }
}
"""
        matches: list[dict[str, str]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_USER_PAGES):
            data = await self.graphql(query, {"after": after})
            connection = data.get("users")
            if not isinstance(connection, dict):
                raise LinearAPIError("Mention user connection was incomplete for policy")
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise LinearAPIError("Mention user connection was incomplete for policy")
            for raw in nodes:
                if not isinstance(raw, dict):
                    raise LinearAPIError("Mention user node was malformed for policy")
                user_id = raw.get("id")
                url = raw.get("url")
                if not isinstance(user_id, str) or not user_id or not isinstance(url, str) or not url:
                    raise LinearAPIError("Mention user node was incomplete for policy")
                if url == user_url:
                    matches.append({"id": user_id, "url": url})
            has_next = page_info.get("hasNextPage")
            cursor = page_info.get("endCursor")
            if not isinstance(has_next, bool) or (
                cursor is not None and not isinstance(cursor, str)
            ):
                raise LinearAPIError("Mention user pagination was incomplete for policy")
            if not has_next:
                if len(matches) > 1:
                    raise LinearAPIError("Mention user URL was ambiguous for policy")
                return matches[0] if matches else None
            if not cursor or cursor in seen_cursors:
                raise LinearAPIError("Mention user pagination did not advance")
            seen_cursors.add(cursor)
            after = cursor
        raise LinearAPIError("Mention user pagination exceeded the policy limit")

    async def get_issue_start_context(self, issue_id: str) -> dict[str, Any]:
        """Read the official Linear inputs for a delegated non-terminal start."""
        data = await self.graphql(
            """
query LinearAgentIssueStart($id: String!) {
  issue(id: $id) {
    id
    state { id name type }
    delegate { id name }
    team {
      id
      states(filter: { type: { eq: "started" } }) {
        nodes { id name type position }
      }
    }
  }
}
""",
            {"id": issue_id},
        )
        issue = data.get("issue") or {}
        team = issue.get("team") or {}
        if not issue.get("id") or not team.get("id"):
            raise LinearAPIError("Issue start context could not be resolved")
        return {
            "id": str(issue.get("id") or ""),
            "state": dict(issue.get("state") or {}),
            "delegate": dict(issue.get("delegate") or {}),
            "team": {"id": str(team.get("id") or "")},
            "started_states": list(((team.get("states") or {}).get("nodes")) or []),
        }

    async def get_comment_team_id(self, comment_id: str) -> str:
        data = await self.graphql(
            """
query LinearPolicyCommentTeam($id: String!) {
  comment(id: $id) { issue { team { id } } }
}
""",
            {"id": comment_id},
        )
        comment = data.get("comment") or {}
        team_id = str(((((comment.get("issue") or {}).get("team") or {}).get("id")) or ""))
        if not team_id:
            raise LinearAPIError("Comment team could not be resolved for policy")
        return team_id

    async def get_issue_agent_sessions(self, issue_id: str) -> list[dict[str, str]]:
        """Read normalized Agent Sessions used by outbound channel policy."""
        query = """
query LinearPolicyIssueAgentSessions($id: String!, $after: String) {
  issue(id: $id) {
    id
    agentSessions(first: 50, after: $after) {
      nodes { id status startedAt endedAt appUser { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
        sessions: list[dict[str, str]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_AGENT_SESSION_PAGES):
            data = await self.graphql(query, {"id": issue_id, "after": after})
            issue = data.get("issue")
            if (
                not isinstance(issue, dict)
                or not isinstance(issue.get("id"), str)
                or not issue.get("id")
            ):
                raise LinearAPIError("Issue Agent Sessions could not be resolved for policy")
            connection = issue.get("agentSessions")
            if not isinstance(connection, dict):
                raise LinearAPIError("Agent Session connection was incomplete for policy")
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise LinearAPIError("Agent Session connection was incomplete for policy")
            for raw in nodes:
                if not isinstance(raw, dict):
                    raise LinearAPIError("Agent Session node was malformed for policy")
                session_id = raw.get("id")
                status = raw.get("status")
                app_user = raw.get("appUser")
                app_user_id = app_user.get("id") if isinstance(app_user, dict) else None
                started_at = raw.get("startedAt")
                ended_at = raw.get("endedAt")
                if (
                    not isinstance(session_id, str)
                    or not session_id
                    or not isinstance(status, str)
                    or status not in AGENT_SESSION_STATUSES
                    or not isinstance(app_user_id, str)
                    or not app_user_id
                    or (started_at is not None and not isinstance(started_at, str))
                    or (ended_at is not None and not isinstance(ended_at, str))
                ):
                    raise LinearAPIError("Agent Session node was incomplete for policy")
                sessions.append({
                    "id": session_id,
                    "status": status,
                    "started_at": started_at or "",
                    "ended_at": ended_at or "",
                    "app_user_id": app_user_id,
                })
            has_next = page_info.get("hasNextPage")
            cursor = page_info.get("endCursor")
            if not isinstance(has_next, bool) or (
                cursor is not None and not isinstance(cursor, str)
            ):
                raise LinearAPIError("Agent Session pagination was incomplete for policy")
            if not has_next:
                return sessions
            if not cursor or cursor in seen_cursors:
                raise LinearAPIError("Agent Session pagination did not advance")
            seen_cursors.add(cursor)
            after = cursor
        raise LinearAPIError("Agent Session pagination exceeded the policy limit")

    async def get_issue_closure_context(self, issue_id: str) -> dict[str, Any]:
        """Read authoritative fields required to accept a human terminal transition."""
        query = """
query LinearNativeIssueClosure($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    updatedAt
    completedAt
    state { id name type }
    team { id states { nodes { id name type } } }
    assignee { id name }
    delegate { id name }
  }
}
"""
        data = await self.graphql(query, {"id": issue_id})
        issue = data.get("issue") or {}
        if str(issue.get("id") or "") != issue_id:
            raise LinearAPIError("Issue closure read-back did not resolve the requested issue")
        team = issue.get("team") or {}
        return {
            "id": str(issue.get("id") or ""),
            "identifier": str(issue.get("identifier") or issue_id),
            "title": str(issue.get("title") or ""),
            "updated_at": str(issue.get("updatedAt") or ""),
            "completed_at": str(issue.get("completedAt") or ""),
            "state": dict(issue.get("state") or {}),
            "team": {"id": str(team.get("id") or "")},
            "team_states": list(((team.get("states") or {}).get("nodes")) or []),
            "assignee": dict(issue.get("assignee") or {}),
            "delegate": dict(issue.get("delegate") or {}),
        }

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
        token = await self.oauth_store.access_token()
        try:
            async with self._session.post(
                LINEAR_GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                allow_redirects=False,
            ) as response:
                status = response.status
                retry_after_raw = response.headers.get("Retry-After")
                payload = await response.json(content_type=None) if status == 200 else {}
        except asyncio.TimeoutError as exc:
            raise LinearAPIError("Linear GraphQL request timed out", retryable=False) from exc
        except aiohttp.ClientError as exc:
            raise LinearAPIError("Linear GraphQL connection failed", retryable=True) from exc
        if status == 401 and refresh_on_unauthorized:
            await self.oauth_store.access_token(force_refresh=True, stale_token=token)
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
        ephemeral: bool = False,
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
        activity_input: dict[str, Any] = {
            "id": activity_id,
            "agentSessionId": agent_session_id,
            "content": {"type": activity_type, "body": body},
        }
        if ephemeral:
            activity_input["ephemeral"] = True
        try:
            data = await self.graphql(
                mutation,
                {"input": activity_input},
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
        target_type = str(target.get("type") or "").casefold()
        if target_type not in {"backlog", "unstarted", "started"}:
            raise LinearAPIError(
                f"Refusing terminal workflow state for automated writeback: {target_state_name}"
            )
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
