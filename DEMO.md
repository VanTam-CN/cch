# Reproducible synthetic demos

The demos show engineering behavior on fictional material. They do not call an
LLM, prove current business knowledge, or replace human semantic review.

| Demo | What it checks | What it does not establish |
| --- | --- | --- |
| `tools/e2e_acceptance.py` | Source ingest, quote alignment, approval-bound publication, retained uncertainty, shared revision, export/restore | Independent user acceptance, external model correctness, production readiness |
| `tools/asset_demo.py` | Source/hash traceability, recorded-description import, explicit supersession filtering, historical retrieval | Image interpretation quality, automatic version inference, claim/visual semantic correctness |
| `examples/demo-project` | A valid 40-object fictional compiler fixture | A populated or approved real project store |

## Governance demo

```bash
.venv/bin/python tools/e2e_acceptance.py --out output/e2e-acceptance
```

Model outputs are recorded fixtures. The script approves proposals only in the
newly created synthetic store. It refuses an existing output directory and does
not modify other homes. Two separate context calls share a revision; these are
CLI calls, not a multi-model cognitive agreement experiment.

## Asset demo

```bash
.venv/bin/python tools/asset_demo.py --out output/asset-demo
```

The source diagrams are generated for this repository. The demo imports explicit
recorded descriptions and marks an older candidate superseded by a newer one.
It then checks normal and historical retrieval and preserves the result reports.
Supersession is a supplied decision, not a conclusion inferred from pixels.

## Handoff to a skill

Give the external agent the context, receipt, and requested asset records,
including source locator, hash, status, and known gaps. The agent can ask a model
to reason about the task or create a document through a skill. Preserve citations
and uncertainty in the result. A retrieved visual must still be checked against
the claim it is intended to support.
