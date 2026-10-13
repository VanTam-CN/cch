"""M2-B.1 L2 semantic re-enrichment exchange (contract version /2).

Separate from the /1 title/summary exchange: /2 jobs re-enrich EXISTING
indexed assets with grounded per-asset concepts only. No aliases here -
"how users might ask" bridging is L3 scope. Governance-neutral by
construction: enrichment never touches asset_status, index_state,
lineage, or the Cognitive Base.
"""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from ..store import canon_digest, now_iso

SCHEMA_VERSION = 'asset-exchange-job/2'
RESULT_VERSION = 'asset-exchange-result/2'


def _job_dir(root: Path, job_id: str) -> Path:
    d = Path(root)/'artifact'/'jobs'/job_id
    if not d.is_dir():
        raise SystemExit(f'NOT_FOUND: asset job {job_id}')
    return d


def export_enrich_job(root: Path, asset_ids: list[str] | None = None,
                      limit: int = 99) -> dict:
    from .index import connect, ASSET_ID_RE
    conn = connect(root)
    if asset_ids:
        for aid in asset_ids:
            if not ASSET_ID_RE.match(aid):
                raise SystemExit(f'VALIDATION_ERROR: bad asset id {aid!r}')
        rows = [conn.execute('SELECT * FROM assets WHERE asset_id=?', (aid,)).fetchone()
                for aid in asset_ids]
        if any(r is None for r in rows):
            missing = [aid for aid, r in zip(asset_ids, rows) if r is None]
            raise SystemExit(f'NOT_FOUND: {missing}')
    else:
        rows = conn.execute(
            "SELECT * FROM assets WHERE index_state='indexed' ORDER BY asset_id LIMIT ?",
            (limit,)).fetchall()
    if not rows:
        raise SystemExit('EMPTY_QUEUE: no indexed assets to enrich')
    job_id = 'JOB-' + uuid.uuid4().hex[:12]
    entries = [{'asset_id': r['asset_id'], 'sha256': r['sha256'],
                'mime': r['mime'], 'size_bytes': r['size_bytes'],
                'locator': {'file': r['source_file'],
                            **({'page_or_slide': r['page_or_slide']} if r['page_or_slide'] else {})},
                'current_semantics': {'title': r['title'], 'summary': r['summary']}}
               for r in rows]
    job = {'schema_version': SCHEMA_VERSION, 'job_id': job_id, 'created_at': now_iso(),
           'asset_count': len(entries),
           'manifest_hash': canon_digest(entries),
           'output_contract': {
               'result_schema_version': RESULT_VERSION,
               'per_asset_required': ['asset_id', 'sha256', 'concepts'],
               'concepts_rules': [
                   'concepts = 素材本身直接表达的主题、对象、机制、活动或关系',
                   '只能依据可见文字和图形内容提取',
                   '不得根据项目常识补充素材中未表达的概念',
                   '不得生成"用户可能怎么问"的口语化表达',
                   '不得为了扩大召回生成同义词列表',
                   '3-12 个，每个 2-16 字，优先名词/名词短语，去重',
                   '不保留"本页/项目/研究"等低信息泛词'],
               'notes': 'concepts 仅作为检索语义 metadata，不改变 asset_status、lineage 或 Cognitive Base 权威性。'},
           'assets': entries}
    directory = Path(root)/'artifact'/'jobs'/job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2) + chr(10), 'utf-8')
    return {'job_id': job_id, 'job_path': str(directory/'job.json'), 'assets': len(entries)}


def import_enrich_result(root: Path, job_id: str, result_path: Path) -> dict:
    from .index import connect, apply_concept_enrichment
    directory = _job_dir(root, job_id)
    job = json.loads((directory/'job.json').read_text('utf-8'))
    result = json.loads(Path(result_path).read_text('utf-8'))
    if result.get('schema_version') != RESULT_VERSION:
        raise SystemExit(f'INVALID_RESULT: schema_version must be {RESULT_VERSION}')
    if result.get('job_id') != job_id:
        raise SystemExit(f'INVALID_RESULT: job_id mismatch, expected {job_id}')
    if job.get('schema_version') != SCHEMA_VERSION:
        raise SystemExit(f'INVALID_JOB: schema_version must be {SCHEMA_VERSION}')
    entries = job['assets']
    if canon_digest(entries) != job['manifest_hash']:
        raise SystemExit('INVALID_JOB: job manifest hash mismatch')
    by_id = {e['asset_id']: e for e in entries}
    seen, prepared = [], []
    # Validate everything first; apply in one transaction only if the
    # whole result is clean (no half-batch writes on failure).
    for item in result.get('assets', []):
        aid = item.get('asset_id')
        if aid not in by_id:
            raise SystemExit(f'INVALID_RESULT: {aid!r} not in job manifest')
        if aid in seen:
            raise SystemExit(f'INVALID_RESULT: duplicate asset {aid}')
        seen.append(aid)
        if item.get('sha256') != by_id[aid]['sha256']:
            raise SystemExit(f'INVALID_RESULT: {aid}: sha256 does not match frozen candidate')
        if 'concepts' not in item:
            raise SystemExit(f'INVALID_RESULT: {aid}: missing concepts')
        if not isinstance(item['concepts'], list):
            raise SystemExit(f'INVALID_RESULT: {aid}: concepts must be a list of strings')
        prepared.append((aid, item['concepts']))
    conn = connect(root)
    try:
        for aid, concepts in prepared:
            apply_concept_enrichment(conn, aid, concepts, 'vlm-concepts-v1', job_id)
        conn.commit()
    except SystemExit:
        conn.rollback()
        raise
    applied = [aid for aid, _ in prepared]
    skipped = [e['asset_id'] for e in entries if e['asset_id'] not in seen]
    receipt = {'job_id': job_id, 'imported_at': now_iso(), 'applied': applied,
               'skipped': skipped, 'result_path': str(result_path),
               'enrichment_method': 'vlm-concepts-v1', 'semantic_schema_version': 2}
    (directory/'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + chr(10), 'utf-8')
    return receipt
