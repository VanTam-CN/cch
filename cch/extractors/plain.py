"""Plain text / log extractor with line ranges; JSONL with record locators.

For logs and JSONL we attach a role (user/assistant/tool) only when the
record itself declares one; roles are never guessed from position.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from . import make_block, normalize, version_of

EXTRACTOR = ('cch-plain', '1')
ROLE_KEYS = ('role', 'speaker', 'author')
TIME_KEYS = ('time', 'timestamp', 'ts', 'datetime', 'event_time')
ROLE_MAP = {'user': 'user', 'human': 'user', 'assistant': 'assistant', 'model': 'assistant',
            'ai': 'assistant', 'tool': 'tool', 'system': 'system'}

def _event_time(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
    except ValueError:
        return None

def extract_plain(path, source_sha256, lines_per_block=40):
    lines = normalize(Path(path).read_text(encoding='utf-8')).split('\n')
    blocks = []
    for start in range(0, len(lines), lines_per_block):
        chunk = lines[start:start + lines_per_block]
        block = make_block(source_sha256, *EXTRACTOR,
                           {'kind': 'text_line_range', 'line_start': start + 1,
                            'line_end': start + len(chunk)},
                           '\n'.join(chunk))
        if block:
            blocks.append(block)
    report = {'media_type': 'text/plain+log', 'extractor': {'name': EXTRACTOR[0], 'version': EXTRACTOR[1]},
              'blocks': len(blocks), 'covered': ['utf-8 text grouped by fixed line ranges'],
              'not_covered': ['binary attachments', 'non-UTF-8 encodings']}
    return blocks, report

def extract_jsonl(path, source_sha256):
    lines = normalize(Path(path).read_text(encoding='utf-8')).split('\n')
    blocks, bad = [], 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            block = make_block(source_sha256, *EXTRACTOR, {'kind': 'text_line_range', 'line_start': i + 1, 'line_end': i + 1},
                               line, quality_flags=['NOT_JSON'])
            if block:
                blocks.append(block)
            continue
        role = 'unknown'
        for key in ROLE_KEYS:
            if isinstance(record, dict) and isinstance(record.get(key), str):
                role = ROLE_MAP.get(record[key].strip().lower(), 'unknown')
                break
        event = None
        for key in TIME_KEYS:
            if isinstance(record, dict) and key in record:
                event = _event_time(record[key])
                if event:
                    break
        text = json.dumps(record, ensure_ascii=False, sort_keys=True)
        block = make_block(source_sha256, *EXTRACTOR, {'kind': 'jsonl_record', 'record': i + 1, 'line_start': i + 1, 'line_end': i + 1},
                           text, role=role, event_time=event)
        if block:
            blocks.append(block)
    report = {'media_type': 'application/jsonl', 'extractor': {'name': EXTRACTOR[0], 'version': EXTRACTOR[1]},
              'blocks': len(blocks), 'covered': ['one block per JSONL record with declared roles/times'],
              'not_covered': ['non-JSON lines are kept verbatim and flagged'] if bad else [],
              'warnings': [f'{bad} non-JSON lines'] if bad else []}
    return blocks, report
