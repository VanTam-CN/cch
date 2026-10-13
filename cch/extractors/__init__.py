"""Format extractors producing located, frozen text blocks.

CRLF is normalized to LF here and never touched again, so evidence offsets
stay valid. Extractors report what they did and did not cover; silent partial
import is forbidden. Code blocks are data, never commands.
"""
from __future__ import annotations
import importlib.metadata
from pathlib import Path
from .core_shim import digest, canon

def normalize(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')

def version_of(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'

def make_block(source_sha256, extractor_name, extractor_version, locator, text, role='unknown',
               event_time=None, quality_flags=None, context=None):
    text = normalize(text)
    if not text.strip():
        return None
    block = {'block_id': 'BLK-' + digest((source_sha256 + canon(locator) + text).encode())[:12],
             'source_sha256': source_sha256, 'extractor': {'name': extractor_name, 'version': extractor_version},
             'locator': locator, 'text': text, 'text_sha256': digest(text.encode()),
             'role': role, 'event_time': event_time, 'quality_flags': quality_flags or []}
    if context:
        block['context'] = context
    return block

def extract(path, media_type, source_sha256):
    """Dispatch to the format extractor; returns (blocks, report)."""
    path = Path(path)
    if media_type == 'text/markdown':
        from .markdown import extract_markdown
        return extract_markdown(path, source_sha256)
    if media_type in {'text/plain', 'text/log'}:
        from .plain import extract_plain
        return extract_plain(path, source_sha256)
    if media_type == 'application/jsonl':
        from .plain import extract_jsonl
        return extract_jsonl(path, source_sha256)
    if media_type == 'application/docx':
        from .docx import extract_docx
        return extract_docx(path, source_sha256)
    if media_type == 'application/pdf':
        from .pdf import extract_pdf
        return extract_pdf(path, source_sha256)
    raise ValueError(f'no extractor for {media_type}')
