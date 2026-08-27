from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from oauth_store import LinearAPIError  # noqa: E402
from quota_watchdog import (  # noqa: E402
    STATE_FILENAME,
    QuotaWatchdog,
    count_workspace_issues,
    main,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
EXPECTED_TEAM_IDS = frozenset({"team-ops", "team-game"})


def page(
    nodes: list[dict],
    *,
    more: bool = False,
    cursor: str | None = None,
    created_count: int | None = None,
) -> dict:
    return {
        "organization": {
            "id": "org-1",
            "createdIssueCount": len(nodes) if created_count is None else created_count,
            "teams": {
                "nodes": [
                    {"id": "team-ops", "key": "OPS", "private": False},
                    {"id": "team-game", "key": "GAME", "private": False},
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": "team-game"},
            },
        },
        "administrableTeams": {
            "nodes": [
                {"id": "team-ops", "key": "OPS", "private": False},
                {"id": "team-game", "key": "GAME", "private": False},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": "team-game"},
        },
        "issues": {
            "nodes": [
                {**node, "team": node.get("team", {"id": "team-ops", "key": "OPS"})}
                for node in nodes
            ],
            "pageInfo": {"hasNextPage": more, "endCursor": cursor},
        },
    }


def quota_client(responses: list[dict]) -> mock.MagicMock:
    client = mock.MagicMock()
    client.actor_id = "actor-1"
    client.organization_id = "org-1"
    client.graphql = mock.AsyncMock(side_effect=responses)
    return client


class QuotaPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "watchdog-state"
        self.state_dir.mkdir(mode=0o700)
        os.chmod(self.state_dir, 0o700)

    def watchdog(self, when: datetime = NOW) -> QuotaWatchdog:
        return QuotaWatchdog(self.state_dir, clock=lambda: when)

    def test_severity_boundaries_199_200_225_240(self) -> None:
        expected = {
            199: ("ok", ""),
            200: (
                "warning",
                "Linear workspace quota WARNING: 200/250 issues (50 remaining). Rolling net growth: unavailable. Estimated exhaustion: unavailable.",
            ),
            225: (
                "high",
                "Linear workspace quota HIGH: 225/250 issues (25 remaining). Rolling net growth: unavailable. Estimated exhaustion: unavailable.",
            ),
            240: (
                "critical",
                "Linear workspace quota CRITICAL: 240/250 issues (10 remaining). Rolling net growth: unavailable. Estimated exhaustion: unavailable.",
            ),
        }
        for total, (severity, alert) in expected.items():
            with self.subTest(total=total):
                isolated = self.state_dir / str(total)
                isolated.mkdir(mode=0o700)
                result = QuotaWatchdog(isolated, clock=lambda: NOW).evaluate(total)
                self.assertEqual(result.summary["severity"], severity)
                self.assertEqual(result.alert, alert)

    def test_first_alert_then_identical_replay_is_silent(self) -> None:
        first = self.watchdog().evaluate(200)
        self.assertTrue(first.alert)
        self.watchdog().save(first.next_state)

        replay = self.watchdog().evaluate(200)

        self.assertEqual(replay.alert, "")
        self.assertEqual(replay.summary["notification_reasons"], [])

    def test_identical_total_on_later_run_is_also_silent(self) -> None:
        first = self.watchdog().evaluate(200)
        self.watchdog().save(first.next_state)

        replay = self.watchdog(NOW.replace(day=19)).evaluate(200)

        self.assertEqual(replay.summary["rolling_net_growth_per_day"], 0.0)
        self.assertEqual(replay.summary["growth_trend"], "nonpositive")
        self.assertEqual(replay.summary["notification_reasons"], [])
        self.assertEqual(replay.alert, "")

    def test_changed_severity_alerts_and_recovery_alerts(self) -> None:
        first = self.watchdog().evaluate(224)
        self.watchdog().save(first.next_state)

        raised = self.watchdog().evaluate(225)
        self.assertEqual(raised.summary["notification_reasons"], ["severity_changed"])
        self.assertIn("quota HIGH", raised.alert)
        self.watchdog().save(raised.next_state)

        recovered = self.watchdog().evaluate(199)
        self.assertEqual(
            recovered.summary["notification_reasons"],
            ["severity_changed", "total_changed_materially"],
        )
        self.assertIn("quota OK", recovered.alert)

    def test_material_total_compares_to_last_alert_not_last_sample(self) -> None:
        first = self.watchdog().evaluate(200)
        self.watchdog().save(first.next_state)
        growth_alert = self.watchdog(NOW.replace(day=19)).evaluate(201)
        self.assertIn("trend_changed", growth_alert.summary["notification_reasons"])
        self.watchdog(NOW.replace(day=19)).save(growth_alert.next_state)
        for day, total in enumerate((202, 203, 204, 205), start=2):
            result = self.watchdog(NOW.replace(day=18 + day)).evaluate(total)
            self.assertEqual(result.alert, "")
            self.watchdog(NOW.replace(day=18 + day)).save(result.next_state)

        material = self.watchdog(NOW.replace(day=24)).evaluate(206)

        self.assertIn(
            "total_changed_materially", material.summary["notification_reasons"]
        )
        self.assertTrue(material.alert)

    def test_growth_and_exhaustion_are_derived_from_prior_samples(self) -> None:
        first = self.watchdog().evaluate(200)
        self.watchdog().save(first.next_state)

        second = self.watchdog(NOW.replace(day=19)).evaluate(205)

        self.assertEqual(second.summary["rolling_net_growth_per_day"], 5.0)
        self.assertEqual(second.summary["estimated_exhaustion_date"], "2026-08-28")
        self.assertIn("trend_changed", second.summary["notification_reasons"])
        self.assertIn("5 issues/day", second.alert)
        self.assertIn("2026-08-28", second.alert)

    def test_nonpositive_growth_has_no_exhaustion_date(self) -> None:
        first = self.watchdog().evaluate(210)
        self.watchdog().save(first.next_state)

        second = self.watchdog(NOW.replace(day=19)).evaluate(209)

        self.assertEqual(second.summary["growth_trend"], "nonpositive")
        self.assertEqual(second.summary["rolling_net_growth_per_day"], -1.0)
        self.assertIsNone(second.summary["estimated_exhaustion_date"])
        self.assertIn("Estimated exhaustion: unavailable", second.alert)

    def test_exhaustion_date_only_alerts_when_it_moves_by_seven_days(self) -> None:
        first = self.watchdog().evaluate(200)
        self.watchdog().save(first.next_state)
        growing = self.watchdog(NOW.replace(day=19)).evaluate(205)
        self.watchdog(NOW.replace(day=19)).save(growing.next_state)

        near = self.watchdog(NOW.replace(day=20)).evaluate(209)
        self.assertNotIn(
            "exhaustion_moved_meaningfully", near.summary["notification_reasons"]
        )
        self.watchdog(NOW.replace(day=20)).save(near.next_state)

        far = self.watchdog(NOW.replace(day=25)).evaluate(209)
        self.assertIn(
            "exhaustion_moved_meaningfully", far.summary["notification_reasons"]
        )

    def test_write_is_0600_and_state_is_secret_free(self) -> None:
        result = self.watchdog().evaluate(200)
        self.watchdog().save(result.next_state)
        state_path = self.state_dir / STATE_FILENAME

        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"schema", "samples", "last_alert"})
        encoded = json.dumps(payload).casefold()
        for forbidden in ("token", "oauth", "path", "team_id", "organization"):
            self.assertNotIn(forbidden, encoded)

    def test_corrupt_or_unsafe_state_fails_closed_without_replacement(self) -> None:
        state_path = self.state_dir / STATE_FILENAME
        cases = ("{broken", json.dumps({"schema": "wrong"}))
        for raw in cases:
            with self.subTest(raw=raw):
                state_path.write_text(raw, encoding="utf-8")
                os.chmod(state_path, 0o600)
                before = state_path.read_bytes()
                with self.assertRaisesRegex(ValueError, "continuity state"):
                    self.watchdog().evaluate(200)
                self.assertEqual(state_path.read_bytes(), before)

        state_path.write_text("{}", encoding="utf-8")
        os.chmod(state_path, 0o644)
        with self.assertRaisesRegex(ValueError, "continuity state"):
            self.watchdog().evaluate(200)

    def test_state_directory_must_be_explicit_owned_exactly_0700(self) -> None:
        os.chmod(self.state_dir, 0o755)
        with self.assertRaisesRegex(ValueError, "state directory"):
            self.watchdog().evaluate(200)


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_workspace_pagination_counts_ops_and_game_and_is_revalidated(
        self,
    ) -> None:
        client = mock.MagicMock()
        client.actor_id = "actor-1"
        client.organization_id = "org-1"
        client.graphql = mock.AsyncMock(
            side_effect=[
                page(
                    [
                        {"id": "1"},
                        {"id": "2", "team": {"id": "team-game", "key": "GAME"}},
                    ],
                    more=True,
                    cursor="next",
                    created_count=3,
                ),
                page([{"id": "3"}], created_count=3),
                page(
                    [
                        {"id": "1"},
                        {"id": "2", "team": {"id": "team-game", "key": "GAME"}},
                    ],
                    more=True,
                    cursor="next",
                    created_count=3,
                ),
                page([{"id": "3"}], created_count=3),
            ]
        )

        total = await count_workspace_issues(client, EXPECTED_TEAM_IDS)

        self.assertEqual(total, 3)
        self.assertEqual(
            [call.args[1]["after"] for call in client.graphql.await_args_list],
            [None, "next", None, "next"],
        )
        query = client.graphql.await_args_list[0].args[0]
        self.assertIn("issues(first: 50, after: $after, includeArchived: true)", query)
        self.assertIn("teams(first: 250, includeArchived: true)", query)
        self.assertIn("administrableTeams(first: 250, includeArchived: true)", query)
        self.assertNotIn("createdIssueCount", query)
        self.assertNotIn("team(id:", query)
        self.assertNotIn("viewer {", query)

    async def test_pagination_drift_fails_closed(self) -> None:
        client = mock.MagicMock()
        client.actor_id = "actor-1"
        client.organization_id = "org-1"
        client.graphql = mock.AsyncMock(
            side_effect=[
                page([{"id": "1"}, {"id": "2"}]),
                page([{"id": "1"}, {"id": "3"}]),
            ]
        )

        with self.assertRaisesRegex(LinearAPIError, "changed during revalidation"):
            await count_workspace_issues(client, EXPECTED_TEAM_IDS)

    async def test_reviewed_team_manifest_mismatch_fails_closed(self) -> None:
        payload = page([{"id": "1"}])
        payload["administrableTeams"]["nodes"].pop()
        client = quota_client([payload])
        with self.assertRaisesRegex(LinearAPIError, "reviewed team manifest"):
            await count_workspace_issues(client, EXPECTED_TEAM_IDS)

    async def test_team_or_issue_identity_shape_fails_closed(self) -> None:
        malformed_nodes = (
            {"id": "", "team": {"id": "team-ops", "key": "OPS"}},
            {"id": "1", "team": None},
            {"id": "1", "team": {"id": "", "key": "OPS"}},
            {"id": "1", "team": {"id": "team-ops", "key": ""}},
        )
        for node in malformed_nodes:
            with self.subTest(node=node):
                client = quota_client([page([node])])
                with self.assertRaisesRegex(LinearAPIError, "identity was malformed"):
                    await count_workspace_issues(client, EXPECTED_TEAM_IDS)

    async def test_second_pass_requires_team_bytes_and_order_to_match(self) -> None:
        first = [
            {"id": "1", "team": {"id": "team-ops", "key": "OPS"}},
            {"id": "2", "team": {"id": "team-game", "key": "GAME"}},
        ]
        for second in (
            [
                {"id": "1", "team": {"id": "team-game", "key": "GAME"}},
                {"id": "2", "team": {"id": "team-game", "key": "GAME"}},
            ],
            list(reversed(first)),
        ):
            with self.subTest(second=second):
                client = quota_client([page(first), page(second)])
                with self.assertRaisesRegex(
                    LinearAPIError, "changed during revalidation"
                ):
                    await count_workspace_issues(client, EXPECTED_TEAM_IDS)

    async def test_repeated_cursor_and_duplicate_identity_fail_closed(self) -> None:
        for responses, message in (
            (
                [
                    page([{"id": "1"}], more=True, cursor="same"),
                    page([{"id": "2"}], more=True, cursor="same"),
                ],
                "did not advance",
            ),
            ([page([{"id": "1"}, {"id": "1"}])], "duplicate"),
        ):
            with self.subTest(message=message):
                client = quota_client(responses)
                with self.assertRaisesRegex(LinearAPIError, message):
                    await count_workspace_issues(client, EXPECTED_TEAM_IDS)

    async def test_page_size_and_page_count_are_bounded(self) -> None:
        oversized = quota_client(
            [page([{"id": str(index)} for index in range(51)])]
        )
        with self.assertRaisesRegex(LinearAPIError, "page limit"):
            await count_workspace_issues(oversized, EXPECTED_TEAM_IDS)

        endless = quota_client(
            [
                page([{"id": str(index)}], more=True, cursor=f"cursor-{index}")
                for index in range(100)
            ]
        )
        with self.assertRaisesRegex(LinearAPIError, "page limit"):
            await count_workspace_issues(endless, EXPECTED_TEAM_IDS)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.oauth_file = self.root / "linear-oauth.json"

    def client_factory(self, *, oauth_file: str):
        self.assertEqual(oauth_file, str(self.oauth_file))
        client = mock.MagicMock()
        client.actor_id = "actor-1"
        client.organization_id = "org-1"
        client.connect = mock.AsyncMock()
        client.close = mock.AsyncMock()
        client.graphql = mock.AsyncMock(
            side_effect=[
                page(
                    [{"id": str(index)} for index in range(start, start + 50)],
                    more=start < 150,
                    cursor=f"cursor-{start + 50}" if start < 150 else None,
                    created_count=200,
                )
                for _pass in range(2)
                for start in range(0, 200, 50)
            ]
        )
        return client

    def args(self, *, dry_run: bool = False) -> list[str]:
        args = [
            "--oauth-file",
            str(self.oauth_file),
            "--state-dir",
            str(self.state_dir),
            "--expected-team-id",
            "team-ops",
            "--expected-team-id",
            "team-game",
        ]
        return args + (["--dry-run"] if dry_run else [])

    def invoke(self, *, dry_run: bool = False, factory=None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                self.args(dry_run=dry_run),
                client_factory=factory or self.client_factory,
                clock=lambda: NOW,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_emits_deterministic_json_and_never_writes_state(self) -> None:
        code, stdout, stderr = self.invoke(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertFalse((self.state_dir / STATE_FILENAME).exists())
        summary = json.loads(stdout)
        self.assertEqual(
            summary["schema"], "linear-workspace-quota-watchdog-dry-run/v2"
        )
        self.assertEqual(summary["as_of"], "2026-08-18T12:00:00Z")
        self.assertEqual(summary["total"], 200)
        self.assertEqual(summary["buffer"], 50)
        self.assertTrue(summary["would_alert"])
        self.assertFalse(summary["would_write_state"])
        self.assertEqual(
            stdout, json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
        )

    def test_normal_stdout_is_exact_alert_then_empty(self) -> None:
        code, stdout, stderr = self.invoke()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            stdout,
            "Linear workspace quota WARNING: 200/250 issues (50 remaining). Rolling net growth: unavailable. Estimated exhaustion: unavailable.\n",
        )

        code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr), (0, "", ""))

    def test_failed_alert_emission_does_not_acknowledge_or_write_state(self) -> None:
        class BrokenStdout(io.StringIO):
            def write(self, value: str) -> int:
                raise BrokenPipeError("closed")

        stderr = io.StringIO()
        with mock.patch("sys.stdout", BrokenStdout()), contextlib.redirect_stderr(stderr):
            code = main(
                self.args(),
                client_factory=self.client_factory,
                clock=lambda: NOW,
            )

        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "Linear quota watchdog failed safely.\n")
        self.assertFalse((self.state_dir / STATE_FILENAME).exists())

    def test_errors_are_redacted_and_stdout_remains_empty(self) -> None:
        secret = "secret-token-value"

        def failing_factory(*, oauth_file: str):
            client = mock.MagicMock()
            client.connect = mock.AsyncMock(
                side_effect=RuntimeError(f"failed {secret} {oauth_file}")
            )
            client.close = mock.AsyncMock()
            return client

        code, stdout, stderr = self.invoke(factory=failing_factory)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "Linear quota watchdog failed safely.\n")
        self.assertNotIn(secret, stderr)
        self.assertNotIn(str(self.oauth_file), stderr)


class WrapperTests(unittest.TestCase):
    def test_cron_wrapper_is_no_agent_and_forwards_no_secrets(self) -> None:
        wrapper = (PLUGIN_ROOT / "scripts" / "linear_quota_watchdog.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("no_agent=true", wrapper)
        self.assertIn("linear_quota_watchdog.py", wrapper)
        self.assertNotIn("LINEAR_OPERATIONS_TEAM", wrapper)
        self.assertNotIn("access_token", wrapper)
        self.assertNotIn("refresh_token", wrapper)


if __name__ == "__main__":
    unittest.main()
