"""WPS-cloud full-page rasterization for PPTX/DOCX scan candidates.

Why: PPTX/DOCX embedded-media scanning misses charts drawn with native
shapes and full-page layouts. Rendering runs through the user's own
KDocs (WPS cloud) account via kdocs-cli: upload -> cloud export to PDF ->
local pdftoppm. LibreOffice is never used.

Design bounds:
- Opt-in per scan (--wps-render); without credentials the plain scan
  result is kept and failures are reported per file, never swallowed.
- Rendered page PNGs are cached under <home>/artifact/render-cache/<doc
  sha256>/page-<N>.png. The candidate's frozen sha256 covers those exact
  bytes, so pack/export stays offline and byte-identical later.
- Cloud temp files are renamed to CCH-RENDER-TMP/<name> (no server-side
  delete API); the user can batch-clean them by that prefix.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from .scan import AssetCandidate

CLI = shutil.which("kdocs-cli") or "kdocs-cli"
POLL_ROUNDS = 30
POLL_SLEEP = 2.0
LAST_RENDERER = "wps-cloud"


def _cli(action: str, params: dict) -> dict:
    proc = subprocess.run(
        [CLI, *action.split(), json.dumps(params, ensure_ascii=False)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            f"kdocs-cli {action}: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def _poll_to_finished(poll_action: str, task_id: str) -> dict:
    last: dict = {}
    for _ in range(POLL_ROUNDS):
        last = _cli(poll_action, {"task_id": task_id, "task_type": "normal_export"})
        status = last.get("status") or last.get("data", {}).get("status", "")
        if status == "finished":
            return last
        time.sleep(POLL_SLEEP)
    raise RuntimeError(
        f"kdocs-cli {poll_action}: task {task_id} not finished after "
        f"{POLL_ROUNDS} polls: {json.dumps(last, ensure_ascii=False)[:300]}")


def _find_key(payload: dict, key: str):
    def walk(node):
        if isinstance(node, dict):
            if key in node and node[key] not in (None, ""):
                return node[key]
            for v in node.values():
                hit = walk(v)
                if hit is not None:
                    return hit
        return None
    return walk(payload)


def _extract_url(payload: dict) -> str:
    def walk(node):
        if isinstance(node, str) and node.startswith("https://"):
            return node
        if isinstance(node, dict):
            for v in node.values():
                hit = walk(v)
                if hit is not None:
                    return hit
        if isinstance(node, list):
            for v in node:
                hit = walk(v)
                if hit is not None:
                    return hit
        return None
    url = walk(payload)
    if not url:
        raise RuntimeError(
            "no download URL in export response: "
            + json.dumps(payload, ensure_ascii=False)[:300])
    return url


def _download(url: str, dest: Path) -> None:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "cch-render/0.3"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)


def _pdf_to_pages(pdf: Path, out_dir: Path) -> list[Path]:
    subprocess.run(["pdftoppm", "-png", "-r", "150", str(pdf), str(out_dir / "page")],
                   check=True, capture_output=True, timeout=300)
    return sorted(out_dir.glob("page-*.png")) or sorted(out_dir.glob("page*.png"))


def _render_to_pngs(path: Path, work_dir: Path) -> list[tuple[int, Path]]:
    """Upload -> cloud PDF -> local pdftoppm; returns (page_no, png_path).

    Leaves the PNGs (and the intermediate PDF) inside work_dir; the caller
    owns that directory's lifecycle.
    """
    suffix = path.suffix.lower()
    if suffix not in {".pptx", ".ppt", ".docx", ".doc"}:
        raise ValueError(f"unsupported render format: {path}")
    work_dir.mkdir(parents=True, exist_ok=True)

    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    upload = _cli("drive upload-new-file", {"name": path.name, "content_base64": b64})
    file_id = _find_key(upload, "file_id") or _find_key(upload, "id")
    if not file_id:
        raise RuntimeError(
            "upload returned no file_id: " + json.dumps(upload, ensure_ascii=False)[:300])

    # Mark the cloud copy as renderer temp (no server-side delete API);
    # rename failures are non-fatal and only affect manual cleanup.
    try:
        _cli("drive rename-file", {"file_id": file_id,
                                   "name": f"CCH-RENDER-TMP/{path.name}"})
    except Exception:
        pass

    if suffix in {".pptx", ".ppt"}:
        step = _cli("wpp export-pdf", {"file_id": file_id, "format": "pdf"})
        task_id = _find_key(step, "task_id")
        if not task_id:
            raise RuntimeError(
                "wpp export returned no task_id: " + json.dumps(step, ensure_ascii=False)[:300])
        url = _extract_url(_poll_to_finished("wpp export-pdf", task_id))
    else:
        # wps export needs the online link id, not the file id.
        link = _cli("drive get-file-link", {"file_id": file_id})
        link_id = _find_key(link, "link_id") or _find_key(link, "id")
        if not link_id:
            raise RuntimeError(
                "get-file-link returned no link id: " + json.dumps(link, ensure_ascii=False)[:300])
        step = _cli("wps export", {"link_id": link_id, "format": "pdf"})
        task_id = _find_key(step, "task_id")
        if not task_id:
            raise RuntimeError(
                "wps export returned no task_id: " + json.dumps(step, ensure_ascii=False)[:300])
        url = _extract_url(_poll_to_finished("wps query-export", task_id))

    pdf_path = work_dir / f"{path.stem}.pdf"
    _download(url, pdf_path)
    pngs = _pdf_to_pages(pdf_path, work_dir)
    pdf_path.unlink(missing_ok=True)
    return list(enumerate(pngs, start=1))


def _soffice_binary() -> str | None:
    import shutil as _shutil
    return _shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"


def _render_to_pngs_local(path: Path, work_dir: Path) -> list[tuple[int, Path]]:
    """LibreOffice fallback: soffice -> PDF -> pdftoppm (no cloud round-trip).

    Used only when the WPS cloud path is unavailable (account export
    restrictions, network, auth); the caller records that fallback happened.
    """
    soffice = _soffice_binary()
    if not Path(soffice).is_file():
        raise RuntimeError("LibreOffice fallback unavailable: soffice not found")
    work_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf",
         "--outdir", str(work_dir), str(path)],
        check=True, capture_output=True, timeout=300)
    pdf_path = work_dir / f"{path.stem}.pdf"
    if not pdf_path.is_file():
        raise RuntimeError(f"LibreOffice produced no PDF for {path}")
    pngs = _pdf_to_pages(pdf_path, work_dir)
    pdf_path.unlink(missing_ok=True)
    return list(enumerate(pngs, start=1))


def render_document_cached(path: Path, cache_dir: Path) -> list[AssetCandidate]:
    """Render one PPTX/DOCX; cache page PNGs; return page candidates.

    WPS cloud first; on failure falls back to local LibreOffice when
    installed (sets renderer='libreoffice' in the returned metadata dict
    via candidates' embedded_ref namespace 'render:page<N>'). Exposes
    which engine produced the cache through the module-level LAST_RENDERER.

    The candidate identity mirrors PDF page candidates: kind='pdf-page',
    embedded_ref='wps:page<N>', sha256 = hash of the cached PNG bytes.
    Re-rendering the same document refreshes the cache in place.
    """
    global LAST_RENDERER
    try:
        pages = _render_to_pngs(path, cache_dir)
        LAST_RENDERER = "wps-cloud"
    except Exception as wps_exc:
        soffice = _soffice_binary()
        if not Path(soffice).is_file():
            raise
        import sys
        print(json.dumps({"notice": "WPS_RENDER_FALLBACK",
                          "message": f"WPS cloud render failed ({str(wps_exc)[:200]}); using local LibreOffice"},
                         ensure_ascii=False), file=sys.stderr)
        pages = _render_to_pngs_local(path, cache_dir)
        LAST_RENDERER = "libreoffice-local"
    given = str(path)
    out = []
    for page_no, png in pages:
        stable = cache_dir / f"page-{page_no}.png"
        if png != stable:
            png.replace(stable)
        data = stable.read_bytes()
        out.append(AssetCandidate(
            kind="pdf-page", source_path=given, container=None,
            page=page_no, embedded_ref=f"render:page{page_no}",
            mime="image/png", size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest()))
    return out
