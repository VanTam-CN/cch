"""Outbox sync service for M3-H0 shadow memory.

The service drains the local outbox through the configured provider.
Retries reuse the same stable operation_id, so a FAILED event can never
create a duplicate memory on the provider side.  SUBMITTED means the
provider accepted the async retain request; it is not "DONE" and not a
guarantee that memory units were extracted (that remains an observation
debt for H0).

Failure isolation contract: any provider error only transitions the
outbox entry to FAILED.  The archived event is never mutated, and no
governance surface (Cognitive Base, sources, artifacts, authoring) is
ever touched by this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .local_store import MemoryStore
from .models import (
    MemoryEvent,
    IntegrityError,
    bank_id_for,
    operation_id_for,
)
from .provider import MemoryProvider


@dataclass
class SyncResult:
    submitted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: dict[str, Any] = field(default_factory=dict)


class MemoryService:
    def __init__(self, *, store: MemoryStore, provider: MemoryProvider):
        self.store = store
        self.provider = provider

    def sync(self) -> SyncResult:
        result = SyncResult()
        # RC1: at most one sync per memory home (memory/locks/sync.lock).
        # The lock covers the whole drain, so two cron/manual syncs can
        # never consume the same outbox entry twice; it never contains a
        # second lock, so the bounded-wait deadlock surface stays empty.
        with self.store.sync_lock():
            for entry in self.store.pending_events():
                event_id = entry["event_id"]
                op_id = operation_id_for(event_id)
                # RC1: verify archived payload hash and archive/outbox
                # hash agreement before any provider call.
                try:
                    self.store.verify_outbox_integrity(event_id)
                except IntegrityError as exc:
                    self.store.mark_failed(
                        event_id, error=repr(exc), operation_id=op_id
                    )
                    result.failed.append(event_id)
                    result.errors[event_id] = repr(exc)
                    continue
                archive = self.store.load_verified_event(event_id)
                if archive is None:
                    result.skipped.append(event_id)
                    continue
                event = MemoryEvent.from_payload(archive["payload"])
                # Defense in depth: the contract already rejects non-normal
                # sensitivity / allow_memory=false at validation time.
                retention = event.payload["retention"]
                if not retention.get("allow_memory") or retention.get("sensitivity") != "normal":
                    result.skipped.append(event_id)
                    continue
                scope = event.payload["scope"]
                try:
                    # RC1: ingest/sync path registers the scope-to-bank
                    # mapping; recall must not be the only registration
                    # point for banks.json.
                    bank_id = bank_id_for(scope["domain"], scope["project"])
                    self.store.register_bank(bank_id, scope["domain"], scope["project"])
                    submission = self.provider.retain_event(
                        event, bank_id=bank_id, operation_id=op_id
                    )
                    self.store.mark_submitted(
                        event_id,
                        operation_id=submission.operation_id,
                        provider_response=submission.provider_response,
                    )
                    self.store.record_submission(
                        event_id,
                        attempt_no=(entry.get("attempt_count") or 0) + 1,
                        operation_id=submission.operation_id,
                        result="submitted",
                        detail=submission.provider_response,
                    )
                    result.submitted.append(event_id)
                except Exception as exc:  # noqa: BLE001 - isolate per event
                    self.store.mark_failed(
                        event_id, error=repr(exc), operation_id=op_id
                    )
                    self.store.record_submission(
                        event_id,
                        attempt_no=(entry.get("attempt_count") or 0) + 1,
                        operation_id=op_id,
                        result="failed",
                        detail=repr(exc),
                    )
                    result.failed.append(event_id)
                    result.errors[event_id] = repr(exc)
        return result
