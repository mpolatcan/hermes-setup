from __future__ import annotations

import contextlib
import hashlib
import io
import json
import stat
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from oauth_store import LinearAPIError  # noqa: E402
from retention import (  # noqa: E402
    RetentionInventoryReader,
    _read_json_object,
    _run,
    _write_private_json,
    build_manifest,
    classify_inventory,
    main,
)


AS_OF = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def comment(
    comment_id: str = "comment-app",
    *,
    body: str = "Automated housekeeping",
    author_is_app: bool | None = True,
    created_at: str = "2025-01-04T00:00:00Z",
    updated_at: str = "2025-01-04T00:00:00Z",
) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "author_is_app": author_is_app,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def api_comment(
    comment_id: str,
    *,
    body: str,
    app: bool,
    created_at: str = "2025-01-04T00:00:00Z",
    updated_at: str = "2025-01-04T00:00:00Z",
) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "user": {"app": app},
    }


def issue(identifier: str = "OPS-100") -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": "Superseded housekeeping record",
        "description": "Redundant record with no retained context.",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-03T00:00:00Z",
        "completed_at": "2025-01-02T00:00:00Z",
        "canceled_at": None,
        "state_type": "completed",
        "state_name": "Done",
        "team_id": "team-ops",
        "project_name": "Maintenance",
        "labels": [],
        "parent_count": 0,
        "child_count": 0,
        "relation_count": 0,
        "inverse_relation_count": 0,
        "attachment_count": 0,
        "document_count": 0,
        "comments": [],
    }


def successor() -> dict:
    value = issue("OPS-999")
    value.update({"title": "Current consolidated record", "state_type": "started"})
    return value


def attestations() -> dict:
    return {"OPS-100": {"successor": "OPS-999", "verified": True}}


class RetentionClassifierTests(unittest.TestCase):
    def classify(self, candidate: dict, mapping: dict | None = None):
        return classify_inventory(
            [candidate, successor()],
            successor_attestations=attestations() if mapping is None else mapping,
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )

    def test_only_complete_old_attested_unprotected_issue_is_candidate(self):
        result = self.classify(issue())

        self.assertEqual([entry.identifier for entry in result.candidates], ["OPS-100"])
        self.assertEqual(result.candidates[0].canonical_successor, "OPS-999")
        self.assertEqual(result.candidates[0].age_days, 592)

    def test_app_comment_with_protected_semantics_is_protected(self):
        candidate = issue()
        candidate["comments"] = [comment(
            "comment-app-security", body="Incident security decision evidence"
        )]

        result = self.classify(candidate)

        self.assertEqual(result.candidates, ())
        reasons = result.reasons_by_identifier["OPS-100"]
        self.assertIn("decision_security_or_incident_semantics", reasons)
        self.assertNotIn("human_discussion", reasons)
        self.assertNotIn("ambiguous_comment_authorship", reasons)

    def test_plural_security_and_incident_terms_are_protected(self):
        for text in ("credentials", "secrets", "vulnerabilities", "postmortems"):
            with self.subTest(text=text):
                candidate = issue()
                candidate["description"] = f"Retains {text} evidence"
                result = self.classify(candidate)
                self.assertEqual(result.candidates, ())
                self.assertIn(
                    "decision_security_or_incident_semantics",
                    result.reasons_by_identifier["OPS-100"],
                )

    def test_workflow_state_name_participates_in_protected_semantics(self):
        candidate = issue()
        candidate["state_name"] = "Incident Closed"

        result = self.classify(candidate)

        self.assertEqual(result.candidates, ())
        self.assertIn(
            "decision_security_or_incident_semantics",
            result.reasons_by_identifier["OPS-100"],
        )

    def test_protected_fixture_corpus_has_zero_false_positives(self):
        fixtures: list[tuple[str, dict]] = []

        def protected(name: str, **changes: object) -> None:
            value = issue(f"OPS-{200 + len(fixtures)}")
            value.update(changes)
            fixtures.append((name, value))

        protected("active", state_type="started", state_name="In Progress")
        protected("nonterminal", state_type="unstarted", state_name="Todo")
        protected("operational_inbox_label", labels=["Operational Inbox"])
        protected("operational_inbox_project", project_name="Ops Inbox")
        protected(
            "human_discussion",
            comments=[comment("comment-1", body="Keep this", author_is_app=False)],
        )
        protected(
            "unknown_comment_author",
            comments=[comment("comment-2", body="?", author_is_app=None)],
        )
        protected("decision", title="Decision: retain vendor A")
        protected("security", description="Contains security access review evidence")
        protected("incident", labels=["postmortem"])
        protected("parent_dependency", parent_count=1)
        protected("child_dependency", child_count=1)
        protected("relation", relation_count=1)
        protected("inverse_relation", inverse_relation_count=1)
        protected("attachment", attachment_count=1)
        protected("document", document_count=1)
        protected("canonical_pointer", description="Canonical: https://linear.app/acme/issue/OPS-999/current")
        protected("bare_issue_pointer", description="Use OPS-999 as the retained record")
        protected("too_young", updated_at="2026-08-01T00:00:00Z")
        protected("ambiguous_state", state_type="custom")
        protected("ambiguous_terminal_timestamp", completed_at=None)

        inventory = [value for _, value in fixtures] + [successor()]
        mapping = {
            value["identifier"]: {"successor": "OPS-999", "verified": True}
            for _, value in fixtures
        }
        result = classify_inventory(
            inventory,
            successor_attestations=mapping,
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.summary["candidate_count"], 0)
        self.assertEqual(result.summary["protected_count"], len(inventory))
        for name, value in fixtures:
            with self.subTest(name=name):
                self.assertTrue(result.reasons_by_identifier[value["identifier"]])

    def test_successor_must_be_explicit_verified_distinct_and_present(self):
        invalid = [
            {},
            {"OPS-100": {"successor": "OPS-999", "verified": False}},
            {"OPS-100": {"successor": "OPS-100", "verified": True}},
            {"OPS-100": {"successor": "OPS-404", "verified": True}},
        ]

        for mapping in invalid:
            with self.subTest(mapping=mapping):
                result = self.classify(issue(), mapping)
                self.assertEqual(result.candidates, ())
                self.assertIn(
                    "no_verified_canonical_successor",
                    result.reasons_by_identifier["OPS-100"],
                )

    def test_verified_successors_are_never_candidates_in_chains_or_cycles(self):
        middle = issue("OPS-101")
        retained = issue("OPS-102")
        chain = classify_inventory(
            [issue(), middle, retained],
            successor_attestations={
                "OPS-100": {"successor": "OPS-101", "verified": True},
                "OPS-101": {"successor": "OPS-102", "verified": True},
                "OPS-102": {"successor": "OPS-100", "verified": False},
            },
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )

        self.assertEqual(
            [entry.identifier for entry in chain.candidates],
            ["OPS-100"],
        )
        self.assertIn(
            "verified_canonical_successor",
            chain.reasons_by_identifier["OPS-101"],
        )
        self.assertIn(
            "verified_canonical_successor",
            chain.reasons_by_identifier["OPS-102"],
        )

        cycle = classify_inventory(
            [issue(), middle],
            successor_attestations={
                "OPS-100": {"successor": "OPS-101", "verified": True},
                "OPS-101": {"successor": "OPS-100", "verified": True},
            },
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )

        self.assertEqual(cycle.candidates, ())
        self.assertIn(
            "verified_canonical_successor",
            cycle.reasons_by_identifier["OPS-100"],
        )
        self.assertIn(
            "verified_canonical_successor",
            cycle.reasons_by_identifier["OPS-101"],
        )

    def test_duplicate_or_malformed_inventory_fails_closed(self):
        duplicate = issue()
        with self.assertRaisesRegex(ValueError, "ambiguous inventory"):
            self.classify_inventory_direct([issue(), duplicate, successor()])
        malformed = issue()
        del malformed["attachment_count"]
        with self.assertRaisesRegex(ValueError, "ambiguous issue evidence"):
            self.classify_inventory_direct([malformed, successor()])

    def test_timestamp_chronology_fails_closed(self):
        malformed_timestamps = {
            "completed_before_created": {
                "completed_at": "2024-12-31T23:59:59Z",
            },
            "canceled_before_created": {
                "state_type": "canceled",
                "completed_at": None,
                "canceled_at": "2024-12-31T23:59:59Z",
            },
            "updated_before_created": {
                "updated_at": "2024-12-31T23:59:59Z",
            },
            "future_completed": {
                "completed_at": "2026-08-18T12:00:01Z",
            },
            "future_canceled": {
                "state_type": "canceled",
                "completed_at": None,
                "canceled_at": "2026-08-18T12:00:01Z",
            },
            "future_updated": {
                "updated_at": "2026-08-18T12:00:01Z",
            },
        }

        for name, changes in malformed_timestamps.items():
            with self.subTest(name=name):
                candidate = issue()
                candidate.update(changes)
                with self.assertRaisesRegex(ValueError, "ambiguous issue evidence"):
                    self.classify_inventory_direct([candidate, successor()])

    def test_recent_app_comment_prevents_candidacy(self):
        candidate = issue()
        candidate["comments"] = [comment(
            created_at="2026-08-18T11:59:59Z",
            updated_at="2026-08-18T11:59:59Z",
        )]

        result = self.classify(candidate)

        self.assertEqual(result.candidates, ())
        self.assertIn("too_young", result.reasons_by_identifier["OPS-100"])

    def test_comment_timestamp_evidence_fails_closed(self):
        malformed = {
            "missing_created": {"created_at": None},
            "missing_updated": {"updated_at": None},
            "malformed_created": {"created_at": "not-a-timestamp"},
            "future_created": {"created_at": "2026-08-18T12:00:01Z", "updated_at": "2026-08-18T12:00:01Z"},
            "future_updated": {"updated_at": "2026-08-18T12:00:01Z"},
            "created_before_issue": {"created_at": "2024-12-31T23:59:59Z", "updated_at": "2025-01-04T00:00:00Z"},
        }

        for name, changes in malformed.items():
            with self.subTest(name=name):
                candidate = issue()
                evidence = comment()
                evidence.update(changes)
                candidate["comments"] = [evidence]
                with self.assertRaisesRegex(ValueError, "ambiguous comment evidence"):
                    self.classify(candidate)

    def test_linear_comment_update_clock_skew_is_accepted_and_uses_newest_timestamp(self):
        candidate = issue()
        candidate["comments"] = [comment(
            created_at="2025-01-04T00:00:00.900Z",
            updated_at="2025-01-04T00:00:00.100Z",
        )]

        result = self.classify(candidate)

        self.assertEqual([entry.identifier for entry in result.candidates], ["OPS-100"])
        self.assertEqual(result.candidates[0].last_activity_at, "2025-01-04T00:00:00.900000Z")

    def test_contradictory_terminal_timestamps_fail_closed(self):
        for state_type, contradictory_field in (
            ("completed", "canceled_at"),
            ("canceled", "completed_at"),
        ):
            with self.subTest(state_type=state_type):
                candidate = issue()
                candidate.update(
                    {
                        "state_type": state_type,
                        "completed_at": "2025-01-02T00:00:00Z"
                        if state_type == "completed"
                        else None,
                        "canceled_at": "2025-01-02T00:00:00Z"
                        if state_type == "canceled"
                        else None,
                    }
                )
                candidate[contradictory_field] = "2025-01-02T00:00:00Z"
                with self.assertRaisesRegex(ValueError, "ambiguous issue evidence"):
                    self.classify_inventory_direct([candidate, successor()])

    def test_non_http_canonical_pointers_are_protected(self):
        pointers = (
            "See www.example.com/runbooks/current for the retained record",
            "Contact mailto:operations@example.com",
            "Use //example.com/runbooks/current as the source",
            "See [the runbook](www.example.com/runbooks/current)",
            "Contact [Operations](mailto:operations@example.com)",
            "See [the source](//example.com/runbooks/current)",
            "Open slack://channel/incident-room",
            "Restore s3://evidence-bucket/manifest.json",
            "Inspect file:///private/runbook.md",
        )

        for pointer in pointers:
            with self.subTest(pointer=pointer):
                candidate = issue()
                candidate["description"] = pointer
                result = self.classify(candidate)
                self.assertEqual(result.candidates, ())
                self.assertIn(
                    "canonical_pointer",
                    result.reasons_by_identifier["OPS-100"],
                )

    def test_dotted_prose_and_plain_email_are_not_canonical_pointers(self):
        ordinary_text = (
            "The service moved from version 1.2.3 to 1.2.4.",
            "Ask operations@example.com whether this can be removed.",
            "The example.com domain is mentioned without a path.",
            "The www.example.com host is mentioned without a path.",
        )

        for text in ordinary_text:
            with self.subTest(text=text):
                candidate = issue()
                candidate["description"] = text
                result = self.classify(candidate)
                self.assertEqual(
                    [entry.identifier for entry in result.candidates],
                    ["OPS-100"],
                )
                self.assertNotIn(
                    "canonical_pointer",
                    result.reasons_by_identifier["OPS-100"],
                )

    def classify_inventory_direct(self, inventory: list[dict]):
        return classify_inventory(
            inventory,
            successor_attestations=attestations(),
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )

    def test_manifest_order_hash_and_bytes_are_deterministic(self):
        second = issue("OPS-101")
        mapping = attestations() | {
            "OPS-101": {"successor": "OPS-999", "verified": True}
        }
        first_result = classify_inventory(
            [second, successor(), issue()],
            successor_attestations=mapping,
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )
        second_result = classify_inventory(
            [issue(), second, successor()],
            successor_attestations=deepcopy(mapping),
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )

        first = build_manifest(first_result)
        second_manifest = build_manifest(second_result)

        self.assertEqual(first, second_manifest)
        self.assertEqual(
            [item["identifier"] for item in first["candidates"]],
            ["OPS-100", "OPS-101"],
        )
        payload = dict(first)
        digest = payload.pop("sha256")
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        self.assertEqual(digest, expected)

    def test_same_immutable_envelope_produces_deterministic_bytes(self):
        candidate = issue()
        mapping = attestations()
        result = classify_inventory(
            [candidate, successor()],
            successor_attestations=mapping,
            minimum_age_days=180,
            as_of=AS_OF,
            team_id="team-ops",
            team_key="OPS",
        )

        first = json.dumps(build_manifest(result), indent=2, sort_keys=True).encode("ascii")
        candidate["title"] = "mutated after validation"
        mapping["OPS-100"]["successor"] = "OPS-404"
        second = json.dumps(build_manifest(result), indent=2, sort_keys=True).encode("ascii")

        self.assertEqual(first, second)
        with self.assertRaises((AttributeError, TypeError)):
            result.envelope.issues[0].comments += (comment(),)


class RetentionInventoryReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_rejects_incomplete_project_name(self):
        client = mock.MagicMock()
        detail = self.detail_payload()
        detail["project"] = {"name": ""}
        client.graphql = mock.AsyncMock(side_effect=[
            {"issue": detail},
        ])

        with self.assertRaisesRegex(LinearAPIError, "project evidence"):
            await RetentionInventoryReader(client)._read_issue(
                "id-OPS-100", "OPS-100", "team-ops"
            )

    async def test_reader_uses_queries_only_and_paginates_human_comments(self):
        client = mock.MagicMock()
        client.graphql = mock.AsyncMock(
            side_effect=[
                {
                    "team": {
                        "id": "team-ops",
                        "key": "OPS",
                        "issues": {
                            "nodes": [{"id": "id-OPS-100", "identifier": "OPS-100"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
                {"issue": self.detail_payload()},
                {
                    "issue": {
                        "id": "id-OPS-100",
                        "comments": {
                            "nodes": [
                                api_comment("c-bot", body="done", app=True)
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "comments-2"},
                        },
                    },
                },
                {
                    "issue": {
                        "id": "id-OPS-100",
                        "comments": {
                            "nodes": [
                                api_comment("c-human", body="retain", app=False)
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
                {"issue": self.detail_payload()},
                {
                    "issue": {
                        "id": "id-OPS-100",
                        "comments": {
                            "nodes": [
                                api_comment("c-bot", body="done", app=True)
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "comments-2"},
                        },
                    },
                },
                {
                    "issue": {
                        "id": "id-OPS-100",
                        "comments": {
                            "nodes": [
                                api_comment("c-human", body="retain", app=False)
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
                {
                    "team": {
                        "id": "team-ops",
                        "key": "OPS",
                        "issues": {
                            "nodes": [{"id": "id-OPS-100", "identifier": "OPS-100"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
            ]
        )

        result = await RetentionInventoryReader(client).read_team("team-ops", "OPS")

        self.assertEqual(len(result), 1)
        self.assertEqual(
            [comment["author_is_app"] for comment in result[0]["comments"]],
            [True, False],
        )
        self.assertEqual(
            result[0]["comments"][0]["created_at"], "2025-01-04T00:00:00Z"
        )
        queries = [call.args[0] for call in client.graphql.await_args_list]
        self.assertTrue(all("mutation" not in query.casefold() for query in queries))
        self.assertTrue(all("includeArchived: true" in query for query in queries))
        comment_calls = [
            call for call in client.graphql.await_args_list
            if "LinearRetentionComments" in call.args[0]
        ]
        self.assertEqual(comment_calls[-1].args[1]["after"], "comments-2")
        self.assertIsNone(client.graphql.await_args_list[-1].args[1]["after"])

    async def test_reader_rejects_new_candidate_protection_evidence_on_revalidation(self):
        changes = {
            "human comment": None,
            "relation": "relations",
            "attachment": "attachments",
        }

        for name, connection_name in changes.items():
            with self.subTest(name=name):
                second_detail = deepcopy(self.detail_payload())
                second_comments = []
                if name == "human comment":
                    second_comments = [
                        api_comment("c-human", body="retain", app=False)
                    ]
                else:
                    second_detail[connection_name]["nodes"] = [{"id": f"new-{name}"}]
                client = self.drifting_client(second_detail, second_comments)

                await self.assert_drift_aborts_before_output(
                    client, "changed during revalidation"
                )

    async def test_reader_rejects_updated_at_change_on_revalidation(self):
        second_detail = deepcopy(self.detail_payload())
        second_detail["updatedAt"] = "2026-08-18T11:59:59Z"
        client = self.drifting_client(second_detail, [])

        await self.assert_drift_aborts_before_output(client, "changed during revalidation")

    async def test_reader_rejects_comment_timestamp_change_on_revalidation(self):
        first_comment = api_comment("c-app", body="done", app=True)
        second_comment = api_comment(
            "c-app",
            body="done",
            app=True,
            updated_at="2025-01-05T00:00:00Z",
        )
        client = mock.MagicMock()
        client.connect = mock.AsyncMock()
        client.close = mock.AsyncMock()
        membership = {
            "team": {
                "id": "team-ops",
                "key": "OPS",
                "issues": {
                    "nodes": [{"id": "id-OPS-100", "identifier": "OPS-100"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }

        def comments_payload(value: dict) -> dict:
            return {
                "issue": {
                    "id": "id-OPS-100",
                    "comments": {
                        "nodes": [value],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }

        client.graphql = mock.AsyncMock(side_effect=[
            membership,
            {"issue": self.detail_payload()},
            comments_payload(first_comment),
            {"issue": self.detail_payload()},
            comments_payload(second_comment),
        ])

        await self.assert_drift_aborts_before_output(client, "changed during revalidation")

    async def test_reader_rejects_identity_change_on_revalidation(self):
        second_detail = deepcopy(self.detail_payload())
        second_detail["identifier"] = "OPS-101"
        client = self.drifting_client(second_detail, [])

        await self.assert_drift_aborts_before_output(client, "identity changed")

    async def test_reader_rejects_added_inventory_membership_before_output(self):
        client = self.membership_drifting_client(
            [
                {
                    "team": {
                        "id": "team-ops",
                        "key": "OPS",
                        "issues": {
                            "nodes": [{"id": "id-OPS-100", "identifier": "OPS-100"}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "inventory-2"},
                        },
                    },
                },
                {
                    "team": {
                        "id": "team-ops",
                        "key": "OPS",
                        "issues": {
                            "nodes": [{"id": "id-OPS-101", "identifier": "OPS-101"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
            ]
        )

        await self.assert_drift_aborts_before_output(client, "inventory membership changed")
        self.assertEqual(client.graphql.await_args_list[-1].args[1]["after"], "inventory-2")

    async def test_reader_rejects_removed_inventory_membership_before_output(self):
        client = self.membership_drifting_client(
            [
                {
                    "team": {
                        "id": "team-ops",
                        "key": "OPS",
                        "issues": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                }
            ]
        )

        await self.assert_drift_aborts_before_output(client, "inventory membership changed")

    async def test_reader_rejects_reordered_final_membership_before_output(self):
        client = mock.MagicMock()
        client.connect = mock.AsyncMock()
        client.close = mock.AsyncMock()
        first_page = {
            "team": {
                "id": "team-ops",
                "key": "OPS",
                "issues": {
                    "nodes": [
                        {"id": "id-OPS-100", "identifier": "OPS-100"},
                        {"id": "id-OPS-101", "identifier": "OPS-101"},
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
        second_payload = deepcopy(self.detail_payload())
        second_payload.update({"id": "id-OPS-101", "identifier": "OPS-101"})
        empty_comments = lambda issue_id: {
            "issue": {
                "id": issue_id,
                "comments": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
        client.graphql = mock.AsyncMock(side_effect=[
            first_page,
            {"issue": self.detail_payload()}, empty_comments("id-OPS-100"),
            {"issue": second_payload}, empty_comments("id-OPS-101"),
            {"issue": self.detail_payload()}, empty_comments("id-OPS-100"),
            {"issue": second_payload}, empty_comments("id-OPS-101"),
            {
                "team": {
                    "id": "team-ops",
                    "key": "OPS",
                    "issues": {
                        "nodes": [
                            {"id": "id-OPS-101", "identifier": "OPS-101"},
                            {"id": "id-OPS-100", "identifier": "OPS-100"},
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            },
        ])

        await self.assert_drift_aborts_before_output(client, "inventory membership changed")

    async def test_reader_rejects_pagination_drift(self):
        client = mock.MagicMock()
        client.graphql = mock.AsyncMock(return_value={
            "team": {
                "id": "team-ops",
                "key": "OPS",
                "issues": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": None},
                },
            }
        })

        with self.assertRaisesRegex(LinearAPIError, "pagination"):
            await RetentionInventoryReader(client).read_team("team-ops", "OPS")

    @staticmethod
    def detail_payload() -> dict:
        return {
            "id": "id-OPS-100",
            "identifier": "OPS-100",
            "title": "Old record",
            "description": "Redundant",
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": "2025-01-03T00:00:00Z",
            "completedAt": "2025-01-02T00:00:00Z",
            "canceledAt": None,
            "state": {"type": "completed", "name": "Done"},
            "team": {"id": "team-ops"},
            "project": {"name": "Maintenance"},
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "parent": None,
            "children": {"nodes": []},
            "relations": {"nodes": []},
            "inverseRelations": {"nodes": []},
            "attachments": {"nodes": []},
            "documents": {"nodes": []},
        }

    def drifting_client(self, second_detail: dict, second_comments: list[dict]):
        client = mock.MagicMock()
        client.connect = mock.AsyncMock()
        client.close = mock.AsyncMock()
        client.graphql = mock.AsyncMock(
            side_effect=[
                {
                    "team": {
                        "id": "team-ops",
                        "key": "OPS",
                        "issues": {
                            "nodes": [{"id": "id-OPS-100", "identifier": "OPS-100"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
                {"issue": self.detail_payload()},
                {
                    "issue": {
                        "id": "id-OPS-100",
                        "comments": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
                {"issue": second_detail},
                {
                    "issue": {
                        "id": "id-OPS-100",
                        "comments": {
                            "nodes": second_comments,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
            ]
        )
        return client

    def membership_drifting_client(self, final_inventory_pages: list[dict]):
        client = self.drifting_client(self.detail_payload(), [])
        client.graphql.side_effect = [
            *client.graphql.side_effect,
            *final_inventory_pages,
        ]
        return client

    async def assert_drift_aborts_before_output(self, client, error_pattern: str) -> None:
        args = SimpleNamespace(
            oauth_file="unused-oauth.json",
            team_id="team-ops",
            team_key="OPS",
            successors="unused-successors.json",
            minimum_age_days=180,
            as_of="2026-08-18T12:00:00Z",
            output="unused-manifest.json",
        )
        with mock.patch("retention._write_private_json") as writer:
            with self.assertRaisesRegex(LinearAPIError, error_pattern):
                await _run(
                    args,
                    client_factory=lambda **_kwargs: client,
                    reader_factory=RetentionInventoryReader,
                    now=lambda: AS_OF,
                )

        writer.assert_not_called()
        client.close.assert_awaited_once()


class RetentionCliTests(unittest.TestCase):
    def test_successor_attestation_reader_rejects_symlink_and_unsafe_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real.json"
            real.write_text("{}\n", encoding="utf-8")
            real.chmod(0o600)
            link = root / "link.json"
            link.symlink_to(real)

            with self.assertRaisesRegex(ValueError, "successor attestations"):
                _read_json_object(link, "successor attestations")

            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o700)
            unsafe.chmod(0o770)
            candidate = unsafe / "successors.json"
            candidate.write_text("{}\n", encoding="utf-8")
            candidate.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "successor attestations"):
                _read_json_object(candidate, "successor attestations")

    def test_manifest_writer_rejects_group_or_world_writable_parent(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "unsafe"
            parent.mkdir(mode=0o700)
            parent.chmod(0o770)

            with self.assertRaisesRegex(ValueError, "manifest parent"):
                _write_private_json(
                    parent / "manifest.json",
                    {"schema": "linear-operations-retention-dry-run/v1", "sha256": "0" * 64},
                )

    def test_manifest_writer_refuses_to_replace_an_unrelated_file(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "existing.json"
            output.write_text('{"access_token":"do-not-touch"}\n', encoding="utf-8")
            original = output.read_bytes()

            with self.assertRaisesRegex(ValueError, "existing retention manifest"):
                _write_private_json(
                    output,
                    {"schema": "linear-operations-retention-dry-run/v1", "sha256": "0" * 64},
                )

            self.assertEqual(output.read_bytes(), original)

    def test_cli_requires_explicit_output_and_writes_private_idempotent_manifest(self):
        inventory = [issue(), successor()]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oauth = root / "linear-oauth.json"
            oauth.write_text("{}", encoding="utf-8")
            oauth.chmod(0o600)
            attest = root / "successors.json"
            attest.write_text(json.dumps(attestations()), encoding="utf-8")
            output = root / "manifest.json"
            factory = mock.Mock()
            client = mock.AsyncMock()
            client.connect = mock.AsyncMock()
            client.close = mock.AsyncMock()
            factory.return_value = client
            reader = mock.MagicMock()
            reader.read_team = mock.AsyncMock(return_value=inventory)

            args = [
                "--oauth-file", str(oauth),
                "--team-id", "team-ops",
                "--team-key", "OPS",
                "--successors", str(attest),
                "--minimum-age-days", "180",
                "--as-of", "2026-08-18T12:00:00Z",
                "--output", str(output),
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    args,
                    client_factory=factory,
                    reader_factory=lambda _client: reader,
                    now=lambda: AS_OF,
                )
            first_bytes = output.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                second_code = main(
                    args,
                    client_factory=factory,
                    reader_factory=lambda _client: reader,
                    now=lambda: AS_OF,
                )

            self.assertEqual((code, second_code), (0, 0))
            self.assertEqual(first_bytes, output.read_bytes())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["candidate_count"], 1)
            self.assertNotIn("access_token", stdout.getvalue())
            self.assertNotIn("refresh_token", stdout.getvalue())
            factory.assert_called_with(oauth_file=str(oauth))
            client.connect.assert_awaited()
            client.close.assert_awaited()

    def test_cli_rejects_future_as_of_and_accepts_exact_run_clock(self):
        reader = mock.MagicMock()
        reader.read_team = mock.AsyncMock(return_value=[issue(), successor()])
        client = mock.AsyncMock()
        client.connect = mock.AsyncMock()
        client.close = mock.AsyncMock()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            attest = root / "successors.json"
            attest.write_text(json.dumps(attestations()), encoding="utf-8")
            base = [
                "--oauth-file", str(root / "oauth.json"),
                "--team-id", "team-ops",
                "--team-key", "OPS",
                "--successors", str(attest),
                "--minimum-age-days", "180",
            ]
            with self.assertRaisesRegex(ValueError, "as_of cannot be in the future"):
                main(
                    base + [
                        "--as-of", "2026-08-18T12:00:00.000001Z",
                        "--output", str(root / "future.json"),
                    ],
                    client_factory=lambda **_kwargs: client,
                    reader_factory=lambda _client: reader,
                    now=lambda: AS_OF,
                )

            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    base + [
                        "--as-of", "2026-08-18T12:00:00Z",
                        "--output", str(root / "exact.json"),
                    ],
                    client_factory=lambda **_kwargs: client,
                    reader_factory=lambda _client: reader,
                    now=lambda: AS_OF,
                )
            self.assertEqual(code, 0)

    def test_cli_without_output_is_rejected(self):
        with self.assertRaises(SystemExit):
            main([])


if __name__ == "__main__":
    unittest.main()
