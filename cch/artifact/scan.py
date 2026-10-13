"""Deterministic asset discovery in authorized directories.

Finds standalone images plus embedded images in PPTX/DOCX and page
rasterization candidates in PDFs. Read-only over the source tree; every
asset carries a sha256 content hash and an exact locator for traceability.
"""
from __future__ import annotations
import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
OOXML_EXTS = {'.pptx', '.docx'}


@dataclass(frozen=True)
class AssetCandidate:
    kind: str            # 'image' | 'pdf-page'
    source_path: str     # path as given by the user (locator root)
    container: str | None  # inner zip path for embedded media, None for standalone
    page: int | None     # 1-based page number for pdf-page candidates
    embedded_ref: str | None
    mime: str
    size_bytes: int
    sha256: str

    @property
    def locator(self) -> dict:
        return {'file': self.source_path,
                **({'page_or_slide': self.page} if self.page is not None else {}),
                **({'embedded_ref': self.embedded_ref} if self.embedded_ref else {})}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mime_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.webp': 'image/webp', '.gif': 'image/gif'}.get(suffix, 'application/octet-stream')


def _pptx_slide_map(zf: zipfile.ZipFile) -> dict[str, int]:
    """Map normalized media zip-path -> lowest slide number referencing it."""
    mapping: dict[str, int] = {}
    rels = sorted(n for n in zf.namelist()
                  if re.fullmatch(r'ppt/slides/_rels/slide\d+\.xml\.rels', n))
    for rel in rels:
        slide_no = int(re.search(r'slide(\d+)\.xml\.rels', rel).group(1))
        data = zf.read(rel).decode('utf-8', 'replace')
        for target in re.findall(r'Target="([^"]+)"', data):
            if '/media/' not in target and not target.startswith('media/'):
                continue
            resolved = posixpath.normpath(posixpath.join('ppt/slides', target))
            if resolved not in mapping or slide_no < mapping[resolved]:
                mapping[resolved] = slide_no
    return mapping


def _scan_ooxml(path: Path, given: str) -> list[AssetCandidate]:
    """Extract embedded media from pptx/docx zip containers.

    For PPTX, media are mapped back to their slide number so locators
    meet the proposal's traceability bar (file + slide + embedded ref).
    """
    out = []
    try:
        with zipfile.ZipFile(path) as zf:
            slide_map = _pptx_slide_map(zf) if path.suffix.lower() == '.pptx' else {}
            names = sorted(n for n in zf.namelist() if n.startswith(('ppt/media/', 'word/media/')))
            for name in names:
                if Path(name).suffix.lower() not in IMAGE_EXTS:
                    continue
                data = zf.read(name)
                slide = slide_map.get(name)
                out.append(AssetCandidate(
                    kind='image', source_path=given, container=name,
                    page=slide, embedded_ref=name, mime=_mime_for(name),
                    size_bytes=len(data), sha256=_sha256_bytes(data)))
    except zipfile.BadZipFile as exc:
        raise SystemExit(f'INVALID_CONTAINER: {given}: not a valid OOXML zip: {exc}')
    return out


def _scan_pdf(path: Path, given: str) -> list[AssetCandidate]:
    """Embedded images per page, else the page itself as a VLM candidate.

    Page candidates have no image bytes yet (rasterization is deferred);
    their hash covers the page content stream so it stays deterministic
    and distinct per page.
    """
    from pypdf import PdfReader
    out = []
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # pypdf raises assorted parse errors
        raise SystemExit(f'INVALID_CONTAINER: {given}: unreadable PDF: {exc}')
    for index, page in enumerate(reader.pages, start=1):
        try:
            images = list(page.images)
        except Exception:  # damaged xobjects must not kill the whole scan
            images = []
        if images:
            for im in images:
                data = im.data
                out.append(AssetCandidate(
                    kind='image', source_path=given, container=given,
                    page=index, embedded_ref=f'pdf:page{index}/{im.name}',
                    mime=_mime_for(im.name) if Path(im.name).suffix else 'image/jpeg',
                    size_bytes=len(data), sha256=_sha256_bytes(data)))
        else:
            try:
                contents = page.get_contents()
                page_bytes = contents.get_data() if contents is not None else b''
            except Exception:
                page_bytes = b''
            if not page_bytes:
                page_bytes = f'{path.name}#page={index}'.encode('utf-8')
            out.append(AssetCandidate(
                kind='pdf-page', source_path=given, container=None,
                page=index, embedded_ref=f'pdf:page{index}',
                mime='application/pdf', size_bytes=len(page_bytes),
                sha256=_sha256_bytes(page_bytes)))
    return out


def scan_directory(root: Path) -> list[AssetCandidate]:
    """Walk an authorized directory and return every asset candidate."""
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f'NOT_FOUND: scan root {root} is not a directory')
    out = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        given = str(path)
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTS:
            data = path.read_bytes()
            out.append(AssetCandidate(
                kind='image', source_path=given, container=None, page=None,
                embedded_ref=None, mime=_mime_for(path.name),
                size_bytes=len(data), sha256=_sha256_bytes(data)))
        elif suffix in OOXML_EXTS:
            out.extend(_scan_ooxml(path, given))
        elif suffix == '.pdf':
            out.extend(_scan_pdf(path, given))
    return out
