# Optional Hindsight shadow integration

CCH supplies approved knowledge; Hindsight supplies working memory. A memory hit
is not source evidence, approval, or a current project fact.

## Install and configure

```bash
uv pip install --python .venv/bin/python -e ".[memory-hindsight]"
export CCH_MEMORY_PROVIDER=hindsight
export CCH_HINDSIGHT_BASE_URL='http://127.0.0.1:8888'
```

Set `CCH_HINDSIGHT_API_KEY` in the local environment when your service requires
it. Do not put credentials in tracked files. The example URL is local-only and
is not a deployed service supplied by this repository.

```bash
.venv/bin/python -m cch.memory.cli --help
.venv/bin/cch-memory --home ./cch-home provider-health
```

The supported console entry is `cch-memory`. Use its `event add`, `sync`,
`status`, and `recall` subcommands. Events are explicitly supplied; CCH does not
automatically observe your chats or operating-system activity.

## Current contract

- Three event kinds: task episode, human correction, decision event.
- Frozen local event archive and a pending/failed/submitted outbox.
- Scope-specific banks and stable operation IDs for retries.
- Asynchronous accepted submission is separate from extracted and recalled memory.
- Diagnostic recall output retains the event metadata needed to trace origins.
- Provider failure must not alter the approved cognition store.

## Not yet delivered

Unified agent context, memory-to-cognition proposals, automatic promotion,
and reversible curation are not implemented. Recall stays outside runtime
context and has no governance authority.

Tests exercise fake clients and an optional SDK response model. They do not prove
that any live Hindsight deployment persists data, recovers after restart, or
fits a particular device's resources. Test those properties independently with
synthetic data before relying on a real service.
