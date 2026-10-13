"""Proposal contract shared by daily updates and document import.

Submissions are untrusted input: unknown fields, unknown ops, path escapes,
out-of-scope objects and forged approval fields are rejected before anything
is frozen. Frozen proposal versions are immutable; changes create new versions
and invalidate previous approvals via hash binding.
"""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path
from .core import CCHError
from .publisher import check_applicability, object_hash
from .snapshots import load_base
from .store import Home, approved_commit, canon, canon_digest, history, now_iso, resolve_revision

STATUSES = ['SUBMITTED', 'NEEDS_FIX', 'READY', 'APPROVED', 'REJECTED', 'STALE', 'APPLIED']
ENVELOPE_KEYS = {'schema_version', 'proposal_id', 'proposal_version', 'base_revision', 'scope',
                 'submitter', 'origin', 'reason', 'operations', 'evidence_assets',
                 'candidate_assets', 'risk_flags', 'requested_at'}

def validate_envelope(envelope):
    if not isinstance(envelope, dict) or envelope.get('schema_version') != 'proposal/1':
        raise CCHError('INVALID_PROPOSAL', 'schema_version must be proposal/1')
    unknown = set(envelope) - ENVELOPE_KEYS
    if unknown:
        raise CCHError('INVALID_PROPOSAL', f'unknown envelope fields: {sorted(unknown)}')
    pid = envelope.get('proposal_id')
    if not (isinstance(pid, str) and re.fullmatch(r'PR-[A-Za-z0-9][A-Za-z0-9_-]*', pid)):
        raise CCHError('INVALID_PROPOSAL', 'proposal_id must match PR-<id>')
    version = envelope.get('proposal_version')
    if type(version) is not int or version < 1:
        raise CCHError('INVALID_PROPOSAL', 'proposal_version must be a positive integer')
    revision = envelope.get('base_revision')
    if not (isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision)):
        raise CCHError('INVALID_PROPOSAL', 'base_revision must be a commit hash')
    scope = envelope.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'domain', 'project', 'topic'}:
        raise CCHError('INVALID_PROPOSAL', 'scope must be {domain, project, topic}')
    if not scope['domain'] or not scope['project'] or '*' in (scope['domain'], scope['project']):
        raise CCHError('SCOPE_VIOLATION', 'proposal scope must be an explicit domain/project')
    if not (isinstance(scope['topic'], list) and all(isinstance(t, str) for t in scope['topic'])):
        raise CCHError('INVALID_PROPOSAL', 'scope.topic must be a string list')
    submitter = envelope.get('submitter')
    if not isinstance(submitter, dict) or not isinstance(submitter.get('agent_id'), str):
        raise CCHError('INVALID_PROPOSAL', 'submitter.agent_id required')
    origin = envelope.get('origin')
    if not isinstance(origin, dict) or origin.get('kind') not in {'manual', 'import_job'}:
        raise CCHError('INVALID_PROPOSAL', 'origin.kind must be manual or import_job')
    if origin['kind'] == 'import_job' and not isinstance(origin.get('job_id'), str):
        raise CCHError('INVALID_PROPOSAL', 'import_job origin requires job_id')
    if not (isinstance(envelope.get('reason'), str) and envelope['reason'].strip()):
        raise CCHError('INVALID_PROPOSAL', 'reason required')
    if not isinstance(envelope.get('operations'), list) or not envelope['operations']:
        raise CCHError('INVALID_PROPOSAL', 'operations must be a non-empty list')
    for key in ['evidence_assets', 'candidate_assets', 'risk_flags']:
        if envelope.get(key) is not None and not isinstance(envelope[key], list):
            raise CCHError('INVALID_PROPOSAL', f'{key} must be a list')
    if not isinstance(envelope.get('requested_at'), str):
        raise CCHError('INVALID_PROPOSAL', 'requested_at required')
    return envelope

class _EmptyBase:
    """Stand-in base for validating a bootstrap proposal against an empty store."""
    objects = {}
    evidence = {}

def proposal_hash(envelope):
    return canon_digest(envelope)

def freeze(home, envelope):
    """Validate against the bound revision, then freeze immutably under proposals/."""
    validate_envelope(envelope)
    if envelope['base_revision'] == '0'*40:
        if approved_commit(home) is not None:
            raise CCHError('INVALID_PROPOSAL', 'zero base_revision is only valid for the first publish on an empty store')
        check_applicability(envelope, _EmptyBase())
    else:
        resolve_revision(home, envelope['base_revision'])
        _, base = load_base(home, envelope['base_revision'])
        check_applicability(envelope, base)
    pid, version = envelope['proposal_id'], envelope['proposal_version']
    directory = home.proposals/pid
    existing = sorted(p.name for p in directory.glob('v*')) if directory.exists() else []
    target = directory/f'v{version}'
    phash = proposal_hash(envelope)
    if target.exists():
        stored = json.loads((target/'proposal.json').read_text(encoding='utf-8'))
        if proposal_hash(stored) == phash:
            return {'proposal_id': pid, 'proposal_version': version, 'proposal_hash': phash,
                    'status': read_status(home, pid, version)['status'], 'idempotent': True}
        raise CCHError('INVALID_PROPOSAL', f'{pid} v{version} already frozen with different content')
    if existing:
        latest = max(int(v[1:]) for v in existing)
        if version != latest + 1:
            raise CCHError('INVALID_PROPOSAL', f'next version for {pid} is v{latest + 1}')
        previous = json.loads((directory/f'v{latest}'/'proposal.json').read_text(encoding='utf-8'))
        if proposal_hash(previous) == phash:
            raise CCHError('INVALID_PROPOSAL', 'identical content must reuse the existing version, not a new one')
    target.mkdir(parents=True)
    (target/'proposal.json').write_text(json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    write_status(home, pid, version, 'SUBMITTED')
    return {'proposal_id': pid, 'proposal_version': version, 'proposal_hash': phash,
            'status': 'SUBMITTED', 'idempotent': False}

def _status_path(home, pid, version):
    return home.proposals/pid/f'v{version}'/'status.json'

def write_status(home, pid, version, status, **extra):
    if status not in STATUSES:
        raise CCHError('VALIDATION_ERROR', f'invalid workflow status {status}')
    record = {'status': status, 'updated_at': now_iso(), **extra}
    _status_path(home, pid, version).write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record

def read_status(home, pid, version):
    path = _status_path(home, pid, version)
    if not path.is_file():
        return {'status': 'SUBMITTED'}
    return json.loads(path.read_text(encoding='utf-8'))

def read_proposal(home, pid, version):
    path = home.proposals/pid/f'v{version}'/'proposal.json'
    if not path.is_file():
        raise CCHError('NOT_FOUND', f'proposal {pid} v{version} not found')
    return json.loads(path.read_text(encoding='utf-8'))

def latest_version(home, pid):
    directory = home.proposals/pid
    if not directory.exists():
        raise CCHError('NOT_FOUND', f'proposal {pid} not found')
    return max(int(p.name[1:]) for p in directory.glob('v*'))

def list_proposals(home):
    result = []
    if not home.proposals.exists():
        return result
    for directory in sorted(home.proposals.iterdir()):
        if not directory.is_dir():
            continue
        pid = directory.name
        version = latest_version(home, pid)
        envelope = read_proposal(home, pid, version)
        result.append({'proposal_id': pid, 'latest_version': version,
                       'status': read_status(home, pid, version)['status'],
                       'base_revision': envelope['base_revision'],
                       'proposal_hash': proposal_hash(envelope),
                       'origin': envelope.get('origin', {}).get('kind'),
                       'operations': len(envelope['operations'])})
    return result

def refresh(home, pid):
    """Rebase the latest version onto the current approved revision as a NEW version."""
    version = latest_version(home, pid)
    envelope = read_proposal(home, pid, version)
    current = approved_commit(home)
    if current is None:
        raise CCHError('EMPTY_STORE', 'cannot refresh against an empty store')
    if envelope['base_revision'] == current:
        return {'proposal_id': pid, 'proposal_version': version, 'refreshed': False,
                'reason': 'base_revision already current'}
    updated = json.loads(canon(envelope))
    updated['base_revision'] = current
    updated['proposal_version'] = version + 1
    updated['requested_at'] = now_iso()
    result = freeze(home, updated)
    write_status(home, pid, version + 1, 'SUBMITTED', refreshed_from=f'v{version}')
    old = read_status(home, pid, version)
    if old['status'] in {'READY', 'APPROVED'}:
        write_status(home, pid, version, 'STALE', invalidated_by=f'v{version + 1}')
    return {**result, 'refreshed': True}
