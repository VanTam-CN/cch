"""PDF extractor: per-page text blocks; pages without extractable text are
flagged NEEDS_OCR and never silently counted as imported."""
from __future__ import annotations
import re
from pathlib import Path
from . import make_block, normalize, version_of

def extract_pdf(path, source_sha256):
    from pypdf import PdfReader
    version = version_of('pypdf')
    reader = PdfReader(str(path))
    blocks, ocr_pages, empty_pages = [], [], []
    for p, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ''
        except Exception as exc:  # corrupt page objects must surface, not vanish
            raw = ''
            empty_pages.append({'page': p, 'error': str(exc)[:120]})
        text = normalize(raw)
        if not text.strip():
            ocr_pages.append(p)
            continue
        parts, buf = [], []
        for line in text.split('\n'):
            if line.strip():
                buf.append(line)
            elif buf:
                parts.append('\n'.join(buf)); buf = []
        if buf:
            parts.append('\n'.join(buf))
        for b, part in enumerate(parts):
            block = make_block(source_sha256, 'cch-pdf', version,
                               {'kind': 'pdf_page_block', 'page': p, 'block': b}, part)
            if block:
                blocks.append(block)
    text_pages = len(reader.pages) - len(ocr_pages)
    report = {'media_type': 'application/pdf', 'extractor': {'name': 'cch-pdf', 'version': version},
              'blocks': len(blocks), 'pages': len(reader.pages), 'text_pages': text_pages,
              'needs_ocr_pages': ocr_pages, 'page_errors': empty_pages,
              'covered': ['extractable text per page with page/block locators'],
              'not_covered': ['scanned pages without a text layer', 'images', 'forms'],
              'intake_status': 'NEEDS_OCR' if text_pages == 0 else 'PARTIAL_OCR' if ocr_pages else 'OK'}
    return blocks, report
