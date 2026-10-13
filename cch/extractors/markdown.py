"""Markdown extractor: heading sections with heading paths, code fences preserved."""
from __future__ import annotations
import re
from pathlib import Path
from . import make_block, normalize, version_of

EXTRACTOR = ('cch-markdown', '1')

def extract_markdown(path, source_sha256):
    lines = normalize(Path(path).read_text(encoding='utf-8')).split('\n')
    blocks, headings, buffer, start = [], [], [], None
    in_code, code_start = False, None

    def flush(end):
        nonlocal buffer, start
        if buffer and any(l.strip() for l in buffer):
            block = make_block(source_sha256, *EXTRACTOR,
                               {'kind': 'markdown_section', 'headings': list(headings),
                                'line_start': start + 1, 'line_end': end},
                               '\n'.join(buffer))
            if block:
                blocks.append(block)
        buffer, start = [], None

    for i, line in enumerate(lines):
        fence = re.match(r'^(```|~~~)', line)
        if fence and not in_code:
            flush(i)
            in_code, code_start = True, i
            continue
        if fence and in_code:
            code = '\n'.join(lines[code_start + 1:i])
            block = make_block(source_sha256, *EXTRACTOR,
                               {'kind': 'code_fence', 'line_start': code_start + 1, 'line_end': i},
                               code, role='code',
                               quality_flags=['CODE_AS_DATA'])
            if code.strip() and block:
                blocks.append(block)
            in_code = False
            continue
        if in_code:
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)$', line)
        if heading:
            flush(i)
            level, text = len(heading.group(1)), heading.group(2).strip()
            headings = headings[:level - 1] + [text]
            continue
        if line.strip():
            if start is None:
                start = i
            buffer.append(line)
        else:
            flush(i)
    flush(len(lines))
    report = {'media_type': 'text/markdown', 'extractor': {'name': EXTRACTOR[0], 'version': EXTRACTOR[1]},
              'blocks': len(blocks), 'covered': ['headings', 'paragraphs', 'code-fences-as-data'],
              'not_covered': ['HTML blocks are kept verbatim without structure']}
    return blocks, report
