"""memory-event/1 contract, deterministic bank mapping, and policy gates.

Contract notes (H0):

- A memory event is a *working-memory* record.  It is never CCH evidence,
  never CCH cognition, and a recall hit is never a project fact.
- Events are strictly validated, canonicalized, hashed, and frozen on
  first write.  Payloads must never contain secrets, chain-of-thought /
  raw reasoning, or large binary payloads.
- No automatic secret detection is implemented: the contract simply
  forbids secret-bearing fields, and validation rejects them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

MEMORY_EVENT_SCHEMA = "memory-event/1"

ALLOWED_EVENT_TYPES = ("TASK_EPISODE", "HUMAN_CORRECTION", "DECISION_EVENT")

ALLOWED_ACTOR_KINDS = ("agent", "human", "system")

ALLOWED_ORIGIN_KINDS = ("agent_run", "conversation", "manual", "work_receipt")

EVENT_ID_PATTERN = re.compile(r"^EVT-[0-9]{8}-[A-Za-z0-9_-]{1,64}$")

# Low-information generic keys that are forbidden as content keys.  These
# are generic field names; the values they carry may still be retained.
FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "chain_of_thought",
        "chain-of-thought",
        "raw_reasoning",
        "raw-reasoning",
        "raw_thoughts",
        "thinking",
        "scratchpad",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
        "credentials",
    }
)

MAX_SUMMARY_CHARS = 4000
MAX_ITEM_CHARS = 2000
MAX_ITEMS = 50


class MemoryEventError(ValueError):
    """Raised when a payload violates the memory-event/1 contract."""


class EventIdConflictError(MemoryEventError):
    """Same event_id, different payload hash: never overwrite."""


class IntegrityError(MemoryEventError):
    """Archived/outbox bytes no longer match their recorded sha256."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MemoryEventError(message)


def _validate_iso8601_utc(value: str, label: str) -> None:
    _require(isinstance(value, str), f"{label} must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MemoryEventError(f"{label} must be ISO-8601: {value!r}") from None
    _require(
        parsed.tzinfo is not None,
        f"{label} must carry a timezone offset: {value!r}",
    )
    if value.endswith("Z"):
        # Normalize the accepted shorthand back to an explicit offset.
        return None


def _validate_scope(scope: Any) -> None:
    _require(isinstance(scope, dict), "scope must be an object")
    _require(set(scope.keys()) == {"domain", "project"}, "scope must have exactly keys: domain, project")
    for key in ("domain", "project"):
        value = scope[key]
        _require(isinstance(value, str) and value.strip(), f"scope.{key} must be a non-empty string")


def _validate_actor(actor: Any) -> None:
    _require(isinstance(actor, dict), "actor must be an object")
    _require(set(actor.keys()) == {"kind", "id"}, "actor must have exactly keys: kind, id")
    _require(
        actor["kind"] in ALLOWED_ACTOR_KINDS,
        f"actor.kind must be one of {ALLOWED_ACTOR_KINDS}",
    )
    _require(isinstance(actor["id"], str) and actor["id"].strip(), "actor.id must be a non-empty string")


def _validate_origin(origin: Any) -> None:
    _require(isinstance(origin, dict), "origin must be an object")
    _require(
        set(origin.keys()) == {"kind", "id", "sha256"},
        "origin must have exactly keys: kind, id, sha256",
    )
    _require(
        origin["kind"] in ALLOWED_ORIGIN_KINDS,
        f"origin.kind must be one of {ALLOWED_ORIGIN_KINDS}",
    )
    _require(isinstance(origin["id"], str) and origin["id"].strip(), "origin.id must be a non-empty string")
    sha = origin["sha256"]
    _require(sha is None or (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)), "origin.sha256 must be null or a 64-char lowercase hex string")


def _validate_content(content: Any) -> None:
    _require(isinstance(content, dict), "content must be an object")
    _require(
        set(content.keys()) <= {"summary", "decisions", "unresolved", "outcome"},
        "content allows only keys: summary, decisions, unresolved, outcome",
    )
    _require("summary" in content, "content.summary is required")
    summary = content["summary"]
    _require(isinstance(summary, str) and summary.strip(), "content.summary must be a non-empty string")
    _require(len(summary) <= MAX_SUMMARY_CHARS, f"content.summary exceeds {MAX_SUMMARY_CHARS} chars")
    for list_key in ("decisions", "unresolved"):
        if list_key in content:
            items = content[list_key]
            _require(isinstance(items, list), f"content.{list_key} must be an array")
            _require(len(items) <= MAX_ITEMS, f"content.{list_key} exceeds {MAX_ITEMS} items")
            for i, item in enumerate(items):
                _require(isinstance(item, str), f"content.{list_key}[{i}] must be a string")
                _require(item.strip(), f"content.{list_key}[{i}] must be non-empty")
                _require(len(item) <= MAX_ITEM_CHARS, f"content.{list_key}[{i}] exceeds {MAX_ITEM_CHARS} chars")
    if "outcome" in content:
        outcome = content["outcome"]
        _require(isinstance(outcome, str), "content.outcome must be a string")
        _require(len(outcome) <= MAX_SUMMARY_CHARS, f"content.outcome exceeds {MAX_SUMMARY_CHARS} chars")


def _validate_retention(retention: Any) -> None:
    _require(isinstance(retention, dict), "retention must be an object")
    _require(
        set(retention.keys()) == {"allow_memory", "sensitivity"},
        "retention must have exactly keys: allow_memory, sensitivity",
    )
    _require(retention["allow_memory"] is True, "retention.allow_memory must be true to enter the outbox")
    _require(
        retention["sensitivity"] == "normal",
        "retention.sensitivity must be 'normal'; H0 refuses non-normal sensitivity",
    )


def validate_memory_event(payload: Any) -> dict:
    """Strictly validate a memory-event/1 payload and return it unchanged."""

    _require(isinstance(payload, dict), "event payload must be a JSON object")
    _require(payload.get("schema") == MEMORY_EVENT_SCHEMA, f"schema must be {MEMORY_EVENT_SCHEMA!r}")
    _require(
        set(payload.keys())
        == {
            "schema",
            "event_id",
            "event_type",
            "scope",
            "occurred_at",
            "actor",
            "content",
            "origin",
            "retention",
        },
        "memory-event/1 allows exactly keys: schema, event_id, event_type, scope, occurred_at, actor, content, origin, retention",
    )
    event_id = payload["event_id"]
    _require(isinstance(event_id, str) and bool(EVENT_ID_PATTERN.fullmatch(event_id)), f"event_id must match EVT-YYYYMMDD-<slug>: {event_id!r}")
    _require(
        payload["event_type"] in ALLOWED_EVENT_TYPES,
        f"event_type must be one of {ALLOWED_EVENT_TYPES}",
    )
    _validate_iso8601_utc(payload["occurred_at"], "occurred_at")
    _validate_scope(payload["scope"])
    _validate_actor(payload["actor"])
    _validate_origin(payload["origin"])
    _validate_content(payload["content"])
    _validate_retention(payload["retention"])
    return payload


def canonical_event_bytes(payload: dict) -> bytes:
    """Canonical JSON bytes for hashing: sorted keys, tight separators, UTF-8."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def event_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_event_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class MemoryEvent:
    """A validated, frozen memory event with its canonical hash.

    RC1: the event owns an immutable canonical snapshot (UTF-8 bytes of
    canonical JSON).  Mutating the caller's input dict -- or the dict
    returned by ``event.payload`` -- can never change the event itself,
    and ``sha256`` always corresponds to the internal canonical payload.
    """

    _canonical_bytes: bytes
    _sha256: str

    @classmethod
    def from_payload(cls, payload: Any) -> "MemoryEvent":
        validate_memory_event(payload)
        canonical = canonical_event_bytes(payload)
        return cls(
            _canonical_bytes=canonical,
            _sha256=hashlib.sha256(canonical).hexdigest(),
        )

    @property
    def payload(self) -> dict:
        """A fresh decode of the frozen canonical snapshot (detached)."""
        return json.loads(self._canonical_bytes.decode("utf-8"))

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @property
    def event_id(self) -> str:
        return self.payload["event_id"]


# ---------------------------------------------------------------------------
# Bank mapping (deterministic, project-level only in H0).
# ---------------------------------------------------------------------------


def scope_key(domain: str, project: str) -> str:
    """Stable internal scope key: domain + NUL + project."""

    return f"{domain}\u0000{project}"


def bank_id_for(domain: str, project: str) -> str:
    """Deterministic bank id; never expose project names to the provider."""

    digest = hashlib.sha256(scope_key(domain, project).encode("utf-8")).hexdigest()
    return f"cch-{digest[:20]}"


# ---------------------------------------------------------------------------
# Deterministic rendering of an event into stable Markdown for retain.
# ---------------------------------------------------------------------------

_SECTION_TITLES = {
    "TASK_EPISODE": "TASK_EPISODE",
    "HUMAN_CORRECTION": "HUMAN_CORRECTION",
    "DECISION_EVENT": "DECISION_EVENT",
}


def render_event_markdown(event: MemoryEvent) -> str:
    """Render the event as deterministic Markdown (no arbitrary JSON dumps)."""

    p = event.payload
    lines = [f"# {_SECTION_TITLES[p['event_type']]}", ""]
    lines.append("Summary:")
    lines.append(p["content"]["summary"])
    lines.append("")
    for list_key, title in (("decisions", "Decisions:"), ("unresolved", "Unresolved:")):
        items = p["content"].get(list_key) or []
        if items:
            lines.append(title)
            for item in items:
                lines.append(f"- {item}")
            lines.append("")
    outcome = p["content"].get("outcome")
    if outcome:
        lines.append("Outcome:")
        lines.append(outcome)
        lines.append("")
    return "\n".join(lines)


def operation_id_for(event_id: str) -> str:
    """Stable UUID operation id: identical retries reuse the same id."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cch-memory:{event_id}"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
