from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from linear_client import LinearClient  # noqa: E402
from oauth_store import LinearAPIError, LinearOAuthStore  # noqa: E402


def activity_page(
    *,
    nodes: list[dict],
    has_next: object = False,
    cursor: object = None,
    session_id: str = "session-1",
) -> dict:
    return {
        "agentSession": {
            "id": session_id,
            "activities": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }
    }


def response(body: object) -> dict:
    return {
        "content": {
            "__typename": "AgentActivityResponseContent",
            "body": body,
        }
    }


def session_page(
    *,
    issue_id: str,
    nodes: list[dict],
    has_next: bool = False,
    cursor: str | None = None,
) -> dict:
    return {
        "issue": {
            "id": issue_id,
            "agentSessions": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }
    }


class AgentSessionPolicyEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def client(self) -> LinearClient:
        return LinearClient(oauth_store=mock.MagicMock(spec=LinearOAuthStore))

    async def test_plan_context_reads_exact_revision_and_human_app_flag(self):
        client = self.client()
        issue = {
            "id": "issue-1",
            "title": "Short brief",
            "updatedAt": "2026-08-09T18:00:00.000Z",
            "description": "brief",
            "team": {"id": "ops-1"},
            "state": {"id": "todo-1", "type": "unstarted"},
            "assignee": {"id": "human-1", "app": False},
            "delegate": {"id": "actor-1"},
        }
        client.graphql = mock.AsyncMock(return_value={"issue": issue})

        result = await client.get_issue_plan_context("OPS-1")

        self.assertEqual(result, issue)
        query, variables = client.graphql.await_args_list[0].args
        self.assertIn("updatedAt", query)
        self.assertIn("assignee { id app }", query)
        self.assertEqual(variables, {"id": "OPS-1"})

    async def test_issue_session_pagination_rejects_resolved_issue_drift(self):
        client = self.client()
        client.graphql = mock.AsyncMock(side_effect=[
            session_page(issue_id="issue-a", nodes=[], has_next=True, cursor="next"),
            session_page(
                issue_id="issue-b",
                nodes=[{
                    "id": "foreign-session",
                    "status": "complete",
                    "startedAt": "2026-08-09T18:00:00.000Z",
                    "endedAt": "2026-08-09T18:01:00.000Z",
                    "appUser": {"id": "specialist-1"},
                }],
            ),
        ])

        with self.assertRaisesRegex(LinearAPIError, "authorization context changed"):
            await client.get_issue_agent_sessions("OPS-1")

    async def test_plan_context_missing_issue_fails_closed(self):
        client = self.client()
        client.graphql = mock.AsyncMock(return_value={"issue": None})

        with self.assertRaisesRegex(LinearAPIError, "plan context was unavailable"):
            await client.get_issue_plan_context("OPS-404")

    async def test_complete_session_counts_nonempty_terminal_responses(self):
        client = self.client()
        client.graphql = mock.AsyncMock(
            return_value=activity_page(nodes=[
                response("SPECIALIST_READY"),
                {
                    "content": {
                        "__typename": "AgentActivityThoughtContent",
                        "body": "working",
                    }
                },
            ])
        )

        count = await client.get_agent_session_terminal_response_count("session-1")

        self.assertEqual(count, 1)
        client.graphql.assert_awaited_once()
        self.assertEqual(
            client.graphql.await_args_list[0].args[1],
            {"id": "session-1", "after": None},
        )

    async def test_terminal_response_on_second_page_is_counted(self):
        client = self.client()
        client.graphql = mock.AsyncMock(side_effect=[
            activity_page(nodes=[], has_next=True, cursor="cursor-1"),
            activity_page(nodes=[response("SECOND_PAGE_READY")]),
        ])

        count = await client.get_agent_session_terminal_response_count("session-1")

        self.assertEqual(count, 1)
        self.assertEqual(
            [call.args[1]["after"] for call in client.graphql.await_args_list],
            [None, "cursor-1"],
        )

    async def test_whitespace_only_response_is_not_terminal_evidence(self):
        client = self.client()
        client.graphql = mock.AsyncMock(
            return_value=activity_page(nodes=[response("  \n\t")])
        )

        self.assertEqual(
            await client.get_agent_session_terminal_response_count("session-1"),
            0,
        )

    async def test_repeated_activity_cursor_fails_closed(self):
        client = self.client()
        client.graphql = mock.AsyncMock(side_effect=[
            activity_page(nodes=[], has_next=True, cursor="cursor-1"),
            activity_page(nodes=[], has_next=True, cursor="cursor-1"),
        ])

        with self.assertRaisesRegex(LinearAPIError, "did not advance"):
            await client.get_agent_session_terminal_response_count("session-1")

    async def test_malformed_activity_pagination_fails_closed(self):
        client = self.client()
        client.graphql = mock.AsyncMock(
            return_value=activity_page(nodes=[], has_next="yes", cursor=["bad"])
        )

        with self.assertRaisesRegex(LinearAPIError, "pagination was incomplete"):
            await client.get_agent_session_terminal_response_count("session-1")

    async def test_activity_page_limit_fails_closed(self):
        client = self.client()
        client.graphql = mock.AsyncMock(
            return_value=activity_page(nodes=[], has_next=True, cursor="cursor-1")
        )

        with (
            mock.patch("linear_client.MAX_AGENT_ACTIVITY_PAGES", 1),
            self.assertRaisesRegex(LinearAPIError, "exceeded the policy limit"),
        ):
            await client.get_agent_session_terminal_response_count("session-1")


if __name__ == "__main__":
    unittest.main()
