"""Fail-closed, read-only retention classification for Linear Operations issues."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

try:
    from .linear_client import LinearClient
    from .oauth_store import LinearAPIError
except ImportError:  # Direct loading by the profile-local script and tests.
    from linear_client import LinearClient
    from oauth_store import LinearAPIError


TERMINAL_STATE_TYPES = frozenset({"completed", "canceled"})
REQUIRED_EVIDENCE = frozenset(
    {
        "id",
        "identifier",
        "title",
        "description",
        "created_at",
        "updated_at",
        "completed_at",
        "canceled_at",
        "state_type",
        "state_name",
        "team_id",
        "project_name",
        "labels",
        "parent_count",
        "child_count",
        "relation_count",
        "inverse_relation_count",
        "attachment_count",
        "document_count",
        "comments",
    }
)
COUNT_FIELDS = (
    "parent_count",
    "child_count",
    "relation_count",
    "inverse_relation_count",
    "attachment_count",
    "document_count",
)
PROTECTED_SEMANTICS_RE = re.compile(
    r"\b(?:decisions?|decision-log|architectural decision|adr|security|credentials?|secrets?|"
    r"access review|privacy|threat|risk acceptance|incidents?|post-?mortems?|"
    r"outage|breach|vulnerabilit(?:y|ies)|cve|root cause|rca|forensic|sev[ _-]?[0-4])\b",
    re.IGNORECASE,
)
INBOX_RE = re.compile(r"\b(?:operational|operations|ops)[ _-]*inbox\b", re.IGNORECASE)
POINTER_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*:(?://|[^\s<>()]+)|"
    r"(?<![\w@])www\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?::\d+)?/[^\s<>()]+|"
    r"(?<![:\w])//(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?::\d+)?/[^\s<>()]+|"
    r"\b[A-Z][A-Z0-9]*-\d+\b|"
    r"\b(?:canonical|source[ _-]+of[ _-]+truth|system[ _-]+of[ _-]+record)\b",
    re.IGNORECASE,
)
MAX_PAGES = 100


@dataclass(frozen=True)
class CommentEvidence:
    id: str
    body: str
    author_is_app: bool | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IssueEvidence:
    id: str
    identifier: str
    title: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    canceled_at: datetime | None
    state_type: str
    state_name: str
    team_id: str
    project_name: str | None
    labels: tuple[str, ...]
    parent_count: int
    child_count: int
    relation_count: int
    inverse_relation_count: int
    attachment_count: int
    document_count: int
    comments: tuple[CommentEvidence, ...]


@dataclass(frozen=True)
class SuccessorAttestation:
    source_identifier: str
    successor_identifier: str
    verified: bool


@dataclass(frozen=True)
class RetentionEvidenceEnvelope:
    team_id: str
    team_key: str
    as_of: datetime
    minimum_age_days: int
    issues: tuple[IssueEvidence, ...]
    successor_attestations: tuple[SuccessorAttestation, ...]


@dataclass(frozen=True)
class Candidate:
    id: str
    identifier: str
    canonical_successor: str
    state_type: str
    last_activity_at: str
    age_days: int


@dataclass(frozen=True)
class ClassificationResult:
    envelope: RetentionEvidenceEnvelope
    candidates: tuple[Candidate, ...]
    reasons_by_identifier: Mapping[str, tuple[str, ...]]
    summary: Mapping[str, Any]


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("ambiguous issue evidence: timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ambiguous issue evidence: timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("ambiguous issue evidence: timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _validate_issue(raw: Any, *, as_of: datetime, team_id: str) -> IssueEvidence:
    if not isinstance(raw, dict) or not REQUIRED_EVIDENCE.issubset(raw):
        raise ValueError("ambiguous issue evidence: required fields are missing")
    for field in ("id", "identifier", "title", "state_type", "state_name", "team_id"):
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ValueError(f"ambiguous issue evidence: {field} is invalid")
    for field in ("description", "project_name"):
        if raw[field] is not None and not isinstance(raw[field], str):
            raise ValueError(f"ambiguous issue evidence: {field} is invalid")
    if not isinstance(raw["labels"], list) or any(
        not isinstance(label, str) or not label.strip() for label in raw["labels"]
    ):
        raise ValueError("ambiguous issue evidence: labels are invalid")
    for field in COUNT_FIELDS:
        if isinstance(raw[field], bool) or not isinstance(raw[field], int) or raw[field] < 0:
            raise ValueError(f"ambiguous issue evidence: {field} is invalid")
    if raw["team_id"] != team_id:
        raise ValueError("ambiguous issue evidence: team_id is invalid")
    if not isinstance(raw["comments"], list):
        raise ValueError("ambiguous issue evidence: comments are invalid")
    created_at = _parse_timestamp(raw["created_at"])
    updated_at = _parse_timestamp(raw["updated_at"])
    if updated_at < created_at or updated_at > as_of:
        raise ValueError("ambiguous issue evidence: timestamp chronology is invalid")
    comments: list[CommentEvidence] = []
    comment_ids: set[str] = set()
    for comment in raw["comments"]:
        if (
            not isinstance(comment, dict)
            or not {"id", "body", "author_is_app", "created_at", "updated_at"}.issubset(comment)
            or not isinstance(comment.get("id"), str)
            or not comment.get("id")
            or not isinstance(comment.get("body"), str)
            or not any(
                comment.get("author_is_app") is allowed
                for allowed in (True, False, None)
            )
        ):
            raise ValueError("ambiguous comment evidence: required fields are missing or invalid")
        try:
            comment_created_at = _parse_timestamp(comment["created_at"])
            comment_updated_at = _parse_timestamp(comment["updated_at"])
        except ValueError as exc:
            raise ValueError("ambiguous comment evidence: timestamp is missing or invalid") from exc
        if (
            comment["id"] in comment_ids
            or comment_created_at < created_at
            or comment_updated_at < created_at
            or comment_created_at > as_of
            or comment_updated_at > as_of
        ):
            raise ValueError("ambiguous comment evidence: timestamp chronology is invalid")
        comment_ids.add(comment["id"])
        comments.append(
            CommentEvidence(
                id=comment["id"],
                body=comment["body"],
                author_is_app=comment["author_is_app"],
                created_at=comment_created_at,
                updated_at=comment_updated_at,
            )
        )
    terminal_timestamps: dict[str, datetime] = {}
    for field in ("completed_at", "canceled_at"):
        if raw[field] is not None:
            terminal_timestamps[field] = _parse_timestamp(raw[field])
    if len(terminal_timestamps) > 1:
        raise ValueError("ambiguous issue evidence: terminal timestamps conflict")
    if any(
        timestamp < created_at or timestamp > as_of
        for timestamp in terminal_timestamps.values()
    ):
        raise ValueError("ambiguous issue evidence: timestamp chronology is invalid")
    state_type = raw["state_type"].casefold()
    if (
        state_type == "completed" and raw["canceled_at"] is not None
    ) or (state_type == "canceled" and raw["completed_at"] is not None):
        raise ValueError("ambiguous issue evidence: terminal timestamp contradicts state")
    return IssueEvidence(
        id=raw["id"],
        identifier=raw["identifier"],
        title=raw["title"],
        description=raw["description"],
        created_at=created_at,
        updated_at=updated_at,
        completed_at=terminal_timestamps.get("completed_at"),
        canceled_at=terminal_timestamps.get("canceled_at"),
        state_type=raw["state_type"],
        state_name=raw["state_name"],
        team_id=raw["team_id"],
        project_name=raw["project_name"],
        labels=tuple(raw["labels"]),
        parent_count=raw["parent_count"],
        child_count=raw["child_count"],
        relation_count=raw["relation_count"],
        inverse_relation_count=raw["inverse_relation_count"],
        attachment_count=raw["attachment_count"],
        document_count=raw["document_count"],
        comments=tuple(sorted(comments, key=lambda item: item.id)),
    )


def _last_activity(issue: IssueEvidence) -> datetime:
    timestamps = [issue.created_at, issue.updated_at]
    timestamps.extend(
        timestamp for timestamp in (issue.completed_at, issue.canceled_at) if timestamp is not None
    )
    timestamps.extend(
        timestamp
        for comment in issue.comments
        for timestamp in (comment.created_at, comment.updated_at)
    )
    return max(timestamps)


def _build_envelope(
    inventory: Sequence[dict[str, Any]],
    *,
    successor_attestations: dict[str, Any],
    minimum_age_days: int,
    as_of: datetime,
    team_id: str,
    team_key: str,
) -> RetentionEvidenceEnvelope:
    if minimum_age_days < 1:
        raise ValueError("minimum_age_days must be positive")
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    if not isinstance(successor_attestations, dict):
        raise ValueError("successor attestations must be an object")
    if not isinstance(team_id, str) or not team_id.strip():
        raise ValueError("team_id must be non-empty")
    if not isinstance(team_key, str) or not team_key.strip():
        raise ValueError("team_key must be non-empty")
    as_of_utc = as_of.astimezone(timezone.utc)
    issues = tuple(
        sorted(
            (_validate_issue(raw, as_of=as_of_utc, team_id=team_id) for raw in inventory),
            key=lambda item: (item.identifier, item.id),
        )
    )
    identifiers: set[str] = set()
    ids: set[str] = set()
    for current in issues:
        if current.identifier in identifiers or current.id in ids:
            raise ValueError("ambiguous inventory: duplicate issue identity")
        identifiers.add(current.identifier)
        ids.add(current.id)
    attestations: list[SuccessorAttestation] = []
    for identifier in sorted(identifiers):
        raw_attestation = successor_attestations.get(identifier)
        verified = isinstance(raw_attestation, dict) and raw_attestation.get("verified") is True
        supplied = raw_attestation.get("successor") if isinstance(raw_attestation, dict) else None
        successor_identifier = supplied.strip() if isinstance(supplied, str) else ""
        attestations.append(
            SuccessorAttestation(identifier, successor_identifier, verified)
        )
    return RetentionEvidenceEnvelope(
        team_id=team_id,
        team_key=team_key,
        as_of=as_of_utc,
        minimum_age_days=minimum_age_days,
        issues=issues,
        successor_attestations=tuple(attestations),
    )


def classify_inventory(
    inventory: Sequence[dict[str, Any]],
    *,
    successor_attestations: dict[str, Any],
    minimum_age_days: int,
    as_of: datetime,
    team_id: str,
    team_key: str,
) -> ClassificationResult:
    """Classify a complete inventory; uncertainty protects an issue or aborts the run."""

    envelope = _build_envelope(
        inventory,
        successor_attestations=successor_attestations,
        minimum_age_days=minimum_age_days,
        as_of=as_of,
        team_id=team_id,
        team_key=team_key,
    )
    by_identifier = {issue.identifier: issue for issue in envelope.issues}
    attestations_by_source = {
        attestation.source_identifier: attestation
        for attestation in envelope.successor_attestations
    }

    retained_successors: set[str] = set()
    for source_identifier, attestation in attestations_by_source.items():
        source_issue = by_identifier.get(source_identifier)
        if not attestation.verified:
            continue
        successor_identifier = attestation.successor_identifier
        successor_issue = by_identifier.get(successor_identifier)
        if (
            source_issue is not None
            and successor_issue is not None
            and successor_identifier != source_identifier
            and successor_issue.team_id == source_issue.team_id
        ):
            retained_successors.add(successor_identifier)

    reasons_by_identifier: dict[str, tuple[str, ...]] = {}
    candidates: list[Candidate] = []
    reason_counts: dict[str, int] = {}
    for current in envelope.issues:
        reasons: set[str] = set()
        if current.identifier in retained_successors:
            reasons.add("verified_canonical_successor")
        state_type = current.state_type.casefold()
        if state_type not in TERMINAL_STATE_TYPES:
            reasons.add("active_or_nonterminal")
        elif (
            state_type == "completed" and current.completed_at is None
        ) or (state_type == "canceled" and current.canceled_at is None):
            reasons.add("ambiguous_terminal_timestamp")

        searchable = "\n".join(
            [
                current.title,
                current.description or "",
                current.project_name or "",
                current.state_name,
            ]
            + list(current.labels)
        )
        content_with_comments = searchable + "\n" + "\n".join(
            comment.body for comment in current.comments
        )
        if INBOX_RE.search(searchable):
            reasons.add("operational_inbox")
        if any(comment.author_is_app is False for comment in current.comments):
            reasons.add("human_discussion")
        if any(comment.author_is_app is None for comment in current.comments):
            reasons.add("ambiguous_comment_authorship")
        if PROTECTED_SEMANTICS_RE.search(content_with_comments):
            reasons.add("decision_security_or_incident_semantics")
        if any(getattr(current, field) for field in COUNT_FIELDS[:4]):
            reasons.add("dependency_or_relation")
        if current.attachment_count:
            reasons.add("attachment")
        if current.document_count:
            reasons.add("document")
        if POINTER_RE.search(content_with_comments):
            reasons.add("canonical_pointer")

        last_activity = _last_activity(current)
        age_seconds = (envelope.as_of - last_activity).total_seconds()
        age_days = int(age_seconds // 86400)
        if age_seconds < minimum_age_days * 86400:
            reasons.add("too_young")

        attestation = attestations_by_source[current.identifier]
        successor_identifier = attestation.successor_identifier if attestation.verified else ""
        successor_issue = by_identifier.get(successor_identifier)
        if (
            not successor_identifier
            or successor_identifier == current.identifier
            or successor_issue is None
            or successor_issue.team_id != current.team_id
        ):
            reasons.add("no_verified_canonical_successor")

        ordered_reasons = tuple(sorted(reasons))
        reasons_by_identifier[current.identifier] = ordered_reasons
        if ordered_reasons:
            for reason in ordered_reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        else:
            candidates.append(
                Candidate(
                    id=current.id,
                    identifier=current.identifier,
                    canonical_successor=successor_identifier,
                    state_type=state_type,
                    last_activity_at=last_activity.isoformat().replace("+00:00", "Z"),
                    age_days=age_days,
                )
            )

    candidates.sort(key=lambda item: (item.identifier, item.id))
    summary = MappingProxyType({
        "inventory_count": len(envelope.issues),
        "candidate_count": len(candidates),
        "protected_count": len(envelope.issues) - len(candidates),
        "protected_reason_counts": MappingProxyType(dict(sorted(reason_counts.items()))),
    })
    return ClassificationResult(
        envelope=envelope,
        candidates=tuple(candidates),
        reasons_by_identifier=MappingProxyType(reasons_by_identifier),
        summary=summary,
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def build_manifest(
    result: ClassificationResult,
) -> dict[str, Any]:
    envelope = result.envelope
    summary = {
        **result.summary,
        "protected_reason_counts": dict(result.summary["protected_reason_counts"]),
    }
    payload = {
        "schema": "linear-operations-retention-dry-run/v1",
        "mode": "read-only-dry-run",
        "team_id": envelope.team_id,
        "team_key": envelope.team_key,
        "as_of": envelope.as_of.isoformat().replace("+00:00", "Z"),
        "minimum_age_days": envelope.minimum_age_days,
        "summary": summary,
        "candidates": [
            {
                "id": candidate.id,
                "identifier": candidate.identifier,
                "canonical_successor": candidate.canonical_successor,
                "state_type": candidate.state_type,
                "last_activity_at": candidate.last_activity_at,
                "age_days": candidate.age_days,
            }
            for candidate in result.candidates
        ],
    }
    return {**payload, "sha256": hashlib.sha256(_canonical_json(payload)).hexdigest()}


def _connection_nodes(value: Any, name: str) -> list[Any]:
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        raise LinearAPIError(f"Retention {name} evidence was incomplete")
    return value["nodes"]


def _page(value: Any, name: str) -> tuple[list[Any], bool, str | None]:
    nodes = _connection_nodes(value, name)
    page_info = value.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
        raise LinearAPIError(f"Retention {name} pagination was incomplete")
    cursor = page_info.get("endCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise LinearAPIError(f"Retention {name} pagination was incomplete")
    return nodes, page_info["hasNextPage"], cursor


class RetentionInventoryReader:
    """Read complete policy evidence without exposing any mutation operation."""

    def __init__(self, client: LinearClient) -> None:
        self.client = client

    async def read_team(self, team_id: str, expected_team_key: str) -> list[dict[str, Any]]:
        issue_refs = await self._read_issue_refs(team_id, expected_team_key)
        inventory = []
        for issue_id, identifier in issue_refs:
            inventory.append(await self._read_issue(issue_id, identifier, team_id))
        for index, (issue_id, identifier) in enumerate(issue_refs):
            revalidated = await self._read_issue(issue_id, identifier, team_id)
            if revalidated != inventory[index]:
                raise LinearAPIError("Retention issue evidence changed during revalidation")
        revalidated_refs = await self._read_issue_refs(team_id, expected_team_key)
        if revalidated_refs != issue_refs:
            raise LinearAPIError("Retention issue inventory membership changed during revalidation")
        return inventory

    async def _read_issue_refs(
        self, team_id: str, expected_team_key: str
    ) -> list[tuple[str, str]]:
        issue_refs: list[tuple[str, str]] = []
        issue_ids: set[str] = set()
        identifiers: set[str] = set()
        after: str | None = None
        seen: set[str] = set()
        query = """
query LinearRetentionInventory($teamId: String!, $after: String) {
  team(id: $teamId) {
    id key
    issues(first: 50, after: $after, includeArchived: true) {
      nodes { id identifier }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
        for _ in range(MAX_PAGES):
            data = await self.client.graphql(query, {"teamId": team_id, "after": after})
            team = data.get("team")
            if (
                not isinstance(team, dict)
                or team.get("id") != team_id
                or team.get("key") != expected_team_key
            ):
                raise LinearAPIError("Retention team inventory could not be verified")
            nodes, has_next, cursor = _page(team.get("issues"), "issue inventory")
            for node in nodes:
                if (
                    not isinstance(node, dict)
                    or not isinstance(node.get("id"), str)
                    or not node["id"].strip()
                    or not isinstance(node.get("identifier"), str)
                    or not node["identifier"].strip()
                ):
                    raise LinearAPIError("Retention issue inventory was malformed")
                if node["id"] in issue_ids or node["identifier"] in identifiers:
                    raise LinearAPIError("Retention issue inventory contained duplicate identity")
                issue_refs.append((node["id"], node["identifier"]))
                issue_ids.add(node["id"])
                identifiers.add(node["identifier"])
            if not has_next:
                break
            if not cursor or cursor in seen:
                raise LinearAPIError("Retention issue inventory pagination did not advance")
            seen.add(cursor)
            after = cursor
        else:
            raise LinearAPIError("Retention issue inventory exceeded the page limit")
        return issue_refs

    async def _read_issue(self, issue_id: str, identifier: str, team_id: str) -> dict[str, Any]:
        detail_query = """
query LinearRetentionIssueEvidence($id: String!) {
  issue(id: $id) {
    id identifier title description createdAt updatedAt completedAt canceledAt
    state { type name }
    team { id }
    project { name }
    labels(first: 250, includeArchived: true) { nodes { name } pageInfo { hasNextPage endCursor } }
    parent { id }
    children(first: 1, includeArchived: true) { nodes { id } }
    relations(first: 1, includeArchived: true) { nodes { id } }
    inverseRelations(first: 1, includeArchived: true) { nodes { id } }
    attachments(first: 1, includeArchived: true) { nodes { id } }
    documents(first: 1, includeArchived: true) { nodes { id } }
  }
}
"""
        data = await self.client.graphql(detail_query, {"id": issue_id})
        current = data.get("issue")
        if (
            not isinstance(current, dict)
            or current.get("id") != issue_id
            or current.get("identifier") != identifier
            or not isinstance(current.get("team"), dict)
            or current["team"].get("id") != team_id
        ):
            raise LinearAPIError("Retention issue identity changed during inventory")
        labels, labels_more, _ = _page(current.get("labels"), "labels")
        if labels_more:
            raise LinearAPIError("Retention labels exceeded the evidence limit")
        label_names = []
        for label in labels:
            if not isinstance(label, dict) or not isinstance(label.get("name"), str) or not label["name"]:
                raise LinearAPIError("Retention label evidence was malformed")
            label_names.append(label["name"])

        def count(name: str) -> int:
            return len(_connection_nodes(current.get(name), name))

        parent = current.get("parent")
        if parent is not None and (
            not isinstance(parent, dict) or not isinstance(parent.get("id"), str) or not parent["id"]
        ):
            raise LinearAPIError("Retention parent evidence was malformed")
        state = current.get("state")
        if not isinstance(state, dict):
            raise LinearAPIError("Retention state evidence was incomplete")
        project = current.get("project")
        if project is not None and (
            not isinstance(project, dict)
            or not isinstance(project.get("name"), str)
            or not project["name"].strip()
        ):
            raise LinearAPIError("Retention project evidence was incomplete")
        comments = await self._read_comments(issue_id)
        return {
            "id": current.get("id"),
            "identifier": current.get("identifier"),
            "title": current.get("title"),
            "description": current.get("description"),
            "created_at": current.get("createdAt"),
            "updated_at": current.get("updatedAt"),
            "completed_at": current.get("completedAt"),
            "canceled_at": current.get("canceledAt"),
            "state_type": state.get("type"),
            "state_name": state.get("name"),
            "team_id": current["team"].get("id"),
            "project_name": project.get("name") if project else None,
            "labels": label_names,
            "parent_count": int(parent is not None),
            "child_count": count("children"),
            "relation_count": count("relations"),
            "inverse_relation_count": count("inverseRelations"),
            "attachment_count": count("attachments"),
            "document_count": count("documents"),
            "comments": comments,
        }

    async def _read_comments(self, issue_id: str) -> list[dict[str, Any]]:
        query = """
query LinearRetentionComments($id: String!, $after: String) {
  issue(id: $id) {
    id
    comments(first: 50, after: $after, includeArchived: true) {
      nodes { id body createdAt updatedAt user { app } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
        comments: list[dict[str, Any]] = []
        after: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            data = await self.client.graphql(query, {"id": issue_id, "after": after})
            current = data.get("issue")
            if not isinstance(current, dict) or current.get("id") != issue_id:
                raise LinearAPIError("Retention comment issue identity changed")
            nodes, has_next, cursor = _page(current.get("comments"), "comments")
            for node in nodes:
                if not isinstance(node, dict):
                    raise LinearAPIError("Retention comment evidence was malformed")
                user = node.get("user")
                author_is_app = user.get("app") if isinstance(user, dict) else None
                comments.append(
                    {
                        "id": node.get("id"),
                        "body": node.get("body"),
                        "author_is_app": author_is_app,
                        "created_at": node.get("createdAt"),
                        "updated_at": node.get("updatedAt"),
                    }
                )
            if not has_next:
                return comments
            if not cursor or cursor in seen:
                raise LinearAPIError("Retention comments pagination did not advance")
            seen.add(cursor)
            after = cursor
        raise LinearAPIError("Retention comments exceeded the page limit")


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        parent = path.parent.resolve(strict=True)
        parent_stat = parent.stat()
    except OSError as exc:
        raise ValueError(f"{description} is unavailable or invalid") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or parent_stat.st_mode & 0o022
    ):
        raise ValueError(f"{description} is unavailable or invalid")
    destination = parent / path.name
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open(destination, flags)
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or file_stat.st_mode & 0o077
        ):
            raise ValueError(f"{description} is unavailable or invalid")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} is unavailable or invalid") from exc
    finally:
        if "fd" in locals() and fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        raise ValueError(f"{description} must contain a JSON object")
    return value


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    parent = path.parent.resolve(strict=True)
    parent_stat = parent.stat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or parent_stat.st_mode & 0o022
    ):
        raise ValueError("manifest parent must be an owned directory")
    destination = parent / path.name
    try:
        existing_stat = destination.lstat()
    except FileNotFoundError:
        existing_stat = None
    if existing_stat is not None:
        if not stat.S_ISREG(existing_stat.st_mode) or existing_stat.st_uid != os.getuid():
            raise ValueError("manifest output must be an owned regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(destination, flags)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("manifest output may replace only an existing retention manifest") from exc
        if not isinstance(existing, dict) or existing.get("schema") != "linear-operations-retention-dry-run/v1":
            raise ValueError("manifest output may replace only an existing retention manifest")
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parse_as_of(value: str) -> datetime:
    parsed = _parse_timestamp(value)
    return parsed.astimezone(timezone.utc)


async def _run(
    args: argparse.Namespace,
    *,
    client_factory: Callable[..., LinearClient],
    reader_factory: Callable[[LinearClient], RetentionInventoryReader],
    now: Callable[[], datetime],
) -> dict[str, Any]:
    as_of = _parse_as_of(args.as_of)
    run_now = now()
    if run_now.tzinfo is None:
        raise ValueError("run clock must include a timezone")
    if as_of > run_now.astimezone(timezone.utc):
        raise ValueError("as_of cannot be in the future")
    client = client_factory(oauth_file=args.oauth_file)
    try:
        await client.connect()
        inventory = await reader_factory(client).read_team(args.team_id, args.team_key)
    finally:
        await client.close()
    attestations = _read_json_object(Path(args.successors), "successor attestations")
    result = classify_inventory(
        inventory,
        successor_attestations=attestations,
        minimum_age_days=args.minimum_age_days,
        as_of=as_of,
        team_id=args.team_id,
        team_key=args.team_key,
    )
    manifest = build_manifest(result)
    _write_private_json(Path(args.output), manifest)
    return {
        "inventory_count": result.summary["inventory_count"],
        "candidate_count": result.summary["candidate_count"],
        "protected_count": result.summary["protected_count"],
        "protected_reason_counts": dict(result.summary["protected_reason_counts"]),
        "manifest_sha256": manifest["sha256"],
        "output": args.output,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oauth-file", required=True, help="Profile-local Linear OAuth JSON path")
    parser.add_argument("--team-id", required=True, help="Exact Operations team UUID")
    parser.add_argument("--team-key", required=True, help="Exact Operations team key (for example OPS)")
    parser.add_argument("--successors", required=True, help="Verified successor attestation JSON")
    parser.add_argument("--minimum-age-days", required=True, type=int)
    parser.add_argument("--as-of", required=True, help="Fixed ISO-8601 cutoff for reproducible output")
    parser.add_argument("--output", required=True, help="Explicit JSON manifest output path")
    return parser


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., LinearClient] = LinearClient,
    reader_factory: Callable[[LinearClient], RetentionInventoryReader] = RetentionInventoryReader,
    now: Callable[[], datetime] = _utc_now,
) -> int:
    args = _parser().parse_args(argv)
    summary = asyncio.run(
        _run(
            args,
            client_factory=client_factory,
            reader_factory=reader_factory,
            now=now,
        )
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
