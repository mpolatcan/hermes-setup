"""OAuth and GraphQL client for Linear Agent Activities."""

from __future__ import annotations

import asyncio
import hmac
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
MAX_AGENT_ACTIVITY_PAGES = 100
MAX_USER_PAGES = 100
MAX_CHILD_RELATION_PAGES = 100


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

    async def assign_issue_delegate(self, issue_id: str, delegate_id: str) -> str:
        """Assign the installed app as delegate after a durable manager intake claim."""
        data = await self.graphql(
            """
mutation LinearManagerActivationDelegate($id: String!, $delegateId: String!) {
  issueUpdate(id: $id, input: { delegateId: $delegateId }) {
    success
    issue { id delegate { id } }
  }
}
""",
            {"id": issue_id, "delegateId": delegate_id},
        )
        result = data.get("issueUpdate") or {}
        issue = result.get("issue") or {}
        actual = str(((issue.get("delegate") or {}).get("id") or ""))
        if result.get("success") is not True or not hmac.compare_digest(actual, delegate_id):
            raise LinearAPIError("Manager activation delegate assignment was not confirmed")
        return str(issue.get("id") or issue_id)

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

    async def get_issue_plan_context(self, issue_id: str) -> dict[str, Any]:
        """Read the exact revision and live ownership inputs for plan enrichment."""
        data = await self.graphql(
            """
query IssuePlanContext($id: String!) {
  issue(id: $id) {
    id
    title
    updatedAt
    description
    team { id }
    state { id type }
    assignee { id app }
    delegate { id }
  }
}
""",
            {"id": issue_id},
        )
        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise LinearAPIError("Linear issue plan context was unavailable")
        return issue

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

    async def get_issue_child_terminal_context(self, issue_id: str) -> dict[str, Any]:
        """Read immutable ownership and live guards for creator-owned child terminal actions."""
        query = """
query LinearCreatorChildTerminal($id: String!, $after: String, $stateAfter: String) {
  issue(id: $id) {
    id
    state { id name type }
    creator { id }
    delegate { id }
    project { id }
    parent {
      id
      state { id name type }
      assignee { id app }
      project { id }
    }
    team {
      id
      states(first: 50, after: $stateAfter, filter: { type: { in: ["completed", "canceled"] } }) {
        nodes { id name type position }
        pageInfo { hasNextPage endCursor }
      }
    }
    inverseRelations(first: 100, after: $after) {
      nodes {
        type
        issue { id identifier title state { id name type } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
        issue: dict[str, Any] | None = None
        authorization_context: dict[str, Any] | None = None
        relations: list[dict[str, Any]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_CHILD_RELATION_PAGES):
            data = await self.graphql(
                query,
                {"id": issue_id, "after": after, "stateAfter": None},
            )
            page_issue = data.get("issue")
            if (
                not isinstance(page_issue, dict)
                or not isinstance(page_issue.get("id"), str)
                or not page_issue.get("id")
            ):
                raise LinearAPIError("Creator-owned child terminal context could not be resolved")
            page_team = page_issue.get("team")
            page_authorization_context = {
                "state": page_issue.get("state"),
                "creator": page_issue.get("creator"),
                "delegate": page_issue.get("delegate"),
                "project": page_issue.get("project"),
                "parent": page_issue.get("parent"),
                "team_id": page_team.get("id") if isinstance(page_team, dict) else None,
            }
            if issue is None:
                issue = page_issue
                authorization_context = page_authorization_context
            elif page_issue.get("id") != issue.get("id"):
                raise LinearAPIError("Creator-owned child identity changed during pagination")
            elif page_authorization_context != authorization_context:
                raise LinearAPIError(
                    "Creator-owned child authorization context changed during pagination"
                )
            connection = page_issue.get("inverseRelations")
            if not isinstance(connection, dict):
                raise LinearAPIError("Creator-owned child terminal context was incomplete")
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise LinearAPIError("Creator-owned child terminal context was incomplete")
            for relation in nodes:
                if not isinstance(relation, dict):
                    raise LinearAPIError("Creator-owned child relation was malformed")
                relations.append(relation)
            has_next = page_info.get("hasNextPage")
            cursor = page_info.get("endCursor")
            if not isinstance(has_next, bool) or (
                cursor is not None and not isinstance(cursor, str)
            ):
                raise LinearAPIError("Creator-owned child relation pagination was malformed")
            if not has_next:
                break
            if not cursor or cursor in seen_cursors:
                raise LinearAPIError("Creator-owned child relation pagination cursor was invalid")
            seen_cursors.add(cursor)
            after = cursor
        else:
            raise LinearAPIError("Creator-owned child relation pagination exceeded safety limit")

        assert issue is not None
        team = issue.get("team")
        if not isinstance(team, dict) or not isinstance(team.get("id"), str) or not team.get("id"):
            raise LinearAPIError("Creator-owned child team context was incomplete")
        state_connection = team.get("states")
        if not isinstance(state_connection, dict):
            raise LinearAPIError("Creator-owned child terminal-state context was incomplete")
        first_state_nodes = state_connection.get("nodes")
        state_page_info = state_connection.get("pageInfo")
        if not isinstance(first_state_nodes, list) or not isinstance(state_page_info, dict):
            raise LinearAPIError("Creator-owned child terminal-state context was incomplete")
        states: list[dict[str, Any]] = []
        for state in first_state_nodes:
            if not isinstance(state, dict):
                raise LinearAPIError("Creator-owned child terminal state was malformed")
            states.append(state)
        state_has_next = state_page_info.get("hasNextPage")
        state_cursor = state_page_info.get("endCursor")
        if not isinstance(state_has_next, bool) or (
            state_cursor is not None and not isinstance(state_cursor, str)
        ):
            raise LinearAPIError("Creator-owned child terminal-state pagination was malformed")

        state_query = """
query LinearCreatorChildTerminalStates($id: String!, $after: String) {
  issue(id: $id) {
    id
    state { id name type }
    creator { id }
    delegate { id }
    project { id }
    parent { id state { id name type } assignee { id app } project { id } }
    team {
      id
      states(first: 50, after: $after, filter: { type: { in: ["completed", "canceled"] } }) {
        nodes { id name type position }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
        seen_state_cursors: set[str] = set()
        for _page in range(1, MAX_CHILD_RELATION_PAGES):
            if not state_has_next:
                break
            if not state_cursor or state_cursor in seen_state_cursors:
                raise LinearAPIError(
                    "Creator-owned child terminal-state pagination cursor was invalid"
                )
            seen_state_cursors.add(state_cursor)
            state_data = await self.graphql(
                state_query,
                {"id": issue_id, "after": state_cursor},
            )
            state_issue = state_data.get("issue")
            if (
                not isinstance(state_issue, dict)
                or state_issue.get("id") != issue.get("id")
            ):
                raise LinearAPIError("Creator-owned child identity changed during pagination")
            state_team = state_issue.get("team")
            state_authorization_context = {
                "state": state_issue.get("state"),
                "creator": state_issue.get("creator"),
                "delegate": state_issue.get("delegate"),
                "project": state_issue.get("project"),
                "parent": state_issue.get("parent"),
                "team_id": state_team.get("id") if isinstance(state_team, dict) else None,
            }
            if state_authorization_context != authorization_context:
                raise LinearAPIError(
                    "Creator-owned child authorization context changed during pagination"
                )
            state_connection = (
                state_team.get("states") if isinstance(state_team, dict) else None
            )
            if not isinstance(state_connection, dict):
                raise LinearAPIError("Creator-owned child terminal-state context was incomplete")
            state_nodes = state_connection.get("nodes")
            state_page_info = state_connection.get("pageInfo")
            if not isinstance(state_nodes, list) or not isinstance(state_page_info, dict):
                raise LinearAPIError("Creator-owned child terminal-state context was incomplete")
            for state in state_nodes:
                if not isinstance(state, dict):
                    raise LinearAPIError("Creator-owned child terminal state was malformed")
                states.append(state)
            state_has_next = state_page_info.get("hasNextPage")
            state_cursor = state_page_info.get("endCursor")
            if not isinstance(state_has_next, bool) or (
                state_cursor is not None and not isinstance(state_cursor, str)
            ):
                raise LinearAPIError(
                    "Creator-owned child terminal-state pagination was malformed"
                )
        else:
            if state_has_next:
                raise LinearAPIError(
                    "Creator-owned child terminal-state pagination exceeded safety limit"
                )
        blockers: list[dict[str, str]] = []
        for relation in relations:
            if str(relation.get("type") or "").casefold() != "blocks":
                continue
            blocker = relation.get("issue")
            if not isinstance(blocker, dict):
                raise LinearAPIError("Creator-owned child blocker was malformed")
            blocker_state = blocker.get("state")
            if not isinstance(blocker_state, dict):
                raise LinearAPIError("Creator-owned child blocker state was malformed")
            if str(blocker_state.get("type") or "").casefold() in {"completed", "canceled"}:
                continue
            blocker_id = blocker.get("id")
            if not isinstance(blocker_id, str) or not blocker_id:
                raise LinearAPIError("Creator-owned child blocker identity was incomplete")
            blockers.append({
                "id": blocker_id,
                "identifier": str(blocker.get("identifier") or blocker_id),
                "title": str(blocker.get("title") or ""),
                "state": str(blocker_state.get("name") or ""),
            })
        return {
            "id": str(issue.get("id") or ""),
            "state": dict(issue.get("state") or {}),
            "creator": dict(issue.get("creator") or {}),
            "delegate": dict(issue.get("delegate") or {}),
            "project": dict(issue.get("project") or {}),
            "parent": dict(issue.get("parent") or {}),
            "team": {"id": str(team.get("id") or "")},
            "terminal_states": list(states),
            "open_blockers": blockers,
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
        resolved_issue_id = ""
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
            page_issue_id = str(issue.get("id") or "")
            if not resolved_issue_id:
                resolved_issue_id = page_issue_id
            elif page_issue_id != resolved_issue_id:
                raise LinearAPIError("Issue Agent Session authorization context changed during pagination")
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

    async def get_channel_routing_context(self, issue_ref: str) -> dict[str, Any]:
        """Resolve one explicit issue reference and all authoritative Agent Sessions."""
        query = """
query LinearChannelRoutingContext($id: String!, $after: String) {
  issue(id: $id) {
    id
    identifier
    title
    state { id name type }
    delegate { id name }
    agentSessions(first: 50, after: $after) {
      nodes { id status startedAt endedAt appUser { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
        context: dict[str, Any] | None = None
        sessions: list[dict[str, str]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_AGENT_SESSION_PAGES):
            data = await self.graphql(query, {"id": issue_ref, "after": after})
            issue = data.get("issue")
            if not isinstance(issue, dict) or not str(issue.get("id") or ""):
                raise LinearAPIError("Channel routing issue could not be resolved")
            page_context = {
                "id": str(issue.get("id") or ""),
                "identifier": str(issue.get("identifier") or issue_ref),
                "title": str(issue.get("title") or ""),
                "state": dict(issue.get("state") or {}),
                "delegate": dict(issue.get("delegate") or {}),
            }
            if context is None:
                context = page_context
            elif page_context != context:
                raise LinearAPIError(
                    "Channel routing authorization context changed during pagination"
                )
            connection = issue.get("agentSessions")
            if not isinstance(connection, dict):
                raise LinearAPIError("Channel routing Agent Sessions were unavailable")
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise LinearAPIError("Channel routing Agent Sessions were incomplete")
            for raw in nodes:
                if not isinstance(raw, dict):
                    raise LinearAPIError("Channel routing Agent Session was malformed")
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
                    raise LinearAPIError("Channel routing Agent Session was incomplete")
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
                raise LinearAPIError("Channel routing pagination was incomplete")
            if not has_next:
                assert context is not None
                return {**context, "sessions": sessions}
            if not cursor or cursor in seen_cursors:
                raise LinearAPIError("Channel routing pagination did not advance")
            seen_cursors.add(cursor)
            after = cursor
        raise LinearAPIError("Channel routing pagination exceeded the policy limit")

    async def get_agent_session_terminal_response_count(self, session_id: str) -> int:
        """Count non-empty terminal responses for one authoritative Agent Session."""
        query = """
query LinearPolicyAgentSessionResponses($id: String!, $after: String) {
  agentSession(id: $id) {
    id
    activities(first: 50, after: $after) {
      nodes {
        content {
          __typename
          ... on AgentActivityResponseContent { body }
          ... on AgentActivityThoughtContent { body }
          ... on AgentActivityElicitationContent { body }
          ... on AgentActivityErrorContent { body }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
        count = 0
        after: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_AGENT_ACTIVITY_PAGES):
            data = await self.graphql(query, {"id": session_id, "after": after})
            session = data.get("agentSession")
            if (
                not isinstance(session, dict)
                or str(session.get("id") or "") != session_id
            ):
                raise LinearAPIError("Agent Session response evidence could not be resolved")
            connection = session.get("activities")
            if not isinstance(connection, dict):
                raise LinearAPIError("Agent Session response evidence was incomplete")
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise LinearAPIError("Agent Session response evidence was incomplete")
            for activity in nodes:
                if not isinstance(activity, dict):
                    raise LinearAPIError("Agent Session activity was malformed for policy")
                content = activity.get("content")
                if not isinstance(content, dict):
                    raise LinearAPIError("Agent Session activity content was malformed for policy")
                if content.get("__typename") == "AgentActivityResponseContent":
                    body = content.get("body")
                    if isinstance(body, str) and body.strip():
                        count += 1
            has_next = page_info.get("hasNextPage")
            cursor = page_info.get("endCursor")
            if not isinstance(has_next, bool) or (
                cursor is not None and not isinstance(cursor, str)
            ):
                raise LinearAPIError("Agent Session activity pagination was incomplete")
            if not has_next:
                return count
            if not cursor or cursor in seen_cursors:
                raise LinearAPIError("Agent Session activity pagination did not advance")
            seen_cursors.add(cursor)
            after = cursor
        raise LinearAPIError("Agent Session activity pagination exceeded the policy limit")

    async def get_agent_session_delivery_context(self, session_id: str) -> dict[str, Any]:
        """Read the authoritative app owner for one delivery target."""
        data = await self.graphql(
            """
query LinearAgentSessionDeliveryGuard($id: String!) {
  agentSession(id: $id) {
    id
    appUser { id }
  }
}
""",
            {"id": session_id},
        )
        session = data.get("agentSession")
        if not isinstance(session, dict) or str(session.get("id") or "") != session_id:
            raise LinearAPIError("Agent Session delivery target could not be resolved")
        app_user = session.get("appUser")
        app_user_id = app_user.get("id") if isinstance(app_user, dict) else None
        if not isinstance(app_user_id, str) or not app_user_id:
            raise LinearAPIError("Agent Session delivery context was incomplete")
        return {
            "id": session_id,
            "app_user_id": app_user_id,
        }

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
    creator { id }
    parent { id }
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
            "creator": dict(issue.get("creator") or {}),
            "parent": dict(issue.get("parent") or {}),
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
            raise LinearAPIError("Linear GraphQL request timed out", retryable=True) from exc
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
