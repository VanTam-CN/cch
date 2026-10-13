"""Self-contained asset pack: cch-asset pack <job_id> --out <dir|.zip>.

Materializes a frozen exchange job into one folder or zip a reviewer (or
offline VLM agent) can consume without access to the original library:

  job.json           the frozen manifest, byte-for-byte
  assets/<id>.<ext>  the real asset bytes
  manifest.sha256    sha256 of every packed file (including job.json)
  README.txt         verification and result-return instructions

Byte provenance per candidate kind:
- image: extracted from the OOXML container or read from the standalone
  file; verified against the frozen sha256.
- pdf-page (native PDF): rasterized on demand with pdftoppm; the frozen
  sha covers the PDF content stream (identity), the PNG is derived bytes.
- pdf-page with embedded_ref render:page<N>: read from
  <home>/artifact/render-cache/<doc sha>/page-<N>.png (written at scan
  time by --wps-render) and verified against the frozen sha256. A missing
  cache fails the pack loudly instead of silently re-rendering.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ..store import now_iso


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ext_for(mime: str, ref: str | None) -> str:
    by_mime = {'image/png': '.png', 'image/jpeg': '.jpg',
               'image/webp': '.webp', 'image/gif': '.gif'}
    if mime in by_mime:
        return by_mime[mime]
    if ref:
        suffix = Path(ref.split('/')[-1]).suffix.lower()
        if suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            return '.jpg' if suffix == '.jpeg' else suffix
    return '.bin'


def _container_bytes(source: Path, ref: str) -> bytes:
    with zipfile.ZipFile(source) as zf:
        return zf.read(ref)


def _pdf_embedded_bytes(source: Path, page: int, name: str) -> bytes:
    from pypdf import PdfReader
    reader = PdfReader(str(source))
    for image in reader.pages[page - 1].images:
        if image.name == name:
            return image.data
    raise KeyError(name)


def _pdf_page_png(source: Path, page: int, work_dir: Path) -> bytes:
    work_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(['pdftoppm', '-png', '-r', '150', '-f', str(page),
                    '-l', str(page), str(source), str(work_dir / 'pg')],
                   check=True, capture_output=True, timeout=120)
    hits = sorted(work_dir.glob(f'pg-{page}.png')) or sorted(work_dir.glob('pg-*.png'))
    if not hits:
        raise RuntimeError(f'pdftoppm produced no output for page {page}')
    return hits[0].read_bytes()


def _wps_cached_bytes(root: Path, source: Path, page: int,
                      frozen_sha: str) -> bytes:
    doc_sha = _sha256(source.read_bytes())
    cache = Path(root) / 'artifact' / 'render-cache' / doc_sha / f'page-{page}.png'
    if not cache.is_file():
        raise SystemExit(
            f"WPS_CACHE_MISS: {cache} not found; rescan with --wps-render first")
    data = cache.read_bytes()
    if _sha256(data) != frozen_sha:
        raise SystemExit(
            f"SHA_MISMATCH: cached render {cache} does not match frozen sha256")
    return data


def pack_job(root: Path, job_id: str, out: Path) -> dict:
    from .exchange import _job_dir
    directory = _job_dir(root, job_id)
    job = json.loads((directory / 'job.json').read_text('utf-8'))
    entries = job['assets']

    out = Path(out)
    work_root = Path(tempfile.mkdtemp(prefix='cch-pack-'))
    stage = work_root / 'stage'
    (stage / 'assets').mkdir(parents=True)
    raster_dir = work_root / 'raster'
    try:
        (stage / 'job.json').write_text(
            json.dumps(job, ensure_ascii=False, indent=2) + "\n", "utf-8")

        for entry in entries:
            aid = entry['asset_id']
            locator = entry['locator']
            source = Path(locator['file'])
            ref = locator.get('embedded_ref')
            page = locator.get('page_or_slide')
            kind = entry['kind']
            data: bytes | None = None
            ext = _ext_for(entry['mime'], ref)
            if kind == 'image':
                if ref and ref.startswith('pdf:'):
                    head, _, name = ref.rsplit("/", 1)
                    page_no = int(head.split('page', 1)[1])
                    data = _pdf_embedded_bytes(source, page_no, name)
                elif ref and source.suffix.lower() in {'.pptx', '.ppt', '.docx', '.doc'}:
                    data = _container_bytes(source, ref)
                else:
                    data = source.read_bytes()
                if _sha256(data) != entry['sha256']:
                    raise SystemExit(
                        f"SHA_MISMATCH: {aid} source bytes do not match the frozen manifest")
            elif kind == 'pdf-page':
                if ref and ref.startswith('render:'):
                    data = _wps_cached_bytes(root, source, int(page), entry['sha256'])
                elif page and source.is_file():
                    data = _pdf_page_png(source, int(page), raster_dir)
                    ext = '.png'
            if data is None:
                raise SystemExit(f'NOT_TRACEABLE: {aid}: cannot materialize bytes from {locator}')
            (stage / 'assets' / f'{aid}{ext}').write_bytes(data)

        files = sorted(p for p in stage.rglob('*')
                       if p.is_file() and p.name != 'manifest.sha256')
        lines = [f"{_sha256(p.read_bytes())}  {p.relative_to(stage).as_posix()}"
                 for p in files]
        (stage / "manifest.sha256").write_text("\n".join(lines) + "\n", "utf-8")
        (stage / "README.txt").write_text(
            "CCH self-contained asset pack\n"
            f"job: {job_id}\n"
            f"assets: {len(entries)}\n\n"
            "Verify packed files:  sha256sum -c manifest.sha256\n"
            "job.json is the frozen manifest; asset identity (sha256 in\n"
            "job.json) binds to the original scan, while manifest.sha256\n"
            "covers this pack. For pdf-page assets the PNG is a 150dpi\n"
            "render; identity remains the manifest hash.\n\n"
            "Return semantics: fill the asset-exchange-result/1 JSON\n"
            "contract inside job.json, then run:\n"
            "  cch-asset import-result <job_id> --result result.json\n",
            "utf-8")

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == '.zip':
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(stage.rglob('*')):
                    if p.is_file():
                        zf.write(p, p.relative_to(stage))
            packed: Path = out
        else:
            if out.exists():
                raise SystemExit(f'VALIDATION_ERROR: {out} already exists')
            shutil.copytree(stage, out)
            packed = out
        return {'job_id': job_id, 'packed_at': now_iso(), 'assets': len(entries),
                "path": str(packed),
                "manifest_sha256": _sha256((stage / "manifest.sha256").read_bytes())}
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
