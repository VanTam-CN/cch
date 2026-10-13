# CCH — Cognitive Context Hub

**Governed project knowledge and reusable evidence for long-running AI agents.**

[简体中文](README.zh-CN.md) · [Quick start](QUICKSTART.md) · [Architecture](docs/ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md) · [MIT License](LICENSE)

CCH gives agents and skills a shared, versioned view of what a project knows,
where that knowledge came from, and which historical materials might be reusable.
Agents, skills, and models perform reasoning, writing, layout, and final
PPT/Word generation. CCH supplies knowledge, assets, evidence, and audit records.

**Status:** initial public preview, package version `0.5.0rc1`. All bundled
examples are synthetic. This repository has an independent Git history and
contains no private project trial or deployment archives.

## Three practical workflows

| Workflow | What CCH provides |
| --- | --- |
| A new coding session takes over a project | Scoped context, explicit uncertainty, an approved knowledge revision, and freshness checks |
| A skill needs diagrams from earlier material | Searchable candidate assets with source locators, byte hashes, and supersession information |
| An agent learns something worth retaining | A proposal that a human can inspect and approve before atomic publication |

## Why use it

- **One approved revision:** multiple agents can read the same committed knowledge snapshot.
- **Evidence stays attached:** document identity, locators, quoted spans, and hashes remain traceable.
- **Uncertainty survives:** approval does not silently upgrade tentative or model-inferred statements.
- **Updates are reviewable:** new knowledge follows candidate → proposal → human review → publication.
- **Old material has a lifecycle:** explicitly superseded visuals are excluded by default.
- **Handoffs expose gaps:** evidence packages and receipts record unresolved claims instead of filling them in.

Similarity search and an asset filename do not establish current fact authority.
Mechanical checks do not prove that an interpretation or visual is semantically correct.

## Install from source

Requirements: **macOS or Linux, Python 3.11+, Git, and uv**. The store uses POSIX
file locking. Native Windows operation has not been qualified.

```bash
git clone https://github.com/VanTam-CN/cch.git
cd cch
uv venv --python 3.11
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python tools/provision_tokenizer.py
```

Installation and the explicit tokenizer preparation use the network. The core
compiler and synthetic demos need no model API key and make no model requests.
Use the source installation above; publishing a distribution to PyPI is not part
of this preview.

## Run the synthetic demos

```bash
.venv/bin/python tools/e2e_acceptance.py
.venv/bin/python tools/asset_demo.py
```

The first demo imports Markdown/DOCX/PDF/log fixtures, uses recorded model
results, creates and approves proposals in its isolated demo store, proves that
two context calls use one revision, and exports/restores the store.
The second indexes synthetic diagrams, imports recorded descriptions, and
checks that an explicitly superseded diagram is excluded from normal search.

Outputs are written under ignored `output/` directories. An existing output
is never deleted or overwritten; use a different `--out` path to rerun.
Automated approvals are **only for these isolated fictional demos**. Approve
real project proposals yourself after reviewing their source and changes.

See [QUICKSTART.md](QUICKSTART.md) for expected outputs and [DEMO.md](DEMO.md)
for what the demos prove and what they do not.

## Local interface

```bash
.venv/bin/python webui/server.py --home ./demo-home --port 8765
```

Open `http://127.0.0.1:8765`. The interface supports source ingestion, candidates,
proposals, review, publication, and context queries. It is a local interface;
reviewer labels are not authentication. Keep it on loopback.

## Optional Hindsight adapter

```bash
uv pip install --python .venv/bin/python -e ".[memory-hindsight]"
```

The adapter supports explicit event submission, local archives, an outbox,
retries, and diagnostic recall. It is experimental and disabled by default.
Memory recall is not inserted into agent context and is not approved knowledge.
A real service needs its own connectivity, storage, recovery, and resource
qualification. See [Memory integration](docs/MEMORY.md).

## Scope and limitations

| Area | Current scope |
| --- | --- |
| Knowledge governance and compiler | Implemented; locally tested with synthetic data |
| Source ingestion | Markdown, UTF-8 text/logs, JSONL, DOCX text/tables, and text PDF |
| Asset discovery and retrieval | Standalone images, OOXML embedded images, and PDF candidates; descriptions come from an external model exchange |
| Evidence handoff | Frozen packages, maps, structured storyboards, verification reports, and receipts; semantic review remains human |
| Final document creation | External skills and models |
| Memory | Optional shadow adapter; unified context and memory-to-cognition promotion are not implemented |
| Permissions | Cooperative workflow; no OS-level separation between processes with the same user identity |

`--wps-render` is an explicit optional input-preview conversion path. It uses
an authenticated external WPS account and uploads documents; default discovery
does not invoke it. Scanned PDFs require OCR before source ingestion.

The release checks in [TEST-REPORT.md](TEST-REPORT.md) are tied to this public
snapshot. They are not production acceptance or proof of broad external adoption.

## Documentation

- [User manual (中文)](USER-MANUAL.md)
- [Agent integration](AGENT-INTEGRATION.md)
- [Source import guide](IMPORT-GUIDE.md)
- [Governance and permission boundaries](GOVERNANCE.md)
- [Architecture](docs/ARCHITECTURE.md) and [roadmap](docs/ROADMAP.md)
- [Contributing](CONTRIBUTING.md), [security](SECURITY.md), and [changelog](CHANGELOG.md)

Issues and focused pull requests are welcome. Please use synthetic reproductions
and do not include private documents, chats, credentials, or identifying logs.

## License

CCH source, documentation, and bundled synthetic examples are available under
[MIT](LICENSE). Dependencies retain their own licenses. You must have permission
to ingest or redistribute your own project material.
