"""Generate only the repository's fictional knowledge and image fixtures.

No external files, model calls, business data, or image libraries are used.
Run from the checkout: .venv/bin/python tools/make_examples.py
"""
from pathlib import Path
import struct
import zlib

import yaml

ROOT = Path(__file__).resolve().parents[1]


def object_record(oid, content, **updates):
    record = {
        'id': oid, 'type': 'FACT',
        'scope': {'domain': 'demo', 'project': 'hub', 'topic': []},
        'status': 'ACTIVE', 'confidence': 'CONFIRMED',
        'authority': 'DOCUMENT_CONFIRMED', 'priority': 'REFERENCE',
        'content': content, 'rationale': 'Fictional example; no business authority.',
        'relations': {'supersedes': [], 'depends_on': [], 'conflicts_with': [], 'derived_from': []},
        'provenance': {'evidence_refs': []},
        'lifecycle': {'valid_from': '2026-09-01', 'valid_until': None, 'ttl_hours': None},
        'retrieval': {'keywords': [], 'aliases': []}, 'tags': ['synthetic'],
    }
    record.update(updates)
    return record


def knowledge_example():
    base = ROOT / 'examples' / 'demo-project'
    folder = base / 'cognitive-base'
    folder.mkdir(parents=True, exist_ok=True)
    evidence = base / 'evidence'
    evidence.mkdir(exist_ok=True)
    (evidence / 'demo-record.md').write_text(
        '# Fictional configuration record / 虚构配置记录\n\n'
        'All values describe a made-up project. They are not operational advice.\n\n'
        '当前连接池上限为 200。先试 100 的想法尚未确认。历史值 150 已被 200 替代。\n',
        encoding='utf-8',
    )
    (evidence / 'evidence-index.yaml').write_text(yaml.safe_dump({
        'EV-DEMO-001': {'path': 'evidence/demo-record.md',
                        'summary': 'Fictional current, tentative, and superseded pool values.'},
    }, allow_unicode=True, sort_keys=False), encoding='utf-8')
    provenance = {'evidence_refs': ['EV-DEMO-001']}
    retrieval = {'keywords': ['连接池', '上限', 'pool', '试运行'], 'aliases': []}
    records = [
        object_record('RULE-001', '虚构演示规则：未确认建议必须保留 TENTATIVE，发布须经人工审查。',
                      type='RULE', priority='MUST', authority='USER_CONFIRMED'),
        object_record('FACT-001', '虚构项目当前连接池上限为 200。',
                      provenance=provenance, retrieval=retrieval,
                      relations={'supersedes': ['FACT-003'], 'depends_on': [],
                                 'conflicts_with': [], 'derived_from': []}),
        object_record('FACT-002', '虚构项目连接池先试 100 的试运行建议，尚未确认。',
                      confidence='TENTATIVE', authority='AI_INFERRED',
                      provenance=provenance, retrieval=retrieval),
        object_record('FACT-003', '虚构项目历史连接池上限为 150，已被替代。',
                      status='SUPERSEDED', provenance=provenance, retrieval=retrieval),
    ]
    for number in range(4, 40):
        records.append(object_record(f'FACT-{number:03}',
            f'虚构填充记录 {number:03}：演示目录条目，不构成业务结论。'))
    for record in records:
        (folder / f"{record['id']}.yaml").write_text(
            yaml.safe_dump(record, allow_unicode=True, sort_keys=False), encoding='utf-8')
    print(f'Generated {len(records)} synthetic knowledge objects.')


FONT = {
    'A': ['01110','10001','10001','11111','10001','10001','10001'],
    'C': ['01111','10000','10000','10000','10000','10000','01111'],
    'D': ['11110','10001','10001','10001','10001','10001','11110'],
    'E': ['11111','10000','10000','11110','10000','10000','11111'],
    'G': ['01111','10000','10000','10111','10001','10001','01110'],
    'H': ['10001','10001','10001','11111','10001','10001','10001'],
    'M': ['10001','11011','10101','10101','10001','10001','10001'],
    'N': ['10001','11001','10101','10011','10001','10001','10001'],
    'O': ['01110','10001','10001','10001','10001','10001','01110'],
    'S': ['01111','10000','10000','01110','00001','00001','11110'],
    'T': ['11111','00100','00100','00100','00100','00100','00100'],
    'V': ['10001','10001','10001','10001','10001','01010','00100'],
    '1': ['00100','01100','00100','00100','00100','00100','01110'],
    '2': ['01110','10001','00001','00010','00100','01000','11111'],
}


def diagram(version):
    width, height = 420, 200
    pixels = bytearray(bytes((246, 248, 252)) * width * height)

    def rect(x, y, w, h, color):
        for row in range(y, y + h):
            start = (row * width + x) * 3
            pixels[start:start + w * 3] = bytes(color) * w

    def label(text, x, y, color=(255, 255, 255), scale=2):
        for letter in text:
            for row, bits in enumerate(FONT[letter]):
                for column, bit in enumerate(bits):
                    if bit == '1':
                        rect(x + column * scale, y + row * scale, scale, scale, color)
            x += 6 * scale

    label(f'V{version}', 14, 15, (35, 47, 65))
    for x, text, color in [(14, 'DOCS', (45, 98, 152)),
                            (155, 'CCH', (80, 64, 152)),
                            (296, 'AGENT', (35, 126, 116))]:
        rect(x, 66, 110, 44, color)
        label(text, x + 16, 81)
    for x in (124, 265):
        rect(x, 86, 31, 3, (65, 75, 90))
        rect(x + 24, 81, 3, 13, (65, 75, 90))
    if version == 2:
        rect(155, 144, 110, 38, (65, 75, 90))
        label('MEM', 180, 156)
        rect(208, 110, 3, 34, (65, 75, 90))

    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack(
            '!I', zlib.crc32(kind + data) & 0xffffffff)

    scanlines = b''.join(b'\0' + pixels[y * width * 3:(y + 1) * width * 3]
                         for y in range(height))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'tEXt', b'Description\0Fictional CCH diagram; generated locally.')
            + chunk(b'IDAT', zlib.compress(scanlines)) + chunk(b'IEND', b''))


if __name__ == '__main__':
    knowledge_example()
    folder = ROOT / 'examples' / 'assets'
    folder.mkdir(parents=True, exist_ok=True)
    for version in (1, 2):
        (folder / f'architecture-v{version}.png').write_bytes(diagram(version))
    print('Generated two synthetic asset diagrams.')
