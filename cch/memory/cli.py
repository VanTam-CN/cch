"""Deterministic CLI entry for the cch-memory executable (M3-H0).

Command surface (deliberately minimal):

    cch-memory event add --file event.json
    cch-memory event inspect EVT-...
    cch-memory sync
    cch-memory status
    cch-memory recall --domain D --project P --query "..." [--budget low]
    cch-memory provider-health

Nothing here may touch the Cognitive Base, Source authority, Artifact
store, authoring pipeline, or agent context.  The API key is read from
the environment only and is never printed, logged, or persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from .local_store import MemoryStore
from .models import MemoryEvent, bank_id_for, operation_id_for
from .policy import evaluate_retention
from .provider import NullMemoryProvider

RECALL_SCHEMA = "memory-recall/1"


def _provider_from_env():
    """Build the provider purely from environment variables."""

    name = os.environ.get("CCH_MEMORY_PROVIDER", "none")
    if name == "none":
        return NullMemoryProvider()
    if name == "hindsight":
        from .provider import HindsightMemoryProvider

        return HindsightMemoryProvider.from_env()
    raise SystemExit(f"CCH_MEMORY_PROVIDER must be none|hindsight, got {name!r}")


def _emit(doc: dict) -> None:
    json.dump(doc, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_event_add(args) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    decision = evaluate_retention(payload)
    if not decision.allowed:
        _emit({"status": "rejected", "reason": decision.reason})
        return 2
    store = MemoryStore(args.home)
    event = MemoryEvent.from_payload(payload)
    result = store.add_event(event)
    # RC1: ingest path registers the scope-to-bank mapping so banks.json
    # no longer depends on a prior recall.
    store.register_bank(
        bank_id_for(event.payload["scope"]["domain"], event.payload["scope"]["project"]),
        event.payload["scope"]["domain"],
        event.payload["scope"]["project"],
    )
    result["bank_id"] = bank_id_for(
        event.payload["scope"]["domain"], event.payload["scope"]["project"]
    )
    result["operation_id"] = operation_id_for(event.event_id)
    _emit(result)
    return 0


def cmd_event_inspect(args) -> int:
    store = MemoryStore(args.home)
    archive = store.load_event(args.event_id)
    if archive is None:
        _emit({"status": "not_found", "event_id": args.event_id})
        return 1
    doc = dict(archive)
    doc["outbox"] = store.load_outbox(args.event_id)
    doc["submissions"] = json.loads(
        (store.submissions_dir / f"{args.event_id}.json").read_text(encoding="utf-8")
    ) if (store.submissions_dir / f"{args.event_id}.json").exists() else []
    _emit(doc)
    return 0


def cmd_sync(args) -> int:
    from .service import MemoryService

    store = MemoryStore(args.home)
    service = MemoryService(store=store, provider=_provider_from_env())
    result = service.sync()
    _emit({
        "status": "ok",
        "submitted": result.submitted,
        "failed": result.failed,
        "skipped": result.skipped,
        "errors": result.errors,
    })
    return 0 if not result.failed else 1


def cmd_status(args) -> int:
    store = MemoryStore(args.home)
    events = store.list_events()
    entries = []
    for path in sorted(store.outbox_dir.glob("EVT-*.json")):
        entries.append(json.loads(path.read_text(encoding="utf-8")))
    _emit({
        "home": str(Path(args.home)),
        "event_count": len(events),
        "outbox": [
            {
                "event_id": e["event_id"],
                "state": e["state"],
                "attempt_count": e.get("attempt_count", 0),
                "operation_id": e.get("operation_id"),
                "last_error": e.get("last_error"),
            }
            for e in entries
        ],
        "banks": store.load_banks(),
    })
    return 0


def cmd_recall(args) -> int:
    import datetime as _dt

    store = MemoryStore(args.home)
    provider = _provider_from_env()
    bank_id = bank_id_for(args.domain, args.project)
    store.register_bank(bank_id, args.domain, args.project)
    provider_name = os.environ.get("CCH_MEMORY_PROVIDER", "none")
    try:
        recall = provider.recall(bank_id=bank_id, query=args.query, budget=args.budget)
        memories = [
            {
                "memory_id": m["memory_id"],
                "text": m["text"],
                "metadata": m["metadata"],
                "tags": m["tags"],
                "scores": m["scores"],
            }
            for m in recall.memories
        ]
        error = None
    except Exception as exc:  # noqa: BLE001 - report, do not crash the shell
        memories = []
        error = repr(exc)
    doc = {
        "schema": RECALL_SCHEMA,
        "query": args.query,
        "scope": {"domain": args.domain, "project": args.project},
        "bank_id": bank_id,
        "provider": provider_name,
        "retrieved_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "budget": args.budget,
        "memories": memories,
        "warning": None if error is None else "provider error: results may be incomplete",
        "error": error,
    }
    run_id = store.save_recall_run(
        {"query": args.query, "scope": doc["scope"], "bank_id": bank_id, "budget": args.budget},
        doc,
    )
    doc["recall_run_id"] = run_id
    _emit(doc)
    # Shadow diagnostic only; a recall miss is never evidence of absence.
    return 0


def cmd_provider_health(args) -> int:
    provider = _provider_from_env()
    health = provider.health()
    _emit({
        "provider": health.provider,
        "healthy": health.healthy,
        "detail": health.detail,
    })
    return 0 if health.healthy else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cch-memory", description="CCH shadow memory (M3-H0)")
    parser.add_argument("--home", default="./cch-home", help="CCH home directory")
    sub = parser.add_subparsers(dest="command", required=True)

    event = sub.add_parser("event", help="memory event operations")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    add = event_sub.add_parser("add", help="add a memory event from a JSON file")
    add.add_argument("--file", required=True)
    add.set_defaults(func=cmd_event_add)
    inspect = event_sub.add_parser("inspect", help="show one archived event")
    inspect.add_argument("event_id")
    inspect.set_defaults(func=cmd_event_inspect)

    sync = sub.add_parser("sync", help="drain the outbox to the provider")
    sync.set_defaults(func=cmd_sync)

    status = sub.add_parser("status", help="show outbox and bank status")
    status.set_defaults(func=cmd_status)

    recall = sub.add_parser("recall", help="shadow recall (diagnostic only)")
    recall.add_argument("--domain", required=True)
    recall.add_argument("--project", required=True)
    recall.add_argument("--query", required=True)
    recall.add_argument("--budget", default="low", choices=["low", "mid", "high"])
    recall.set_defaults(func=cmd_recall)

    health = sub.add_parser("provider-health", help="check provider reachability")
    health.set_defaults(func=cmd_provider_health)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
