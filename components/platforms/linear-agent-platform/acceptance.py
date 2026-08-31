"""Acceptance-criteria parsing and fail-closed completion gates."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timezone

from markdown_it import MarkdownIt

_CHECKBOX_RE = re.compile(r"^[ \t]*[-+*][ \t]+\[([ xX])\][ \t]+(.+?)[ \t]*$")
_ACCEPTANCE_HEADINGS = {"kabul kriterleri", "acceptance criteria"}
_COMMONMARK = MarkdownIt("commonmark")


@dataclass(frozen=True)
class AcceptanceCriterion:
    text: str
    checked: bool
    criterion_hash: str


@dataclass(frozen=True)
class AcceptanceGateResult:
    allowed: bool
    reason: str
    criteria: tuple[AcceptanceCriterion, ...]


def acceptance_criteria(description: str) -> tuple[AcceptanceCriterion, ...]:
    """Return genuine bullet task items from the canonical acceptance section."""
    if not isinstance(description, str) or not description:
        return ()

    tokens = _COMMONMARK.parse(description)
    section_start: int | None = None
    section_end: int | None = None
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != "h2" or not token.map:
            continue
        heading_text = ""
        if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
            heading_text = str(tokens[index + 1].content or "").strip().casefold()
        if section_start is None:
            if heading_text in _ACCEPTANCE_HEADINGS:
                section_start = int(token.map[1])
            continue
        section_end = int(token.map[0])
        break
    if section_start is None:
        return ()

    lines = description.splitlines()
    if section_end is None:
        section_end = len(lines)
    checkbox_lines: set[int] = set()
    list_stack: list[str] = []
    for token in tokens:
        if token.type == "bullet_list_open":
            list_stack.append("bullet")
        elif token.type == "ordered_list_open":
            list_stack.append("ordered")
        elif token.type == "list_item_open" and list_stack and list_stack[-1] == "bullet":
            if token.map:
                line_index = int(token.map[0])
                if section_start <= line_index < section_end:
                    checkbox_lines.add(line_index)
        elif token.type in {"bullet_list_close", "ordered_list_close"} and list_stack:
            list_stack.pop()

    result: list[AcceptanceCriterion] = []
    text_occurrences: dict[str, int] = {}
    for line_index in sorted(checkbox_lines):
        if line_index >= len(lines):
            continue
        match = _CHECKBOX_RE.match(lines[line_index])
        if match is None:
            continue
        text = " ".join(match.group(2).split())
        if not text:
            continue
        occurrence = text_occurrences.get(text, 0) + 1
        text_occurrences[text] = occurrence
        identity = f"{text}\0{occurrence}"
        result.append(
            AcceptanceCriterion(
                text=text,
                checked=match.group(1).casefold() == "x",
                criterion_hash=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(result)


def validate_evidence_envelope(envelope: object) -> dict[str, str] | None:
    """Accept only metadata-safe, qualifying PASS evidence."""
    if not isinstance(envelope, dict):
        return None
    required = {
        "criterion_hash", "test_class", "evidence_digest", "evidence_pointer",
        "observed_revision", "result", "timestamp",
    }
    if set(envelope) != required:
        return None
    normalized = {key: str(envelope.get(key) or "") for key in required}
    qualifying = {"integration", "e2e", "live", "vendor", "file", "api", "runtime", "security"}
    hex_chars = frozenset("0123456789abcdef")
    try:
        observed_timestamp = datetime.fromisoformat(
            normalized["observed_revision"].replace("Z", "+00:00")
        )
        evidence_timestamp = datetime.fromisoformat(
            normalized["timestamp"].replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if (
        normalized["result"] != "PASS"
        or normalized["test_class"] not in qualifying
        or len(normalized["criterion_hash"]) != 64
        or any(character not in hex_chars for character in normalized["criterion_hash"])
        or len(normalized["evidence_digest"]) != 64
        or any(character not in hex_chars for character in normalized["evidence_digest"])
        or not normalized["evidence_pointer"].startswith(
            ("linear://", "artifact://", "vendor://", "sha256:")
        )
        or len(normalized["evidence_pointer"]) > 500
        or any(
            character.isspace() or ord(character) < 0x20
            for character in normalized["evidence_pointer"]
        )
        or not normalized["observed_revision"]
        or len(normalized["observed_revision"]) > 200
        or not normalized["timestamp"]
        or len(normalized["timestamp"]) > 200
        or observed_timestamp.tzinfo is None
        or evidence_timestamp.tzinfo is None
    ):
        return None
    return normalized


EvidenceResolver = Callable[[str], Mapping[str, object] | None]


def delegate_attestation_resolver(
    envelopes: object,
    *,
    issue_id: str,
    delegate_id: str,
) -> EvidenceResolver | None:
    """Create the built-in exact-delegate resolver for digest-addressed attestations.

    The authenticated tool invocation is the authority boundary. The model cannot
    nominate another actor or issue, and the pointer must be the exact digest it
    attests. Richer installation-specific resolvers may still dereference Linear,
    vendor, or artifact pointers.
    """
    if not isinstance(envelopes, list) or not issue_id or not delegate_id:
        return None
    records: dict[str, dict[str, str]] = {}
    for envelope in envelopes:
        normalized = validate_evidence_envelope(envelope)
        if normalized is None:
            return None
        pointer = normalized["evidence_pointer"]
        if not hmac.compare_digest(pointer, f"sha256:{normalized['evidence_digest']}"):
            return None
        if pointer in records:
            return None
        records[pointer] = {
            "evidence_pointer": pointer,
            "issue_id": issue_id,
            "delegate_id": delegate_id,
            "criterion_hash": normalized["criterion_hash"],
            "evidence_digest": normalized["evidence_digest"],
            "observed_revision": normalized["observed_revision"],
            "timestamp": normalized["timestamp"],
        }

    def resolve(pointer: str) -> Mapping[str, object] | None:
        return records.get(pointer)

    return resolve


def authenticate_evidence_envelope(
    envelope: object,
    *,
    issue_id: str,
    delegate_id: str,
    resolver: EvidenceResolver | None,
    now: datetime | None = None,
) -> dict[str, str] | None:
    """Resolve a pointer server-side and bind its trusted metadata to one criterion.

    The model-provided envelope is only a claim. The resolver is the authority for
    pointer ownership and digest metadata; absence, ambiguity, or any mismatch
    fails closed. Evidence bytes remain outside this ledger.
    """
    normalized = validate_evidence_envelope(envelope)
    if normalized is None or resolver is None or not issue_id or not delegate_id:
        return None
    try:
        resolved = resolver(normalized["evidence_pointer"])
    except Exception:
        return None
    required = {
        "evidence_pointer",
        "issue_id",
        "delegate_id",
        "criterion_hash",
        "evidence_digest",
        "observed_revision",
        "timestamp",
    }
    if not isinstance(resolved, Mapping) or set(resolved) != required:
        return None
    trusted = {key: str(resolved.get(key) or "") for key in required}
    expected = {
        "evidence_pointer": normalized["evidence_pointer"],
        "issue_id": issue_id,
        "delegate_id": delegate_id,
        "criterion_hash": normalized["criterion_hash"],
        "evidence_digest": normalized["evidence_digest"],
        "observed_revision": normalized["observed_revision"],
        "timestamp": normalized["timestamp"],
    }
    if any(not hmac.compare_digest(trusted[key], value) for key, value in expected.items()):
        return None
    try:
        observed_at = datetime.fromisoformat(
            trusted["observed_revision"].replace("Z", "+00:00")
        )
        evidence_at = datetime.fromisoformat(trusted["timestamp"].replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return None
    if (
        observed_at.tzinfo is None
        or evidence_at.tzinfo is None
        or observed_at > evidence_at
        or evidence_at > current
    ):
        return None
    return normalized


def acceptance_gate(
    description: str,
    evidence_hashes: AbstractSet[str],
) -> AcceptanceGateResult:
    criteria = acceptance_criteria(description)
    if not criteria:
        return AcceptanceGateResult(True, "no_acceptance_criteria", criteria)
    if any(not criterion.checked for criterion in criteria):
        return AcceptanceGateResult(False, "acceptance_unchecked", criteria)
    if any(criterion.criterion_hash not in evidence_hashes for criterion in criteria):
        return AcceptanceGateResult(False, "acceptance_evidence_incomplete", criteria)
    return AcceptanceGateResult(True, "acceptance_complete", criteria)
