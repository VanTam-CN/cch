"""Document-to-candidate pipeline with LLM exchange jobs.

Export produces self-contained job packets (instructions, frozen blocks,
scope, output contract). Import validates the returned file mechanically:
job/manifest binding, structural schema, per-block coverage, exact quote
alignment inside frozen blocks, dedup and advisory matches against the
approved base. Machine validation never approves meaning: candidates stay
AI_INFERRED/TENTATIVE until a human proposal review.
"""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path
from .core import CCHError, TokenCounter, digest, retrieve
from . import sources as sources_mod
from .snapshots import load_base
from .store import Home, approved_commit, canon, canon_digest, now_iso

PROMPT_PATH = Path(__file__).resolve().parents[1]/'prompts'/'extraction-v1.md'
CHUNK_TARGET_TOKENS = 2000
CHUNK_MIN_TOKENS = 1500
CHUNK_MAX_TOKENS = 2500
TYPE_PREFIX = {'RULE': 'RULE', 'TERM': 'TERM', 'FACT': 'FACT', 'DECISION': 'DEC',
               'STATE': 'STATE', 'QUESTION': 'QST', 'PREFERENCE': 'PREF'}
CANDIDATE_STATUSES = ('VALIDATED', 'NEEDS_EVIDENCE_ALIGNMENT', 'NEEDS_SCOPE_REVIEW',
                      'DUPLICATE_OF', 'REJECTED_BY_INTENT')

def _extracted_dir(home, source_id):
    return home.extracted/source_id

def prepare(home, source_id):
    path, meta = sources_mod.get(home, source_id)
    binding = meta['bindings'][-1]
    blocks, report = _extract(path, meta['media_type'], meta['sha256'])
    if report.get('intake_status') == 'NEEDS_OCR':
        raise CCHError('NEEDS_OCR', f"{source_id}: no extractable text (scanned); OCR first, then re-register")
    directory = _extracted_dir(home, source_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'blocks.json').write_text(json.dumps(blocks, ensure_ascii=False, sort_keys=True, indent=1) + '\n', encoding='utf-8')
    (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    chunks = _chunk(home, blocks)
    (directory/'chunks.json').write_text(json.dumps(
        {'source_id': source_id, 'chunk_tokens_target': CHUNK_TARGET_TOKENS,
         'scope': {'domain': binding['domain'], 'project': binding['project'], 'topic': binding['topics']},
         'chunks': chunks}, ensure_ascii=False, sort_keys=True, indent=1) + '\n', encoding='utf-8')
    return {'source_id': source_id, 'blocks': len(blocks), 'chunks': len(chunks),
            'report': {k: report[k] for k in ['blocks', 'covered', 'not_covered'] if k in report},
            'needs_ocr_pages': report.get('needs_ocr_pages', []),
            'intake_status': report.get('intake_status', 'OK')}

def _extract(path, media_type, sha):
    from .extractors import extract
    try:
        return extract(path, media_type, sha)
    except CCHError:
        raise
    except Exception as exc:
        raise CCHError('INCOMPLETE_EXTRACTION', f'extractor failed on {Path(path).name}: {exc}') from exc

def _chunk(home, blocks):
    counter = TokenCounter('cl100k_base')
    chunks, current, size = [], [], 0
    for block in blocks:
        tokens = counter.count(block['text'])
        entry = {'block_id': block['block_id'], 'tokens': tokens, 'state': 'NOT_SENT', 'job_id': None}
        if current and size + tokens > CHUNK_TARGET_TOKENS and size >= CHUNK_MIN_TOKENS:
            chunks.append(current); current, size = [], 0
        current.append(entry); size += tokens
        if size > CHUNK_MAX_TOKENS and len(current) > 1:
            chunks.append(current[:-1]); current = [entry]; size = tokens
    if current:
        chunks.append(current)
    return chunks

def _load_chunks(home, source_id):
    path = _extracted_dir(home, source_id)/'chunks.json'
    if not path.is_file():
        raise CCHError('NOT_FOUND', f'{source_id}: run ingest prepare first')
    return json.loads(path.read_text(encoding='utf-8'))

def _save_chunks(home, source_id, data):
    (_extracted_dir(home, source_id)/'chunks.json').write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + '\n', encoding='utf-8')

def export_job(home, source_id, chunk_index, config_path=None):
    chunks = _load_chunks(home, source_id)
    if not 0 <= chunk_index < len(chunks['chunks']):
        raise CCHError('NOT_FOUND', f'chunk {chunk_index} does not exist ({len(chunks["chunks"])} total)')
    chunk = chunks['chunks'][chunk_index]
    blocks_data = json.loads((_extracted_dir(home, source_id)/'blocks.json').read_text(encoding='utf-8'))
    wanted = {e['block_id'] for e in chunk}
    frozen = [b for b in blocks_data if b['block_id'] in wanted]
    _, meta = sources_mod.get(home, source_id)
    revision = approved_commit(home)
    if revision is None:
        raise CCHError('EMPTY_STORE', 'cannot export jobs against an empty store')
    existing, truncated = [], False
    _, base = load_base(home, revision)
    scope = chunks['scope']
    for oid in sorted(base.objects):
        o = base.objects[oid]
        if o['scope']['domain'] in {scope['domain'], '*'} and o['scope']['project'] in {scope['project'], '*'}:
            existing.append({'id': oid, 'type': o['type'], 'content': o['content']})
    counter = TokenCounter('cl100k_base')
    if sum(counter.count(e['content']) for e in existing) > CHUNK_TARGET_TOKENS:
        existing = [{'id': e['id'], 'type': e['type']} for e in existing]
        truncated = True
    job_id = 'JOB-' + uuid.uuid4().hex[:12]
    job = {'schema_version': 'exchange-job/1', 'job_id': job_id, 'created_at': now_iso(),
           'source': {'source_id': source_id, 'sha256': meta['sha256'], 'name': meta['name'],
                      'media_type': meta['media_type'], 'source_declared_date': meta.get('source_declared_date')},
           'scope': scope, 'base_revision': revision,
           'instructions_version': 'extraction/1', 'instructions': PROMPT_PATH.read_text(encoding='utf-8'),
           'output_schema': 'schemas/exchange-result.schema.json',
           'blocks': frozen,
           'existing_cognition': {'revision': revision, 'objects': existing, 'truncated': truncated,
                                  'note': 'reference only; never treat as words spoken by the source'}}
    job['manifest_hash'] = canon_digest({k: v for k, v in job.items() if k != 'manifest_hash'})
    job_dir = home.jobs/job_id
    job_dir.mkdir(parents=True)
    (job_dir/'job.json').write_text(json.dumps(job, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    for entry in chunk:
        entry['state'], entry['job_id'] = 'SENT', job_id
    _save_chunks(home, source_id, chunks)
    return {'job_id': job_id, 'job_path': str(job_dir/'job.json'), 'blocks': len(frozen),
            'manifest_hash': job['manifest_hash'],
            'note': 'hand job.json to your LLM; save its JSON answer as result.json in the same directory (or pass --result)'}

def _job_dir(home, job_id):
    directory = home.jobs/job_id
    if not (directory/'job.json').is_file():
        raise CCHError('NOT_FOUND', f'job {job_id} not found')
    return directory

def import_result(home, job_id, result_path, config_path=None):
    directory = _job_dir(home, job_id)
    job = json.loads((directory/'job.json').read_text(encoding='utf-8'))
    try:
        result = json.loads(Path(result_path).read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CCHError('INVALID_RESULT', f'result is not valid JSON: {exc}') from exc
    if result.get('schema_version') != 'exchange-result/1':
        raise CCHError('INVALID_RESULT', 'schema_version must be exchange-result/1')
    unknown = set(result) - {'schema_version', 'job_id', 'manifest_hash', 'blocks', 'candidates', 'model_meta'}
    if unknown:
        raise CCHError('INVALID_RESULT', f'unknown result fields rejected: {sorted(unknown)} '
                        '(candidates are proposals only; no approval or command fields are accepted)')
    if result.get('job_id') != job_id:
        raise CCHError('INVALID_RESULT', f"job_id mismatch: expected {job_id}")
    manifest = canon_digest({k: v for k, v in job.items() if k != 'manifest_hash'})
    if result.get('manifest_hash') != manifest:
        raise CCHError('INVALID_RESULT', 'manifest_hash does not match the exported job (job changed or wrong file)')
    blocks = {b['block_id']: b for b in job['blocks']}
    block_status = {b['block_id']: 'UNREPORTED' for b in job['blocks']}
    for entry in result.get('blocks') or []:
        bid = entry.get('block_id')
        if bid not in blocks:
            raise CCHError('INVALID_RESULT', f'unknown block in result: {bid}')
        if entry.get('status') not in {'PROCESSED', 'NO_CANDIDATE', 'NEEDS_RETRY'}:
            raise CCHError('INVALID_RESULT', f'{bid}: invalid block status')
        block_status[bid] = entry['status']
    candidates, seen_keys = [], {}
    raw_candidates = result.get('candidates') or []
    if not isinstance(raw_candidates, list):
        raise CCHError('INVALID_RESULT', 'candidates must be a list')
    _, base = load_base(home, job['base_revision'])
    for raw in raw_candidates:
        candidates.append(_validate_candidate(raw, blocks, job, base, seen_keys))
    record = {'job_id': job_id, 'imported_at': now_iso(), 'base_revision': job['base_revision'],
              'source': job['source'], 'scope': job['scope'],
              'block_status': block_status, 'candidates': candidates,
              'model_meta': result.get('model_meta') or {'model': 'unknown'}}
    (directory/'candidates.json').write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    statuses = [c['status'] for c in candidates]
    failed_blocks = [b for b, s in block_status.items() if s in {'NEEDS_RETRY', 'UNREPORTED'}]
    chunks_data = _load_chunks(home, job['source']['source_id'])
    chunk = next((c for c in chunks_data['chunks'] if any(e.get('job_id') == job_id for e in c)), None)
    if chunk is not None:
        for entry in chunk:
            entry['state'] = 'NEEDS_RETRY' if failed_blocks else 'RESULT_RECEIVED'
        _save_chunks(home, job['source']['source_id'], chunks_data)
    summary = {'job_id': job_id,
               'blocks_reported': sum(1 for s in block_status.values() if s != 'UNREPORTED'),
               'blocks_total': len(block_status), 'blocks_failed': failed_blocks,
               'candidates': len(candidates),
               'by_status': {s: statuses.count(s) for s in CANDIDATE_STATUSES if statuses.count(s)}}
    return summary

def _validate_candidate(raw, blocks, job, base, seen_keys):
    def fail(msg):
        raise CCHError('INVALID_RESULT', msg)
    if not isinstance(raw, dict):
        fail('candidate must be an object')
    unknown = set(raw) - {'candidate_id', 'type', 'content', 'rationale', 'scope_suggestion',
                          'evidence_spans', 'uncertainty', 'temporal', 'matched_existing_ids',
                          'change_intent', 'notes'}
    if unknown:
        fail(f'unknown candidate fields rejected: {sorted(unknown)}')
    cid = raw.get('candidate_id')
    if not (isinstance(cid, str) and re.fullmatch(r'CAND-[A-Za-z0-9][A-Za-z0-9_-]*', cid or '')):
        fail('candidate_id must match CAND-<id>')
    ctype = raw.get('type')
    if ctype not in TYPE_PREFIX:
        fail(f'{cid}: invalid type {ctype}')
    content = raw.get('content')
    if not (isinstance(content, str) and content.strip()):
        fail(f'{cid}: content required')
    spans = raw.get('evidence_spans')
    if not isinstance(spans, list) or not spans:
        fail(f'{cid}: at least one evidence span required')
    uncertainty = raw.get('uncertainty')
    if not isinstance(uncertainty, dict) or uncertainty.get('kind') not in \
            {'EXPLICIT_HEDGE', 'INFERRED', 'TENTATIVE', 'CONDITIONAL', 'UNKNOWN'}:
        fail(f'{cid}: uncertainty.kind required')
    intent = raw.get('change_intent')
    if intent not in {'NEW', 'ADD_EVIDENCE', 'SUGGEST_SUPERSEDE', 'POSSIBLE_DUPLICATE', 'CONFLICT', 'DO_NOT_IMPORT'}:
        fail(f'{cid}: invalid change_intent')
    aligned, misaligned = [], []
    for span in spans:
        bid, quote = span.get('block_id'), span.get('quote')
        if bid not in blocks or not isinstance(quote, str) or not quote:
            fail(f'{cid}: span references unknown block or empty quote')
        text = blocks[bid]['text']
        start, end = span.get('start'), span.get('end')
        if start is not None and end is not None:
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text)):
                fail(f'{cid}: span offsets out of range for {bid}')
            if text[start:end] != quote:
                raise CCHError('QUOTE_MISMATCH', f'{cid}: quote is not the exact text at [{start},{end}) in {bid}')
            aligned.append({'block_id': bid, 'start': start, 'end': end, 'quote': quote,
                            'offset_source': 'model_verified'})
        else:
            occurrences = [m.start() for m in re.finditer(re.escape(quote), text)]
            if len(occurrences) == 1:
                aligned.append({'block_id': bid, 'start': occurrences[0], 'end': occurrences[0] + len(quote),
                                'quote': quote, 'offset_source': 'program_aligned'})
            else:
                misaligned.append({'block_id': bid, 'quote': quote,
                                   'reason': f'{len(occurrences)} exact occurrences in block'})
    key = digest(canon({'content': content, 'spans': [(a['block_id'], a['start'], a['end']) for a in aligned]}))
    duplicate_of = seen_keys.get(key)
    seen_keys[key] = cid
    scope_ok = True
    suggestion = raw.get('scope_suggestion')
    if suggestion is None:
        scope_ok = False
    elif not (isinstance(suggestion, dict) and suggestion.get('domain') == job['scope']['domain']
              and suggestion.get('project') == job['scope']['project']):
        scope_ok = False
    if intent == 'DO_NOT_IMPORT':
        status = 'REJECTED_BY_INTENT'
    elif duplicate_of:
        status = 'DUPLICATE_OF'
    elif misaligned:
        status = 'NEEDS_EVIDENCE_ALIGNMENT'
    elif not scope_ok:
        status = 'NEEDS_SCOPE_REVIEW'
    else:
        status = 'VALIDATED'
    matches = []
    if status == 'VALIDATED':
        config = {'top_k': 5, 'dependency_depth': 1, 'tokenizer': 'cl100k_base',
                  'evidence_words': [], 'full_words': []}
        from .core import config_load, governance, instant
        cfg = config_load()
        states = governance(base, job['scope']['domain'], job['scope']['project'], job['scope'].get('topic', []), instant(job['created_at']))
        matches = [row['id'] for row in retrieve(content, base, states, cfg)[:5]]
    return {'candidate_id': cid, 'status': status, 'duplicate_of': duplicate_of,
            'type': ctype, 'content': content, 'rationale': raw.get('rationale'),
            'scope_suggestion': suggestion, 'evidence_spans': aligned,
            'misaligned_spans': misaligned, 'uncertainty': uncertainty,
            'temporal': raw.get('temporal'),
            'matched_existing_ids': matches or raw.get('matched_existing_ids') or [],
            'change_intent': intent, 'notes': raw.get('notes'),
            'authority': 'AI_INFERRED', 'confidence': 'TENTATIVE'}

def status(home, source_id=None):
    if source_id:
        return _status_for(home, source_id)
    return [_status_for(home, d.name) for d in sorted(home.extracted.iterdir()) if d.is_dir()]

def _status_for(home, source_id):
    directory = _extracted_dir(home, source_id)
    if not (directory/'chunks.json').is_file():
        return {'source_id': source_id, 'state': 'NOT_PREPARED'}
    blocks = json.loads((directory/'blocks.json').read_text(encoding='utf-8'))
    chunks = _load_chunks(home, source_id)
    states = [e['state'] for chunk in chunks['chunks'] for e in chunk]
    jobs_summary = []
    for job_dir in sorted(home.jobs.iterdir()):
        cands = job_dir/'candidates.json'
        if cands.is_file():
            record = json.loads(cands.read_text(encoding='utf-8'))
            if record['source']['source_id'] == source_id:
                jobs_summary.append({'job_id': record['job_id'],
                                     'candidates': {s: sum(1 for c in record['candidates'] if c['status'] == s)
                                                    for s in CANDIDATE_STATUSES if any(c['status'] == s for c in record['candidates'])}})
    processed = sum(1 for s in states if s in {'RESULT_RECEIVED', 'VALIDATED'})
    return {'source_id': source_id, 'blocks': len(blocks), 'chunks': len(chunks['chunks']),
            'block_states': {s: states.count(s) for s in sorted(set(states))},
            'blocks_processed': processed, 'blocks_pending': len(states) - processed,
            'jobs': jobs_summary,
            'coverage': f'{processed}/{len(states)} block entries processed (NO_CANDIDATE counts as processed once results arrive)'}

def build_proposal(home, job_id, candidate_ids, proposal_id=None, reason=None, agent_id='local-agent',
                   session_id=None, config_path=None):
    """Turn VALIDATED NEW candidates from one job into a frozen proposal."""
    directory = _job_dir(home, job_id)
    record = json.loads((directory/'candidates.json').read_text(encoding='utf-8'))
    by_id = {c['candidate_id']: c for c in record['candidates']}
    wanted = []
    for cid in candidate_ids:
        if cid not in by_id:
            raise CCHError('NOT_FOUND', f'{cid}: not a candidate of {job_id}')
        candidate = by_id[cid]
        if candidate['status'] != 'VALIDATED':
            raise CCHError('INVALID_PROPOSAL', f"{cid}: status {candidate['status']}; only VALIDATED candidates can enter a proposal")
        if candidate['change_intent'] not in {'NEW'}:
            raise CCHError('INVALID_PROPOSAL', f"{cid}: change_intent {candidate['change_intent']} requires manual proposal construction")
        wanted.append(candidate)
    if not wanted:
        raise CCHError('INVALID_PROPOSAL', 'no usable candidates selected')
    revision = approved_commit(home)
    if record['base_revision'] != revision:
        from .proposals import refresh as _  # noqa: F401  (documented path; jobs re-export instead)
        raise CCHError('STALE_BASE', f"job was exported at {record['base_revision'][:12]}; re-export the job against {revision[:12]}")
    source_meta = sources_mod.inspect(home, record['source']['source_id'])
    operations, assets = [], []
    used_ids = set()
    for candidate in wanted:
        oid = TYPE_PREFIX[candidate['type']] + '-' + uuid.uuid4().hex[:12]
        while oid in used_ids:
            oid = TYPE_PREFIX[candidate['type']] + '-' + uuid.uuid4().hex[:12]
        used_ids.add(oid)
        eid = 'EV-' + uuid.uuid4().hex[:12]
        spans = candidate['evidence_spans']
        evidence_text = '\n'.join(span['quote'] for span in spans)
        declared = (candidate.get('temporal') or {}).get('source_declared_date')
        obj = {'id': oid, 'type': candidate['type'],
               'scope': {'domain': record['scope']['domain'], 'project': record['scope']['project'],
                         'topic': record['scope'].get('topic', [])},
               'status': 'ACTIVE', 'confidence': candidate['confidence'], 'authority': candidate['authority'],
               'priority': 'REFERENCE', 'content': candidate['content'],
               'rationale': candidate['rationale'],
               'relations': {'supersedes': [], 'depends_on': [], 'conflicts_with': [], 'derived_from': []},
               'provenance': {'evidence_refs': [eid]},
               'lifecycle': {'valid_from': declared, 'valid_until': None, 'ttl_hours': None},
               'retrieval': {'keywords': [], 'aliases': []},
               'tags': ['llm-candidate', job_id]}
        operations.append({'op': 'create', 'object_id': oid, 'expected_object_hash': None,
                           'after': obj, 'evidence_bindings': [eid]})
        assets.append({'id': eid, 'summary': f"{source_meta['name']} 候选证据（{len(spans)} 处定位）",
                       'source_text': evidence_text,
                       'source': {'source_id': record['source']['source_id'],
                                  'source_sha256': record['source']['sha256'],
                                  'source_name': record['source']['name'],
                                  'block_id': spans[0]['block_id'],
                                  'spans': [[s['start'], s['end']] for s in spans]}})
    envelope = {'schema_version': 'proposal/1',
                'proposal_id': proposal_id or ('PR-' + uuid.uuid4().hex[:12]),
                'proposal_version': 1, 'base_revision': revision,
                'scope': record['scope'],
                'submitter': {'agent_id': agent_id, 'session_id': session_id or job_id},
                'origin': {'kind': 'import_job', 'job_id': job_id},
                'reason': reason or f'从 {record["source"]["name"]} 提炼的候选，待人工审核',
                'operations': operations, 'evidence_assets': assets,
                'candidate_assets': [c['candidate_id'] for c in wanted],
                'risk_flags': ['LLM_EXTRACTED'] + (['NEW_FACT'] if any(c['type'] == 'FACT' for c in wanted) else []),
                'requested_at': now_iso()}
    from .proposals import freeze
    return freeze(home, envelope)
