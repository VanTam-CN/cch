"""Provider contract and Hindsight adapter for M3-H0 shadow memory.
Hindsight is a *working memory provider* only.  Its memories are never
CCH evidence, never CCH cognition, and a recall hit is never a project
fact.  The adapter only depends on the official hindsight client
(lazy-imported so the base install works without the memory extra).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from .models import MemoryEvent, render_event_markdown, operation_id_for
@dataclass(frozen=True)
class RetainSubmission:
    event_id: str
    operation_id: str
    status: str  # "submitted"
    provider_response: Any = None
@dataclass(frozen=True)
class MemoryRecall:
    bank_id: str
    query: str
    provider: str
    memories: list[dict] = field(default_factory=list)
@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    healthy: bool
    detail: Any = None
class ProviderProtocolError(Exception):
    """Provider response violated the retain/recall protocol contract."""
class MemoryProvider(Protocol):
    def retain_event(
        self, event: MemoryEvent, *, bank_id: str, operation_id: str
    ) -> RetainSubmission: ...
    def recall(self, *, bank_id: str, query: str, budget: str = "low") -> MemoryRecall: ...
    def health(self) -> ProviderHealth: ...
class NullMemoryProvider:
    """Default provider: remembers nothing, never touches the network."""
    def retain_event(
        self, event: MemoryEvent, *, bank_id: str, operation_id: str
    ) -> RetainSubmission:
        raise RuntimeError("CCH_MEMORY_PROVIDER=none: memory sync is disabled")
    def recall(self, *, bank_id: str, query: str, budget: str = "low") -> MemoryRecall:
        return MemoryRecall(bank_id=bank_id, query=query, provider="none", memories=[])
    def health(self) -> ProviderHealth:
        return ProviderHealth(provider="none", healthy=True, detail="disabled")
class HindsightMemoryProvider:
    """Official-client adapter; mapping is frozen in section 8 of M3-H0.
    Retries reuse the caller-supplied ``operation_id`` (stable uuid5 of
    the event id), so Hindsight-side idempotency holds across outbox
    retries.  No authority/status fields are placed in provider metadata:
    Hindsight memories must never look like governed CCH facts.
    """
    provider_name = "hindsight"
    def __init__(self, client: Any, timeout: float = 300.0):
        self._client = client
        self._timeout = timeout
    @classmethod
    def from_env(cls) -> "HindsightMemoryProvider":
        import os
        base_url = os.environ.get("CCH_HINDSIGHT_BASE_URL")
        if not base_url:
            raise RuntimeError("CCH_HINDSIGHT_BASE_URL is not configured")
        try:
            import hindsight_client  # lazy: optional dependency
        except ImportError as exc:  # pragma: no cover - environment guard
            raise RuntimeError(
                "hindsight-client is not installed; install the memory-hindsight extra"
            ) from exc
        timeout = float(os.environ.get("CCH_MEMORY_TIMEOUT_SECONDS", "300"))
        api_key = os.environ.get("CCH_HINDSIGHT_API_KEY")  # never logged/persisted
        client = hindsight_client.Hindsight(
            base_url, api_key=api_key, timeout=timeout
        )
        return cls(client=client, timeout=timeout)
    def retain_event(
        self, event: MemoryEvent, *, bank_id: str, operation_id: str
    ) -> RetainSubmission:
        from datetime import datetime
        payload = event.payload
        occurred = datetime.fromisoformat(payload["occurred_at"].replace("Z", "+00:00"))
        metadata = {
            "cch_schema": payload["schema"],
            "cch_event_id": event.event_id,
            "cch_domain": payload["scope"]["domain"],
            "cch_project": payload["scope"]["project"],
            "cch_actor": f"{payload['actor']['kind']}:{payload['actor']['id']}",
            "cch_origin_kind": payload["origin"]["kind"],
            "cch_origin_id": payload["origin"]["id"],
            "cch_event_sha256": event.sha256,
        }
        response = self._client.retain(
            bank_id=bank_id,
            content=render_event_markdown(event),
            timestamp=occurred,
            context=f"cch:{payload['event_type']}",
            document_id=f"cch-event:{event.event_id}",
            metadata=metadata,
            tags=["source:cch", f"event:{payload['event_type'].lower()}"],
            retain_async=True,
            operation_id=operation_id,
        )
        # RC1: protocol validation.  The provider must explicitly confirm
        # success, async acceptance, and the requested operation id.
        # Anything else must surface as a provider failure (FAILED), never
        # as a silent SUBMITTED.
        success = self._field(response, "success", None)
        if success is not True:
            raise ProviderProtocolError(
                f"retain response must confirm success=true, got {success!r}"
            )
        # Official hindsight-client v0.10 RetainResponse exposes the async
        # flag as the ``var_async`` attribute (JSON alias "async").  Keep
        # raw-dict alias support so fake/raw dict responses still validate.
        async_flag = self._field(response, "var_async", None)
        if async_flag is None:
            async_flag = self._field(response, "async", None)
        if async_flag is None:
            async_flag = self._field(response, "retain_async", None)
        if async_flag is None:
            async_flag = self._field(response, "async_", None)
        if async_flag is not True:
            raise ProviderProtocolError(
                f"retain response must confirm async=true, got {async_flag!r}"
            )
        returned_op = self._field(response, "operation_id", None)
        if returned_op is None:
            returned_ops = self._field(response, "operation_ids", None) or []
            if isinstance(returned_ops, dict):
                returned_ops = list(returned_ops.values())
            if operation_id not in returned_ops:
                raise ProviderProtocolError(
                    "retain response must echo the requested operation_id"
                )
        elif returned_op != operation_id:
            raise ProviderProtocolError(
                f"retain response operation_id mismatch: expected "
                f"{operation_id!r}, got {returned_op!r}"
            )
        return RetainSubmission(
            event_id=event.event_id,
            operation_id=operation_id,
            status="submitted",
            provider_response=self._jsonify(response),
        )
    def recall(self, *, bank_id: str, query: str, budget: str = "low") -> MemoryRecall:
        response = self._client.recall(bank_id=bank_id, query=query, budget=budget)
        # RC1: a response without a results field is a protocol error,
        # not a legitimate zero-hit recall.
        memories = []
        for item in self._iter_results(response):
            memories.append(
                {
                    "memory_id": str(self._field(item, "id", "")),
                    "text": str(self._field(item, "text", "")),
                    "metadata": self._jsonify(self._field(item, "metadata", None)) or {},
                    "tags": list(self._field(item, "tags", None) or []),
                    "scores": self._jsonify(self._field(item, "scores", None)) or {},
                }
            )
        return MemoryRecall(
            bank_id=bank_id, query=query, provider=self.provider_name, memories=memories
        )
    def health(self) -> ProviderHealth:
        try:
            version = self._client.get_version()
            return ProviderHealth(
                provider=self.provider_name, healthy=True, detail=self._jsonify(version)
            )
        except Exception as exc:  # noqa: BLE001 - health reports any failure
            return ProviderHealth(
                provider=self.provider_name, healthy=False, detail=str(exc)
            )
    # -- helpers --------------------------------------------------------
    @staticmethod
    def _iter_results(response: Any):
        results = getattr(response, "results", None)
        if results is None and isinstance(response, dict):
            results = response.get("results")
        if results is None:
            raise ProviderProtocolError(
                "recall response must contain a results field"
            )
        return results
    @staticmethod
    def _field(item: Any, name: str, default: Any) -> Any:
        """Read one field from a SDK object or dict; None -> default."""
        value = getattr(item, name, None)
        if value is None and isinstance(item, dict):
            value = item.get(name)
        return default if value is None else value
    @staticmethod
    def _jsonify(value: Any) -> Any:
        """Best-effort conversion of SDK objects into plain JSON data."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): HindsightMemoryProvider._jsonify(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [HindsightMemoryProvider._jsonify(v) for v in value]
        data = getattr(value, "model_dump", None)
        if callable(data):
            return HindsightMemoryProvider._jsonify(data())
        data = getattr(value, "__dict__", None)
        if isinstance(data, dict):
            return HindsightMemoryProvider._jsonify(data)
        return str(value)
