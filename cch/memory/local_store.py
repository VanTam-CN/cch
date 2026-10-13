"""Local, atomic, frozen-on-write storage for memory events.
Layout under ``<cch-home>/memory/``::
    events/        EVT-....json      (frozen canonical event payloads)
    outbox/        EVT-....json      (provider submission state machine)
    submissions/   EVT-....json      (audit trail of submission attempts)
    recall-runs/   MEMREC-NNNNNN/    (query.json + result.json)
    banks.json     registered bank mappings
This store never touches store.git, sources/, or the artifact store.  A
memory event is working memory only: never CCH evidence or cognition.
"""
from __future__ import annotations
import contextlib
import errno
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from .models import (
    EventIdConflictError,
    IntegrityError,
    MemoryEvent,
    event_sha256,
    utc_now_iso,
)
OUTBOX_PENDING = "PENDING"
OUTBOX_SUBMITTED = "SUBMITTED"
OUTBOX_FAILED = "FAILED"
def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically: temp file in the same dir, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
LOCK_WAIT_SECONDS = 30.0
@contextlib.contextmanager
def _file_lock(path: Path, *, wait_seconds: float = LOCK_WAIT_SECONDS):
    """Exclusive advisory lock via fcntl.flock.
    Same trust model as the publish lock in cch/store.py: best effort for
    cooperating local processes, bounded wait, and never held across
    network calls.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"memory lock busy: {path}") from None
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
class MemoryStore:
    def __init__(self, home: str | os.PathLike[str]):
        self.home = Path(home)
        self.memory_dir = self.home / "memory"
        self.events_dir = self.memory_dir / "events"
        self.outbox_dir = self.memory_dir / "outbox"
        self.submissions_dir = self.memory_dir / "submissions"
        self.recall_runs_dir = self.memory_dir / "recall-runs"
        self.banks_path = self.memory_dir / "banks.json"
        self.locks_dir = self.memory_dir / "locks"
    # -- cross-process serialization (RC1) ------------------------------
    def store_lock(self):
        """Serialize read-modify-write operations on local memory state."""
        return _file_lock(self.locks_dir / "store.lock")
    def sync_lock(self, *, wait_seconds: float = LOCK_WAIT_SECONDS):
        """Allow at most one concurrent sync per memory home."""
        return _file_lock(self.locks_dir / "sync.lock", wait_seconds=wait_seconds)
    # -- events ---------------------------------------------------------
    def add_event(self, event: MemoryEvent) -> dict:
        """Freeze an event; same id + same hash is idempotent.
        Same id + different hash raises EventIdConflictError.  The outbox
        record is only created when the event archive changes, so an
        idempotent replay can never rewind an already-SUBMITTED outbox
        entry back to PENDING.  First-write is serialized under
        memory/locks/store.lock (RC1), so concurrent writers can never
        last-writer-win the same event_id.
        """
        with self.store_lock():
            return self._add_event_locked(event)
    def _add_event_locked(self, event: MemoryEvent) -> dict:
        event_path = self.events_dir / f"{event.event_id}.json"
        if event_path.exists():
            existing = json.loads(event_path.read_text(encoding="utf-8"))
            if existing.get("event_sha256") == event.sha256:
                return {
                    "status": "idempotent",
                    "event_id": event.event_id,
                    "sha256": event.sha256,
                    "outbox_state": self.outbox_state(event.event_id),
                }
            raise EventIdConflictError(
                f"event_id {event.event_id} already exists with a different payload hash"
            )
        archive = {
            "schema": event.payload["schema"],
            "event_id": event.event_id,
            "event_sha256": event.sha256,
            "payload": event.payload,
            "archived_at": utc_now_iso(),
        }
        _atomic_write_json(event_path, archive)
        outbox = {
            "event_id": event.event_id,
            "event_sha256": event.sha256,
            "state": OUTBOX_PENDING,
            "attempt_count": 0,
            "operation_id": None,
            "last_attempt_at": None,
            "last_error": None,
            "provider_response": None,
            "updated_at": utc_now_iso(),
        }
        _atomic_write_json(self.outbox_dir / f"{event.event_id}.json", outbox)
        return {
            "status": "created",
            "event_id": event.event_id,
            "sha256": event.sha256,
            "outbox_state": OUTBOX_PENDING,
        }
    def load_event(self, event_id: str) -> Optional[dict]:
        path = self.events_dir / f"{event_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    def list_events(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.events_dir.glob("EVT-*.json"))]
    # -- integrity verification (RC1) -----------------------------------
    def load_verified_event(self, event_id: str) -> Optional[dict]:
        """Load an archived event and verify its payload hash.
        Raises IntegrityError when archived bytes no longer match the
        recorded event_sha256; the payload must never silently diverge.
        """
        archive = self.load_event(event_id)
        if archive is None:
            return None
        recomputed = event_sha256(archive.get("payload") or {})
        if recomputed != archive.get("event_sha256"):
            raise IntegrityError(
                f"INTEGRITY_ERROR: archived payload hash mismatch for {event_id}"
            )
        return archive
    def verify_outbox_integrity(self, event_id: str) -> None:
        """Reconcile archive and outbox hashes before any retain (RC1)."""
        archive = self.load_verified_event(event_id)
        outbox = self.load_outbox(event_id)
        if archive is None or outbox is None:
            raise IntegrityError(
                f"INTEGRITY_ERROR: missing archive/outbox entry for {event_id}"
            )
        if archive.get("event_sha256") != outbox.get("event_sha256"):
            raise IntegrityError(
                f"INTEGRITY_ERROR: archive/outbox hash mismatch for {event_id}"
            )
    # -- outbox ---------------------------------------------------------
    def _outbox_path(self, event_id: str) -> Path:
        return self.outbox_dir / f"{event_id}.json"
    def outbox_state(self, event_id: str) -> Optional[str]:
        path = self._outbox_path(event_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["state"]
    def load_outbox(self, event_id: str) -> Optional[dict]:
        path = self._outbox_path(event_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    def pending_events(self) -> list[dict]:
        """Outbox entries eligible for submission: PENDING or FAILED."""
        entries = []
        for path in sorted(self.outbox_dir.glob("EVT-*.json")):
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry["state"] in (OUTBOX_PENDING, OUTBOX_FAILED):
                entries.append(entry)
        return entries
    def mark_submitted(
        self,
        event_id: str,
        *,
        operation_id: str,
        provider_response: Any,
    ) -> dict:
        with self.store_lock():
            return self._mark_submitted_locked(
                event_id,
                operation_id=operation_id,
                provider_response=provider_response,
            )
    def _mark_submitted_locked(
        self,
        event_id: str,
        *,
        operation_id: str,
        provider_response: Any,
    ) -> dict:
        entry = self.load_outbox(event_id)
        if entry is None:
            raise FileNotFoundError(f"no outbox entry for {event_id}")
        entry["state"] = OUTBOX_SUBMITTED
        entry["attempt_count"] = int(entry.get("attempt_count", 0)) + 1
        entry["last_attempt_at"] = utc_now_iso()
        entry["operation_id"] = operation_id
        entry["provider_response"] = provider_response
        entry["last_error"] = None
        entry["updated_at"] = utc_now_iso()
        _atomic_write_json(self._outbox_path(event_id), entry)
        return entry
    def mark_failed(
        self, event_id: str, *, error: Any, operation_id: Optional[str] = None
    ) -> dict:
        with self.store_lock():
            return self._mark_failed_locked(
                event_id, error=error, operation_id=operation_id
            )
    def _mark_failed_locked(
        self, event_id: str, *, error: Any, operation_id: Optional[str] = None
    ) -> dict:
        entry = self.load_outbox(event_id)
        if entry is None:
            raise FileNotFoundError(f"no outbox entry for {event_id}")
        entry["state"] = OUTBOX_FAILED
        entry["attempt_count"] = int(entry.get("attempt_count", 0)) + 1
        entry["last_attempt_at"] = utc_now_iso()
        entry["last_error"] = error
        if operation_id is not None:
            entry["operation_id"] = operation_id
        entry["updated_at"] = utc_now_iso()
        _atomic_write_json(self._outbox_path(event_id), entry)
        return entry
    def record_submission(
        self,
        event_id: str,
        *,
        attempt_no: int,
        operation_id: str,
        result: str,
        detail: Any = None,
    ) -> None:
        with self.store_lock():
            return self._record_submission_locked(
                event_id,
                attempt_no=attempt_no,
                operation_id=operation_id,
                result=result,
                detail=detail,
            )
    def _record_submission_locked(
        self,
        event_id: str,
        *,
        attempt_no: int,
        operation_id: str,
        result: str,
        detail: Any = None,
    ) -> None:
        audit = {
            "event_id": event_id,
            "attempt_no": attempt_no,
            "attempted_at": utc_now_iso(),
            "operation_id": operation_id,
            "result": result,
            "detail": detail,
        }
        path = self.submissions_dir / f"{event_id}.json"
        history: list = []
        if path.exists():
            history = json.loads(path.read_text(encoding="utf-8"))
        history.append(audit)
        _atomic_write_json(path, history)
    # -- banks ----------------------------------------------------------
    def register_bank(self, bank_id: str, domain: str, project: str) -> dict:
        with self.store_lock():
            return self._register_bank_locked(bank_id, domain, project)
    def _register_bank_locked(self, bank_id: str, domain: str, project: str) -> dict:
        banks = self.load_banks()
        existing = banks.get(bank_id)
        if existing is not None and (
            existing.get("domain") != domain or existing.get("project") != project
        ):
            raise ValueError(
                f"bank_id collision: {bank_id} is registered for a different scope"
            )
        banks[bank_id] = {"bank_id": bank_id, "domain": domain, "project": project}
        _atomic_write_json(self.banks_path, banks)
        return banks[bank_id]
    def load_banks(self) -> dict:
        if not self.banks_path.exists():
            return {}
        return json.loads(self.banks_path.read_text(encoding="utf-8"))
    # -- recall runs ------------------------------------------------------
    def new_recall_run_id(self) -> str:
        self.recall_runs_dir.mkdir(parents=True, exist_ok=True)
        max_no = 0
        for path in self.recall_runs_dir.iterdir():
            if path.is_dir() and path.name.startswith("MEMREC-"):
                try:
                    max_no = max(max_no, int(path.name.split("-", 1)[1]))
                except ValueError:
                    continue
        return f"MEMREC-{max_no + 1:06d}"
    def save_recall_run(self, query_doc: dict, result_doc: dict) -> str:
        with self.store_lock():
            run_id = self.new_recall_run_id()
            run_dir = self.recall_runs_dir / run_id
            _atomic_write_json(run_dir / "query.json", query_doc)
            _atomic_write_json(run_dir / "result.json", result_doc)
            return run_id
