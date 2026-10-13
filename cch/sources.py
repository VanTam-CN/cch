"""Content-addressed raw source registration.

Original files are copied into sources/sha256/<sha> and never overwritten.
Import time is not fact time: declared dates stay null unless given. The same
bytes imported again reuse the blob but keep separate scope bindings; the same
name with different bytes is a new source, never a replacement.
"""
from __future__ import annotations
import json
import re
import shutil
import uuid
from pathlib import Path
from .core import CCHError, digest
from .store import Home, now_iso

MEDIA_BY_SUFFIX = {'.md': 'text/markdown', '.markdown': 'text/markdown', '.txt': 'text/plain',
                   '.log': 'text/log', '.jsonl': 'application/jsonl',
                   '.docx': 'application/docx', '.pdf': 'application/pdf'}

def media_type(name):
    suffix = Path(name).suffix.lower()
    if suffix not in MEDIA_BY_SUFFIX:
        raise CCHError('VALIDATION_ERROR', f'unsupported source format: {suffix or "(none)"}; '
                        'XLSX/PPTX/OCR images are not supported in v0.2')
    return MEDIA_BY_SUFFIX[suffix]

def _index_path(home):
    return home.sources/'index.json'

def load_index(home):
    path = _index_path(home)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))

def _save_index(home, index):
    path = _index_path(home)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)

def register(home, file_path, domain, project, topics=(), declared_date=None, source_id=None):
    path = Path(file_path)
    if not path.is_file() or path.is_symlink():
        raise CCHError('VALIDATION_ERROR', f'source must be a regular file: {path}')
    if not domain or not project or '*' in (domain, project):
        raise CCHError('SCOPE_VIOLATION', 'explicit domain and project required for import scope')
    data = path.read_bytes()
    sha = digest(data)
    blob = home.sources/'sha256'/sha
    if not blob.is_file():
        tmp = home.sources/'sha256'/f'.tmp-{uuid.uuid4().hex[:8]}'
        shutil.copy2(path, tmp)
        tmp.replace(blob)  # same-bytes races converge; blob bytes identical by hash
    index = load_index(home)
    existing_same_bytes = [sid for sid, meta in index.items() if meta['sha256'] == sha and meta['name'] == path.name]
    binding = {'domain': domain, 'project': project, 'topics': sorted(topics), 'bound_at': now_iso()}
    if existing_same_bytes:
        sid = existing_same_bytes[0]
        index[sid]['bindings'].append(binding)
        _save_index(home, index)
        return {'source_id': sid, 'sha256': sha, 'blob_reused': True, 'new_binding': True}
    if source_id is None:
        source_id = 'SRC-' + uuid.uuid4().hex[:12]
    if not re.fullmatch(r'SRC-[A-Za-z0-9][A-Za-z0-9_-]*', source_id):
        raise CCHError('VALIDATION_ERROR', 'source_id must match SRC-<id>')
    if source_id in index:
        raise CCHError('VALIDATION_ERROR', f'{source_id}: id already used for different bytes')
    index[source_id] = {'sha256': sha, 'name': path.name, 'media_type': media_type(path.name),
                        'size': len(data), 'imported_at': now_iso(),
                        'source_declared_date': declared_date, 'bindings': [binding]}
    _save_index(home, index)
    return {'source_id': source_id, 'sha256': sha, 'blob_reused': False, 'new_binding': False}

def inspect(home, source_id):
    index = load_index(home)
    if source_id not in index:
        raise CCHError('NOT_FOUND', f'source {source_id} not registered')
    meta = dict(index[source_id])
    meta['blob_verified'] = digest((home.sources/'sha256'/meta['sha256']).read_bytes()) == meta['sha256']
    return meta

def get(home, source_id):
    meta = inspect(home, source_id)
    return home.sources/'sha256'/meta['sha256'], meta
