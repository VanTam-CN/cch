"""Mechanical retention policy for M3-H0 (no LLM decisions).

The policy is deliberately boring: allow exactly the three contracted
event types whose payloads passed strict schema validation, with
allow_memory=true and sensitivity=normal.  Everything else is refused.
This module exists so the "should this be remembered?" rule has one
auditable home instead of being implicit in validation code.

What can never be remembered via memory events (they belong to the CCH
Source/Artifact system instead): whole source documents, PDF/DOCX/PPTX
payloads, tool chatter, debug logs, full raw diffs, chain-of-thought.
The event contract blocks secret-bearing and reasoning fields; this
policy additionally refuses oversized or non-event payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    ALLOWED_EVENT_TYPES,
    MAX_ITEM_CHARS,
    MAX_ITEMS,
    MAX_SUMMARY_CHARS,
    MemoryEventError,
    canonical_event_bytes,
    validate_memory_event,
)


# Hard byte ceiling for a canonical event payload.  Generous for real
# episodes, but far below "whole document" scale.
MAX_EVENT_BYTES = 128 * 1024


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def evaluate_retention(payload: Any) -> PolicyDecision:
    """Decide, mechanically, whether a payload may enter the outbox."""

    try:
        validate_memory_event(payload)
    except MemoryEventError as exc:
        return PolicyDecision(allowed=False, reason=f"contract violation: {exc}")
    if len(canonical_event_bytes(payload)) > MAX_EVENT_BYTES:
        return PolicyDecision(
            allowed=False,
            reason=f"canonical event exceeds {MAX_EVENT_BYTES} bytes",
        )
    if payload["event_type"] not in ALLOWED_EVENT_TYPES:
        return PolicyDecision(allowed=False, reason="event_type not allowed in H0")
    retention = payload["retention"]
    if retention["allow_memory"] is not True:
        return PolicyDecision(allowed=False, reason="allow_memory is not true")
    if retention["sensitivity"] != "normal":
        return PolicyDecision(allowed=False, reason="sensitivity is not normal")
    content = payload["content"]
    if len(content.get("decisions", [])) > MAX_ITEMS or len(content.get("unresolved", [])) > MAX_ITEMS:
        return PolicyDecision(allowed=False, reason="content list exceeds item limit")
    if len(content["summary"]) > MAX_SUMMARY_CHARS:
        return PolicyDecision(allowed=False, reason="summary exceeds length limit")
    return PolicyDecision(allowed=True, reason="ok")
