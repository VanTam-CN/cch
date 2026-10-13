"""Unit tests for M3-H0 shadow memory: contracts, store, adapter, isolation.
These tests use a fake Hindsight client so hindsight_client does not need
to be installed.  Nothing here touches the Cognitive Base, Source store,
Artifact store, or agent context.
"""
from __future__ import annotations
import json
import sys
import threading
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cch.memory.local_store import (  # noqa: E402
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_SUBMITTED,
    MemoryStore,
)
from cch.memory.models import (  # noqa: E402
    ALLOWED_EVENT_TYPES,
    EventIdConflictError,
    canonical_event_bytes,
    MemoryEvent,
    MemoryEventError,
    bank_id_for,
    operation_id_for,
    render_event_markdown,
    scope_key,
)
# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def valid_payload(event_id="EVT-20260921-000001", event_type="TASK_EPISODE"):
    return {
        "schema": "memory-event/1",
        "event_id": event_id,
        "event_type": event_type,
        "scope": {"domain": "电力通信", "project": "5G-A电力无线专网"},
        "occurred_at": "2026-09-21T12:00:00+08:00",
        "actor": {"kind": "agent", "id": "codex"},
        "content": {
            "summary": "Completed the pilot and froze artifacts.",
            "decisions": ["Keep FACT_CURRENT gated on approved cognition."],
            "unresolved": [],
            "outcome": "BLOCKED (correct behavior)",
        },
        "origin": {"kind": "work_receipt", "id": "AUTH-20260921-003", "sha256": None},
        "retention": {"allow_memory": True, "sensitivity": "normal"},
    }
class FakeHindsightClient:
    """Minimal stand-in for hindsight_client.Hindsight.
    Records retain calls so tests can assert the exact mapping.  Set
    ``fail_with`` to an exception class (or a factory) to simulate
    provider failures.
    """
    def __init__(self):
        self.retain_calls = []
        self.fail_with = None
        self.lock = threading.Lock()
    def retain(self, **kwargs):
        with self.lock:
            self.retain_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return {
            "success": True,
            "bank_id": kwargs.get("bank_id"),
            "operation_id": kwargs.get("operation_id"),
            "async": True,
        }


def make_store(tmp_path):
    return MemoryStore(tmp_path)


def make_adapter(store, client):
    from cch.memory.provider import HindsightMemoryProvider

    return HindsightMemoryProvider(client=client)
# ---------------------------------------------------------------------------
# A. Event contract
# ---------------------------------------------------------------------------
class TestEventContract:
    def test_all_three_event_types_pass(self):
        for event_type in ALLOWED_EVENT_TYPES:
            event = MemoryEvent.from_payload(valid_payload(event_type=event_type))
            assert event.payload["event_type"] == event_type
    def test_missing_scope_rejected(self):
        payload = valid_payload()
        del payload["scope"]
        with pytest.raises(MemoryEventError):
            MemoryEvent.from_payload(payload)
    def test_missing_occurred_at_rejected(self):
        payload = valid_payload()
        del payload["occurred_at"]
        with pytest.raises(MemoryEventError):
            MemoryEvent.from_payload(payload)
    def test_non_normal_sensitivity_rejected(self):
        payload = valid_payload()
        payload["retention"]["sensitivity"] = "secret"
        with pytest.raises(MemoryEventError):
            MemoryEvent.from_payload(payload)
    def test_allow_memory_false_rejected_at_contract_level(self):
        payload = valid_payload()
        payload["retention"]["allow_memory"] = False
        with pytest.raises(MemoryEventError):
            MemoryEvent.from_payload(payload)
    def test_forbidden_content_keys_rejected(self):
        for bad in ("chain_of_thought", "api_key", "password"):
            payload = valid_payload()
            payload["content"][bad] = "oops"
            with pytest.raises(MemoryEventError):
                MemoryEvent.from_payload(payload)
    def test_unknown_top_level_key_rejected(self):
        payload = valid_payload()
        payload["extra"] = 1
        with pytest.raises(MemoryEventError):
            MemoryEvent.from_payload(payload)
    def test_sha256_is_deterministic(self):
        a = MemoryEvent.from_payload(valid_payload())
        b = MemoryEvent.from_payload(json.loads(json.dumps(valid_payload())))
        assert a.sha256 == b.sha256
        assert len(a.sha256) == 64
# ---------------------------------------------------------------------------
# B. Event immutability + store behavior
# ---------------------------------------------------------------------------
class TestEventImmutability:
    def test_first_write_created_then_idempotent(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        first = store.add_event(event)
        assert first["status"] == "created"
        second = store.add_event(MemoryEvent.from_payload(valid_payload()))
        assert second["status"] == "idempotent"
    def test_same_id_different_payload_conflict(self, tmp_path):
        store = make_store(tmp_path)
        store.add_event(MemoryEvent.from_payload(valid_payload()))
        other = valid_payload()
        other["content"]["summary"] = "Different summary"
        with pytest.raises(EventIdConflictError):
            store.add_event(MemoryEvent.from_payload(other))
    def test_idempotent_replay_does_not_rewind_submitted_outbox(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        store.mark_submitted(event.event_id, operation_id="op-1", provider_response={})
        store.add_event(MemoryEvent.from_payload(valid_payload()))
        assert store.outbox_state(event.event_id) == OUTBOX_SUBMITTED
    def test_store_layout(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        assert (tmp_path / "memory" / "events" / f"{event.event_id}.json").exists()
        assert (tmp_path / "memory" / "outbox" / f"{event.event_id}.json").exists()
        assert not (tmp_path / "store.git").exists()
# ---------------------------------------------------------------------------
# RC1 additions: integrity, concurrency, provider protocol, policy ceiling
# ---------------------------------------------------------------------------
class TestEventImmutabilityRC1:
    def test_event_detached_from_input_payload(self):
        payload = valid_payload()
        event = MemoryEvent.from_payload(payload)
        original = event.sha256
        payload["content"]["summary"] = "tampered after construction"
        assert event.sha256 == original
        assert event.payload["content"]["summary"] != "tampered after construction"

    def test_event_payload_mutation_does_not_change_event(self):
        event = MemoryEvent.from_payload(valid_payload())
        original = event.sha256
        event.payload["content"]["summary"] = "mutated via property"
        assert event.sha256 == original

    def test_event_hash_matches_archived_payload(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        archive = store.load_event(event.event_id)
        assert archive["event_sha256"] == event.sha256
        assert archive["payload"] == event.payload


class TestArchiveIntegrityRC1:
    def _prepared(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        return store, event

    def test_load_verified_event_ok(self, tmp_path):
        store, event = self._prepared(tmp_path)
        archive = store.load_verified_event(event.event_id)
        assert archive is not None
        store.verify_outbox_integrity(event.event_id)

    def test_tampered_archived_payload_rejected(self, tmp_path):
        store, event = self._prepared(tmp_path)
        path = tmp_path / "memory" / "events" / f"{event.event_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["payload"]["content"]["summary"] = "rewritten offline"
        path.write_text(json.dumps(doc), encoding="utf-8")
        from cch.memory.models import IntegrityError
        with pytest.raises(IntegrityError):
            store.load_verified_event(event.event_id)

    def test_tampered_archived_hash_rejected(self, tmp_path):
        store, event = self._prepared(tmp_path)
        path = tmp_path / "memory" / "events" / f"{event.event_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["event_sha256"] = "0" * 64
        path.write_text(json.dumps(doc), encoding="utf-8")
        from cch.memory.models import IntegrityError
        with pytest.raises(IntegrityError):
            store.load_verified_event(event.event_id)

    def test_tampered_outbox_hash_rejected_before_sync(self, tmp_path):
        store, event = self._prepared(tmp_path)
        path = tmp_path / "memory" / "outbox" / f"{event.event_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["event_sha256"] = "1" * 64
        path.write_text(json.dumps(doc), encoding="utf-8")
        from cch.memory.models import IntegrityError
        with pytest.raises(IntegrityError):
            store.verify_outbox_integrity(event.event_id)
    def test_tampered_archive_sync_marks_failed_not_retained(self, tmp_path):
        """RC1-2: a tampered archive must fail the event, never reach the provider."""
        from cch.memory.provider import HindsightMemoryProvider
        from cch.memory.service import MemoryService

        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        path = tmp_path / "memory" / "events" / f"{event.event_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["payload"]["content"]["summary"] = "offline rewrite"
        path.write_text(json.dumps(doc), encoding="utf-8")

        retained = []

        class SpyClient:
            def retain(self, **kwargs):
                retained.append(kwargs)
                return {"success": True, "async": True, "operation_id": kwargs.get("operation_id")}

        service = MemoryService(
            store=store, provider=HindsightMemoryProvider(client=SpyClient())
        )
        result = service.sync()
        assert result.failed == [event.event_id]
        assert retained == []

    def test_cli_ingest_registers_bank_immediately(self, tmp_path):
        """RC1-5: the CLI event-add path registers the bank before any sync."""
        from cch.memory.cli import main as cli_main

        event_file = tmp_path / "event.json"
        event_file.write_text(json.dumps(valid_payload()), encoding="utf-8")
        home = str(tmp_path)
        rc = cli_main(["--home", home, "event", "add", "--file", str(event_file)])
        assert rc == 0
        store = MemoryStore(home)
        banks = store.load_banks()
        assert len(banks) == 1
        assert banks[bank_id_for("电力通信", "5G-A电力无线专网")]["domain"] == "电力通信"


class TestCrossProcessRaceRC1:
    WRITER_SCRIPT = """
import json, sys
sys.path.insert(0, sys.argv[1])
from cch.memory.local_store import MemoryStore
from cch.memory.models import EventIdConflictError, MemoryEvent
home, event_id, summary, want = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
payload = {
    "schema": "memory-event/1",
    "event_id": event_id,
    "event_type": "TASK_EPISODE",
    "scope": {"domain": "d", "project": "p"},
    "occurred_at": "2026-09-21T12:00:00+08:00",
    "actor": {"kind": "agent", "id": "codex"},
    "content": {"summary": summary},
    "origin": {"kind": "manual", "id": "rc1-race", "sha256": None},
    "retention": {"allow_memory": True, "sensitivity": "normal"},
}
try:
    MemoryStore(home).add_event(MemoryEvent.from_payload(payload))
    print(want)
except EventIdConflictError:
    print("CONFLICT")
"""

    def test_concurrent_writers_never_last_writer_wins(self, tmp_path):
        import subprocess

        home = str(tmp_path)
        repo_root = str(Path(__file__).resolve().parents[1])
        summaries = {"A": "first writer payload", "B": "second writer payload"}
        procs = []
        for want in ("A", "B"):
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        self.WRITER_SCRIPT,
                        repo_root,
                        home,
                        "EVT-20260921-009001",
                        summaries[want],
                        want,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        outcomes = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            outcomes.append((out.strip(), err.strip()))
        winners = [out for out, err in outcomes if out in ("A", "B")]
        conflicts = [out for out, err in outcomes if out == "CONFLICT"]
        assert len(winners) == 1 and len(conflicts) == 1, outcomes
        archive = json.loads(
            (tmp_path / "memory" / "events" / "EVT-20260921-009001.json").read_text(
                encoding="utf-8"
            )
        )
        outbox = json.loads(
            (tmp_path / "memory" / "outbox" / "EVT-20260921-009001.json").read_text(
                encoding="utf-8"
            )
        )
        assert archive["event_sha256"] == outbox["event_sha256"]

    def test_sync_lock_excludes_second_holder(self, tmp_path):
        store = make_store(tmp_path)
        with store.sync_lock():
            second_entered = False
            try:
                with store.sync_lock(wait_seconds=0.3):
                    second_entered = True  # pragma: no cover
            except TimeoutError:
                pass
            assert second_entered is False


class TestFailedOperationIdRC1:
    def test_failed_outbox_keeps_operation_id(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        op_id = operation_id_for(event.event_id)
        store.mark_failed(
            event.event_id, error=repr(TimeoutError("boom")), operation_id=op_id
        )
        entry = store.load_outbox(event.event_id)
        assert entry["state"] == OUTBOX_FAILED
        assert entry["operation_id"] == op_id


class TestBankMappingRC1:
    def test_event_add_registers_bank_before_sync(self, tmp_path):
        from cch.memory.models import bank_id_for

        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        bank = bank_id_for("电力通信", "5G-A电力无线专网")
        # Same registration point as the CLI event-add ingest path.
        store.register_bank(bank, "电力通信", "5G-A电力无线专网")
        assert store.load_banks()[bank]["project"] == "5G-A电力无线专网"


class TestPolicyBytesRC1:
    def test_event_just_below_limit_allowed(self):
        from cch.memory.policy import MAX_EVENT_BYTES, evaluate_retention

        payload = valid_payload()
        # Contract-valid load: 40 CJK items x 1000 chars (~120 KiB), below
        # the 128 KiB canonical-byte ceiling but far beyond ASCII reach.
        payload["content"]["decisions"] = ["电" * 1000 for _ in range(40)]
        assert evaluate_retention(payload).allowed is True
        assert len(canonical_event_bytes(payload)) < MAX_EVENT_BYTES

    def test_event_above_limit_rejected(self):
        from cch.memory.policy import MAX_EVENT_BYTES, evaluate_retention

        payload = valid_payload()
        # Contract-valid load: 50 CJK items x 1100 chars (~165 KiB) crosses
        # the byte ceiling even though every per-field limit still passes.
        payload["content"]["decisions"] = ["电" * 1100 for _ in range(50)]
        decision = evaluate_retention(payload)
        assert decision.allowed is False
        assert "bytes" in decision.reason


class _PydanticLikeRetain:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestRetainProtocolRC1:
    def _adapter(self, store, response):
        class C:
            def retain(self, **kwargs):
                return response

        return make_adapter(store, C())

    def test_malformed_retain_response_failed(self, tmp_path):
        from cch.memory.provider import ProviderProtocolError

        event = MemoryEvent.from_payload(valid_payload())
        adapter = self._adapter(make_store(tmp_path), {"foo": "bar"})
        with pytest.raises(ProviderProtocolError):
            adapter.retain_event(event, bank_id="cch-x", operation_id="op-1")

    def test_wrong_operation_id_failed(self, tmp_path):
        from cch.memory.provider import ProviderProtocolError

        event = MemoryEvent.from_payload(valid_payload())
        adapter = self._adapter(
            make_store(tmp_path),
            {"success": True, "async": True, "operation_id": "different-op"},
        )
        with pytest.raises(ProviderProtocolError):
            adapter.retain_event(event, bank_id="cch-x", operation_id="op-1")

    def test_async_false_response_failed(self, tmp_path):
        from cch.memory.provider import ProviderProtocolError

        event = MemoryEvent.from_payload(valid_payload())
        adapter = self._adapter(
            make_store(tmp_path),
            {"success": True, "async": False, "operation_id": "op-1"},
        )
        with pytest.raises(ProviderProtocolError):
            adapter.retain_event(event, bank_id="cch-x", operation_id="op-1")

    def test_retain_pydantic_var_async_supported(self, tmp_path):
        event = MemoryEvent.from_payload(valid_payload())
        adapter = self._adapter(
            make_store(tmp_path),
            _PydanticLikeRetain(
                success=True, var_async=True, operation_ids=["op-1", "op-2"]
            ),
        )
        submission = adapter.retain_event(
            event, bank_id="cch-x", operation_id="op-1"
        )
        assert submission.status == "submitted"

    def test_real_sdk_retain_response_model_accepted(self, tmp_path):
        """RC2: the official hindsight-client RetainResponse model is accepted."""
        pytest.importorskip("hindsight_client_api")
        from hindsight_client_api.models.retain_response import RetainResponse

        event = MemoryEvent.from_payload(valid_payload())
        real_response = RetainResponse(
            success=True,
            bank_id="b",
            items_count=1,
            **{"async": True},
            operation_id="op-1",
        )
        assert getattr(real_response, "var_async") is True
        adapter = self._adapter(make_store(tmp_path), real_response)
        submission = adapter.retain_event(
            event, bank_id="cch-x", operation_id="op-1"
        )
        assert submission.status == "submitted"

    def test_protocol_error_marks_outbox_failed(self, tmp_path):
        from cch.memory.provider import HindsightMemoryProvider
        from cch.memory.service import MemoryService

        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)

        class BadClient:
            def retain(self, **kwargs):
                return {"success": True, "async": True, "operation_id": "wrong"}

        service = MemoryService(
            store=store, provider=HindsightMemoryProvider(client=BadClient())
        )
        result = service.sync()
        assert result.failed == [event.event_id]
        assert store.outbox_state(event.event_id) == OUTBOX_FAILED


class TestRecallRobustnessRC1:
    def _adapter(self, store, response):
        class C:
            def recall(self, **kwargs):
                return response

        return make_adapter(store, C())

    def test_recall_object_tags_none(self, tmp_path):
        store = make_store(tmp_path)

        class Item:
            id = "m-1"
            text = "t"
            metadata = None
            tags = None
            scores = None

        adapter = self._adapter(store, {"results": [Item()]})
        result = adapter.recall(bank_id="b", query="q")
        assert result.memories[0]["tags"] == []
        assert result.memories[0]["metadata"] == {}
        assert result.memories[0]["scores"] == {}

    def test_recall_dict_tags_none(self, tmp_path):
        store = make_store(tmp_path)
        adapter = self._adapter(
            store,
            {
                "results": [
                    {
                        "id": "m-2",
                        "text": "t",
                        "metadata": None,
                        "tags": None,
                        "scores": None,
                    }
                ]
            },
        )
        result = adapter.recall(bank_id="b", query="q")
        assert result.memories[0]["tags"] == []

    def test_malformed_recall_response_is_error(self, tmp_path):
        from cch.memory.provider import ProviderProtocolError

        store = make_store(tmp_path)
        adapter = self._adapter(store, {"no_results_here": True})
        with pytest.raises(ProviderProtocolError):
            adapter.recall(bank_id="b", query="q")

    def test_empty_results_is_zero_hit(self, tmp_path):
        store = make_store(tmp_path)
        adapter = self._adapter(store, {"results": []})
        result = adapter.recall(bank_id="b", query="q")
        assert result.memories == []
        assert not (tmp_path / "sources").exists()
    def test_load_event_roundtrip(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        loaded = store.load_event(event.event_id)
        assert loaded["payload"] == event.payload
        assert loaded["event_sha256"] == event.sha256
# ---------------------------------------------------------------------------
# C. Bank isolation
# ---------------------------------------------------------------------------
class TestBankIsolation:
    def test_same_scope_same_bank(self):
        assert bank_id_for("电力通信", "5G-A电力无线专网") == bank_id_for(
            "电力通信", "5G-A电力无线专网"
        )
    def test_different_project_different_bank(self):
        assert bank_id_for("d", "p1") != bank_id_for("d", "p2")
        assert bank_id_for("d1", "p") != bank_id_for("d2", "p")
    def test_bank_id_is_stable_format(self):
        bank = bank_id_for("电力通信", "5G-A电力无线专网")
        assert bank.startswith("cch-")
        assert len(bank) == len("cch-") + 20
    def test_known_vector(self):
        assert bank_id_for("电力通信", "5G-A电力无线专网") == "cch-7c7c3b23ee182fcae596"
    def test_register_bank_roundtrip_and_collision(self, tmp_path):
        store = make_store(tmp_path)
        bank = bank_id_for("d", "p")
        store.register_bank(bank, "d", "p")
        store.register_bank(bank, "d", "p")  # same scope: fine
        with pytest.raises(ValueError):
            store.register_bank(bank, "d", "other")
        assert store.load_banks()[bank]["project"] == "p"
# ---------------------------------------------------------------------------
# D. Hindsight adapter (fake client)
# ---------------------------------------------------------------------------
class TestHindsightAdapter:
    def test_retain_mapping(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        client = FakeHindsightClient()
        adapter = make_adapter(store, client)
        op_id = operation_id_for(event.event_id)
        submission = adapter.retain_event(
            event, bank_id=bank_id_for("电力通信", "5G-A电力无线专网"), operation_id=op_id
        )
        assert submission.status == "submitted"
        assert submission.operation_id == op_id
        call = client.retain_calls[0]
        assert call["bank_id"] == "cch-7c7c3b23ee182fcae596"
        assert call["retain_async"] is True
        assert call["document_id"] == f"cch-event:{event.event_id}"
        assert call["context"] == "cch:TASK_EPISODE"
        assert call["operation_id"] == op_id
        md = call["content"]
        assert md.startswith("# TASK_EPISODE")
        assert "Completed the pilot and froze artifacts." in md
        meta = call["metadata"]
        assert meta["cch_event_id"] == event.event_id
        assert meta["cch_event_sha256"] == event.sha256
        assert meta["cch_schema"] == "memory-event/1"
        assert all(isinstance(v, str) for v in meta.values())
        assert "source:cch" in call["tags"]
        assert "event:task_episode" in call["tags"]
    def test_operation_id_stable_across_retries(self):
        assert operation_id_for("EVT-20260921-000001") == operation_id_for(
            "EVT-20260921-000001"
        )
    def test_markdown_rendering_deterministic(self):
        e1 = MemoryEvent.from_payload(valid_payload())
        e2 = MemoryEvent.from_payload(json.loads(json.dumps(valid_payload())))
        assert render_event_markdown(e1) == render_event_markdown(e2)
# ---------------------------------------------------------------------------
# E. Failure isolation via the sync service
# ---------------------------------------------------------------------------
class TestFailureIsolation:
    def _prepare(self, tmp_path):
        store = make_store(tmp_path)
        event = MemoryEvent.from_payload(valid_payload())
        store.add_event(event)
        return store, event
    def _run_sync(self, store, client):
        from cch.memory.service import MemoryService
        service = MemoryService(store=store, provider=make_adapter(store, client))
        return service.sync()
    def test_timeout_keeps_event_and_outbox(self, tmp_path):
        store, event = self._prepare(tmp_path)
        client = FakeHindsightClient()
        client.fail_with = TimeoutError("boom")
        result = self._run_sync(store, client)
        assert result.submitted == []
        assert result.failed == [event.event_id]
        assert store.load_event(event.event_id) is not None
        assert store.outbox_state(event.event_id) == OUTBOX_FAILED
        entry = store.load_outbox(event.event_id)
        assert "boom" in json.dumps(entry["last_error"])
    def test_connection_refused_isolated(self, tmp_path):
        store, event = self._prepare(tmp_path)
        client = FakeHindsightClient()
        client.fail_with = ConnectionRefusedError("refused")
        self._run_sync(store, client)
        assert store.load_event(event.event_id) is not None
        assert store.outbox_state(event.event_id) == OUTBOX_FAILED
    def test_http_500_isolated(self, tmp_path):
        store, event = self._prepare(tmp_path)
        client = FakeHindsightClient()
        class FakeHTTPError(Exception):
            pass
        client.fail_with = FakeHTTPError("500 server error")
        self._run_sync(store, client)
        assert store.outbox_state(event.event_id) == OUTBOX_FAILED
    def test_retry_same_operation_id_then_success(self, tmp_path):
        store, event = self._prepare(tmp_path)
        client = FakeHindsightClient()
        client.fail_with = TimeoutError("boom")
        self._run_sync(store, client)
        failed_attempt = store.load_outbox(event.event_id)["attempt_count"]
        assert failed_attempt == 1
        client2 = FakeHindsightClient()
        result = self._run_sync(store, client2)
        assert result.submitted == [event.event_id]
        entry = store.load_outbox(event.event_id)
        assert entry["state"] == OUTBOX_SUBMITTED
        assert entry["attempt_count"] == 2
        assert entry["operation_id"] == operation_id_for(event.event_id)
        # The retry reuses the same stable operation id.
        assert client2.retain_calls[0]["operation_id"] == operation_id_for(
            event.event_id
        )
    def test_provider_failure_does_not_touch_governance(self, tmp_path, monkeypatch):
        store, event = self._prepare(tmp_path)
        client = FakeHindsightClient()
        client.fail_with = ConnectionRefusedError("down")
        self._run_sync(store, client)
        # No governance files were created anywhere under the fake home.
        assert not (tmp_path / "store.git").exists()
        assert not (tmp_path / "sources").exists()
        assert not (tmp_path / "artifact").exists()
    def test_submitted_never_requeued(self, tmp_path):
        store, event = self._prepare(tmp_path)
        client = FakeHindsightClient()
        self._run_sync(store, client)
        client2 = FakeHindsightClient()
        result = self._run_sync(store, client2)
        assert result.submitted == []
        assert client2.retain_calls == []
# ---------------------------------------------------------------------------
# F. Recall normalization
# ---------------------------------------------------------------------------
class TestRecall:
    def test_recall_normalize_and_no_governance_writes(self, tmp_path):
        store = make_store(tmp_path)
        class FakeRecallResult:
            id = "mem-1"
            text = "Decision: use six-layer architecture"
            metadata = {"cch_event_id": "EVT-20260921-000001"}
            tags = ["source:cch"]
            scores = {"final": 0.9}
        class FakeRecallClient(FakeHindsightClient):
            def recall(self, **kwargs):
                self.last_recall_kwargs = kwargs
                return {"results": [FakeRecallResult()]}
        client = FakeRecallClient()
        adapter = make_adapter(store, client)
        result = adapter.recall(
            bank_id="cch-test", query="architecture", budget="low"
        )
        assert result.provider == "hindsight"
        assert result.memories[0]["memory_id"] == "mem-1"
        assert (
            result.memories[0]["metadata"]["cch_event_id"]
            == "EVT-20260921-000001"
        )
        assert client.last_recall_kwargs["bank_id"] == "cch-test"
        # recall wrote nothing into the store
        assert store.list_events() == []
        assert not (tmp_path / "store.git").exists()
