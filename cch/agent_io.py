"""Agent-facing entry points: unified context reads, receipts, freshness checks.

One task entry resolves the approved ref exactly once and compiles from that
snapshot. Every call gets its own run directory and a canonical-JSON receipt.
Freshness check knows that an unchanged revision can still be time-expired:
TTL, valid_until and not-yet-valid boundaries are re-evaluated for the scope.
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .core import CCHError, compile_context, config_load, governance
from . import store
from .snapshots import load_base
from .store import Home, approved_commit, canon, canon_digest, now_iso, resolve_revision

RECEIPT_FIELDS = ['run_id', 'knowledge_revision', 'compiler_version', 'compiler_hash', 'config_hash',
                  'source_hash', 'query_hash', 'context_hash', 'as_of', 'scope', 'budget',
                  'evidence_mode', 'tokens', 'created_at']

def agent_context(home, query, domain, project, topics=(), as_of=None, budget=16000,
                  config_path=None, evidence='auto', revision=None, run_root=None):
    if not query or not query.strip():
        raise CCHError('VALIDATION_ERROR', 'task query required; scope must not be guessed')
    if not domain or not project or '*' in (domain, project):
        raise CCHError('SCOPE_VIOLATION', 'explicit domain and project required')
    as_of = as_of or datetime.now(timezone.utc)
    config = config_load(config_path)
    commit = resolve_revision(home, revision)
    if commit is None:
        raise CCHError('EMPTY_STORE', 'store has no approved cognition yet; wait for the first approved publish')
    _, base = load_base(home, commit)
    context, audit = compile_context(base, query, domain, project, list(topics), as_of, budget, config, evidence)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    run_dir = (run_root or home.runs)/run_id
    run_dir.mkdir(parents=True)
    (run_dir/'context.md').write_text(context, encoding='utf-8')
    (run_dir/'audit.json').write_text(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    receipt = {'run_id': run_id, 'knowledge_revision': commit, 'compiler_version': audit['compiler_version'],
               'compiler_hash': audit['compiler_hash'], 'config_hash': audit['config_hash'],
               'source_hash': audit['source_hash'], 'query_hash': canon_digest(query),
               'context_hash': audit['context_hash'], 'as_of': audit['as_of'],
               'scope': {'domain': domain, 'project': project, 'topics': sorted(topics)},
               'budget': budget, 'evidence_mode': audit['evidence_mode'], 'tokens': audit['tokens'],
               'created_at': now_iso()}
    receipt['receipt_hash'] = canon_digest({k: receipt[k] for k in RECEIPT_FIELDS})
    (run_dir/'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return {'receipt': receipt, 'context_path': str(run_dir/'context.md'),
            'audit_path': str(run_dir/'audit.json'), 'run_dir': str(run_dir)}

def agent_check(home, receipt, domain=None, project=None, now=None, config_path=None):
    """FRESH only if revision, compiler/config and time validity all hold for the scope."""
    if isinstance(receipt, (str, Path)):
        receipt = json.loads(Path(receipt).read_text(encoding='utf-8'))
    scope = receipt.get('scope') or {}
    domain = domain or scope.get('domain')
    project = project or scope.get('project')
    if not domain or not project:
        raise CCHError('VALIDATION_ERROR', 'receipt scope or explicit domain/project required')
    now = now or datetime.now(timezone.utc)
    result = {'checked_at': now.isoformat(), 'knowledge_revision_at_check': approved_commit(home),
              'receipt_run_id': receipt.get('run_id')}
    if approved_commit(home) != receipt.get('knowledge_revision'):
        return {**result, 'status': 'REVISION_CHANGED', 'action': 'RECOMPILE',
                'reason': 'approved revision moved since the run'}
    config = config_load(config_path)
    if canon_digest(config) != receipt.get('config_hash') or _compiler_hash() != receipt.get('compiler_hash'):
        return {**result, 'status': 'CONFIG_OR_COMPILER_CHANGED', 'action': 'RECOMPILE',
                'reason': 'compiler or config changed since the run'}
    _, base = load_base(home, receipt['knowledge_revision'])
    as_of = _parse_as_of(receipt.get('as_of'))
    before = governance(base, domain, project, scope.get('topics', []), as_of)
    after = governance(base, domain, project, scope.get('topics', []), now)
    crossed = sorted(oid for oid in after
                     if after[oid]['eligible'] != before.get(oid, {}).get('eligible', False)
                     or after[oid]['effective_status'] != before.get(oid, {}).get('effective_status'))
    if crossed:
        return {**result, 'status': 'RECOMPILE_REQUIRED', 'action': 'RECOMPILE',
                'reason': 'time boundaries crossed since the run (TTL/valid_until/valid_from)',
                'affected': crossed}
    return {**result, 'status': 'FRESH', 'action': 'NONE'}

def _parse_as_of(value):
    from .core import instant
    return instant(value) if isinstance(value, str) else datetime.now(timezone.utc)

def _compiler_hash():
    from .core import digest
    return digest({p.name: digest(p.read_bytes()) for p in sorted(Path(__file__).parent.glob('*.py'))})

def export_pack(home, run_dir, target_dir):
    """Self-contained context pack for agents that cannot read the local store."""
    run_dir = Path(run_dir); target_dir = Path(target_dir)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise CCHError('TARGET_NOT_EMPTY', 'pack target must be empty')
    target_dir.mkdir(parents=True)
    receipt = json.loads((run_dir/'receipt.json').read_text(encoding='utf-8'))
    for name in ['context.md', 'audit.json', 'receipt.json']:
        (target_dir/name).write_text((run_dir/name).read_text(encoding='utf-8'), encoding='utf-8')
    (target_dir/'README.txt').write_text(
        'This pack is a snapshot compiled at the stated revision and as-of time.\n'
        'It does not auto-update. Submit new conclusions as a proposal file bound to\n'
        f"base_revision {receipt['knowledge_revision']} via `cch proposal create --file`.\n", encoding='utf-8')
    return {'pack_dir': str(target_dir), 'knowledge_revision': receipt['knowledge_revision']}
