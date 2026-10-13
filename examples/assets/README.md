# Synthetic image candidates

Both PNGs are generated locally by `tools/make_examples.py` using the Python
standard library. They depict a fictional DOCS → CCH → AGENT flow; version 2
adds a MEM box. They contain no real project images or embedded identity data.

`tools/asset_demo.py` supplies recorded descriptions and a fictional human
supersession decision. It tests registration, hash-bound result import, version
filtering, and locators. It does not test fresh model image understanding or
claim that the MEM box represents qualified Hindsight integration.
