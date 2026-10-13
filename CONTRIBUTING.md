# Contributing

Thanks for helping improve CCH. Practical bug reports, reproducible examples,
documentation fixes, and small improvements are welcome.

## Development setup

Use Python 3.11 or newer on macOS or Linux, Git, and `uv`:

```bash
uv venv --python 3.11
uv pip install --python .venv/bin/python -e ".[test,memory-hindsight]"
.venv/bin/python tools/provision_tokenizer.py
.venv/bin/python -m pytest -q
```

The memory tests use fake clients and an SDK response model. They do not call a
live Hindsight service or an LLM. The core installation remains independent of
the optional SDK.

## Pull requests

1. Describe the user problem and the intended behavior.
2. Keep changes focused and preserve existing user data.
3. Add or adjust tests when needed to cover a real behavioral risk.
4. Report exactly which checks ran and any remaining limitations.
5. Update the relevant public documentation if interfaces or scope change.

Use a descriptive branch name, such as `codex/improve-source-errors`.
Do not weaken approval, scope, evidence, freshness, or hash checks to make a test pass.

## Test data and privacy

Contribute only synthetic examples or material you are authorized to publish.
Do not include customer documents, project diagrams, runtime stores, raw chat
histories, deployment captures, API keys, or personal filesystem paths.
Remove identifying metadata from images and Office/PDF documents as well as text.

## Licensing

The repository is distributed under the MIT License. Contributions are made
under the same license. Do not submit third-party content without permission
and preserve required notices for any material you introduce.

## Expectations

This is an early public project. External adoption and production qualification
are not claimed. Issues and releases will document observed results and open work.
