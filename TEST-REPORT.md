# Public preview verification

Date: 2026-10-13. Scope: the independent public CCH snapshot, version `0.5.0rc1`.

## Evidence boundary

- Core tests and demos use synthetic material and isolated local stores.
- Recorded exchange results do not constitute a fresh external model test.
- Optional memory tests use fake clients and an SDK response model.
- Real project trials, private deployment records, and old Git history are not included.
- Human semantic review, external adoption, live provider qualification, and production acceptance are not claimed.

## Release checks

| Check | Observed result |
| --- | --- |
| Existing suite, Python 3.11.11 on macOS | 261 tests and 7 subtests passed; 173.07 seconds; no skipped tests |
| Optional Hindsight SDK contract | Exercised with `hindsight-client 0.10.3`; fake clients and response models only |
| Synthetic publication demo | 19 steps passed; 3 approved demo revisions; 2 context calls shared a revision; export/restore recompiled successfully |
| Synthetic asset demo | 2 generated PNGs registered; frozen-hash import passed; normal retrieval excluded superseded V1; historical retrieval included both |
| Read-only compiler fixture | 40 objects validated; current `200` and tentative `100` selected; superseded `150` excluded |
| Publish-candidate secret scan | Gitleaks 8.30.1 returned no findings, including nested Office content; no suppression baseline |
| Publication hygiene | No identifying local/NAS paths or private-repository references found in selected text; synthetic Office author; local Markdown links resolved |

Runtime dependencies: PyYAML 6.0.3, tiktoken 0.12.0, python-docx 1.2.0,
pypdf 6.3.0; test runner pytest 9.1.1.

Generated run archives remain in ignored local output directories. They are not
part of the public source or proof of independent adoption. Workflow checks run
again on GitHub; inspect the [Actions history](https://github.com/VanTam-CN/cch/actions)
for their actual results. A green workflow does not qualify a live memory service.
