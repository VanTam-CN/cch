"""VLM exchange jobs for asset semantics.

Export freezes candidate metadata (including asset_id and sha256 bindings)
into a self-contained job the user hands to any multimodal agent. Import
mechanically validates the returned result against the frozen manifest and
the artifact index. Meaning is never machine-approved: semantics land as
indexed metadata; asset_status stays unknown unless a human decides.
"""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from ..store import canon, canon_digest, now_iso

SCHEMA_VERSION = 'asset-exchange-job/1'
RESULT_VERSION = 'asset-exchange-result/1'


def _job_dir(root: Path, job_id: str) -> Path:
    d = Path(root)/'artifact'/'jobs'/job_id
    if not d.is_dir():
        raise SystemExit(f'NOT_FOUND: asset job {job_id}')
    return d


def export_job(root: Path, asset_ids: list[str] | None = None, limit: int = 20) -> dict:
    from .index import connect, ASSET_ID_RE
    conn = connect(root)
    if asset_ids:
        for aid in asset_ids:
            if not ASSET_ID_RE.match(aid):
                raise SystemExit(f'VALIDATION_ERROR: bad asset id {aid!r}')
        rows = [conn.execute("SELECT * FROM assets WHERE asset_id=?", (aid,)).fetchone()
                for aid in asset_ids]
        if any(r is None for r in rows):
            missing = [aid for aid, r in zip(asset_ids, rows) if r is None]
            raise SystemExit(f'NOT_FOUND: {missing}')
    else:
        rows = conn.execute(
            "SELECT * FROM assets WHERE index_state='pending' ORDER BY asset_id LIMIT ?",
            (limit,)).fetchall()
    rows = [r for r in rows if r['index_state'] == 'pending']
    if not rows:
        raise SystemExit('EMPTY_QUEUE: no pending assets to export')
    job_id = 'JOB-' + uuid.uuid4().hex[:12]
    entries = [{'asset_id': r['asset_id'], 'kind': r['kind'], 'mime': r['mime'],
                'size_bytes': r['size_bytes'], 'sha256': r['sha256'],
                'locator': {'file': r['source_file'],
                            **({'page_or_slide': r['page_or_slide']} if r['page_or_slide'] else {}),
                            **({'embedded_ref': r['embedded_ref']} if r['embedded_ref'] else {})},
                'scope': {'domain': r['domain'], 'project': r['project'],
                          'topic': json.loads(r['topics'])}}
               for r in rows]
    job = {'schema_version': SCHEMA_VERSION, 'job_id': job_id, 'created_at': now_iso(),
           'asset_count': len(entries),
           'manifest_hash': canon_digest(entries),
           'output_contract': {
               'result_schema_version': RESULT_VERSION,
               'per_asset_required': ['asset_id', 'sha256', 'title', 'summary',
                                      'extracted_text', 'entities', 'visual_type'],
               'asset_status_enum': ['unknown', 'current', 'superseded'],
               'notes': 'title/summary/extracted_text 用素材内可见文字与客观描述；不要把素材当成当前有效事实。'
                        'asset_status 仅作人工复核建议，导入后一律按 unknown 处理；版本替代由人工 supersede 决定。'},
           'assets': entries}
    directory = Path(root)/'artifact'/'jobs'/job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return {'job_id': job_id, 'job_path': str(directory/'job.json'), 'assets': len(entries)}


def import_result(root: Path, job_id: str, result_path: Path) -> dict:
    from .index import connect, apply_semantics
    directory = _job_dir(root, job_id)
    job = json.loads((directory/'job.json').read_text('utf-8'))
    result = json.loads(Path(result_path).read_text('utf-8'))
    if result.get('schema_version') != RESULT_VERSION:
        raise SystemExit(f'INVALID_RESULT: schema_version must be {RESULT_VERSION}')
    if result.get('job_id') != job_id:
        raise SystemExit(f'INVALID_RESULT: job_id mismatch, expected {job_id}')
    entries = job['assets']
    if canon_digest(entries) != job['manifest_hash']:
        raise SystemExit('INVALID_JOB: job manifest hash mismatch')
    by_id = {e['asset_id']: e for e in entries}
    seen, applied, skipped, suggested = [], [], [], []
    conn = connect(root)
    # Validate everything first; apply in one transaction only if the whole
    # result is clean. A failed import must leave no partial state behind.
    prepared = []
    for item in result.get('assets', []):
        aid = item.get('asset_id')
        if aid not in by_id:
            raise SystemExit(f'INVALID_RESULT: {aid!r} not in job manifest')
        if aid in seen:
            raise SystemExit(f'INVALID_RESULT: duplicate asset {aid}')
        seen.append(aid)
        if item.get('sha256') != by_id[aid]['sha256']:
            raise SystemExit(f'INVALID_RESULT: {aid}: sha256 does not match frozen candidate')
        for field in ('title', 'summary', 'extracted_text', 'entities', 'visual_type'):
            if field not in item:
                raise SystemExit(f'INVALID_RESULT: {aid}: missing {field}')
        status = item.get('asset_status', 'unknown')
        if status not in ('unknown', 'current', 'superseded'):
            raise SystemExit(f'INVALID_RESULT: {aid}: bad asset_status {status!r}')
        for field in ('extracted_text', 'entities'):
            if not isinstance(item[field], list):
                raise SystemExit(f'INVALID_RESULT: {aid}: {field} must be a list')
        if not isinstance(item['title'], str) or not isinstance(item['summary'], str):
            raise SystemExit(f'INVALID_RESULT: {aid}: title/summary must be strings')
        semantic = {'title': str(item['title'])[:300], 'summary': str(item['summary'])[:2000],
                    'extracted_text': [str(t)[:200] for t in (item['extracted_text'] or [])][:100],
                    'entities': [str(e)[:200] for e in (item['entities'] or [])][:100],
                    'visual_type': str(item['visual_type'])[:100],
                    'asset_status': 'unknown', 'observed_at': str(item.get('observed_at', ''))[:40],
                    'confidence': str(item.get('confidence', ''))[:40]}
        if status != 'unknown':
            suggested.append({'asset_id': aid, 'suggested_asset_status': status})
        prepared.append((aid, semantic))
    for aid, semantic in prepared:
        apply_semantics(conn, aid, semantic, job_id, 'vlm-exchange-v1', commit=False)
    conn.commit()
    applied = [aid for aid, _ in prepared]
    skipped = [e['asset_id'] for e in entries if e['asset_id'] not in seen]
    receipt = {'job_id': job_id, 'imported_at': now_iso(), 'applied': applied,
               'skipped_still_pending': skipped, 'result_path': str(result_path),
               'status_suggestions': suggested}
    (directory/'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return receipt
