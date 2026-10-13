# Quick start / 快速开始

This walkthrough uses fictional project data. No model request or private service is needed.
所有示例都是虚构数据，不需要模型请求或私有服务。

## 1. Install / 安装

```bash
git clone https://github.com/VanTam-CN/cch.git
cd cch
uv venv --python 3.11
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python tools/provision_tokenizer.py
```

Python 3.11+, Git, and macOS/Linux are required. The tokenizer command explicitly
prepares a digest-checked cache; it is never implicitly invoked by compilation.
If preparation fails, resolve network access and rerun it before compilation.

## 2. Compile a synthetic knowledge base / 编译虚构认知库

```bash
.venv/bin/python -m cch --base examples/demo-project context \
  --domain demo --project hub --as-of 2026-09-07 \
  --query '连接池上限是多少？哪些只是试运行建议？'
```

Expected: a governed context containing the fictional current value `200` and
an explicitly tentative trial suggestion `100`. The superseded value `150`
should not be selected as current knowledge.

`--base` reads a fixture directory. It does not bootstrap or approve a real store.

## 3. Exercise publication and source provenance / 验证发布与来源链

```bash
.venv/bin/python tools/e2e_acceptance.py
```

Expected: `SYNTHETIC E2E DEMO PASS`, an `output/e2e-acceptance/e2e-report.json`
report, and an isolated demo home. This covers import, source alignment,
proposal creation, approval-bound publication, uncertainty preservation,
two context calls using one revision, and export/restore.

Inspect `context.md`, `receipt.json`, proposal review bundles, and the source
locators under the generated demo home. Recorded model responses are fixtures;
a script-mediated demo approval is not a real user's acceptance of project truth.

To repeat without replacing previous data:

```bash
.venv/bin/python tools/e2e_acceptance.py --out output/e2e-run-2
```

## 4. Exercise historical visual reuse / 验证历史图表复用

```bash
.venv/bin/python tools/asset_demo.py
```

Expected: `SYNTHETIC ASSET DEMO PASS` and `output/asset-demo/asset-report.json`.
The active search retains the newer candidate and excludes a human-marked
superseded candidate. A historical search includes both.

Descriptions and supersession are declared synthetic input, not fresh image
understanding by a model. The returned candidate remains separate from approved
project fact authority.

## 5. Open the local interface / 打开本地界面

To inspect the seeded fictional store:

```bash
.venv/bin/python webui/server.py --home output/e2e-acceptance/cch-home --port 8765
```

Open `http://127.0.0.1:8765`. Use source, proposal, review, and context screens.
Keep the interface local; it has no shared-service authentication.
A fresh home, such as `./demo-home`, starts empty.

## 6. Verify changes / 验证修改

```bash
.venv/bin/python -m pytest -q
```

Install `.[test,memory-hindsight]` to also exercise the optional SDK response
contract. Without the extra, its test is explicitly skipped. No test invokes a
live Hindsight provider. See [TEST-REPORT.md](TEST-REPORT.md) for this release's
actual results and limitations.

## Common problems / 常见问题

| Message | Action |
| --- | --- |
| `TOKENIZER_UNAVAILABLE` | Run `tools/provision_tokenizer.py`; verify the tokenizer cache and configured encoding |
| Output already exists | Choose a fresh `--out` directory; the demo preserves existing data |
| `STALE_BASE` | Re-export the job or refresh the proposal against the latest knowledge revision |
| `NEEDS_OCR` | OCR the scanned document outside CCH, then register the resulting source |
| `BLOCKED` | Inspect unresolved evidence; supply evidence or narrow the task before continuing |

Use [USER-MANUAL.md](USER-MANUAL.md) for real project setup and approval steps.
