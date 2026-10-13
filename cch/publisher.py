"""Atomic publisher: apply approved proposals to the formal store via CAS.

Publish flow (under the publish lock): idempotency check against formal
transactions, approval validation, STALE_BASE detection, full-tree build and
validation, commit-tree, compare-and-swap ref update, queue bookkeeping last.
The approved ref and the transactions inside it are the recovery source of
truth — queue JSON files are advisory only.
"""
from __future__ import annotations
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
import yaml
from .core import Base, CCHError, digest
from . import store
from .snapshots import materialize
from .store import canon, canon_digest, now_iso

class _EmptyBase:
    objects = {}
    evidence = {}

def object_hash(obj):
    return canon_digest(obj)

def dump_object(obj):
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=True, width=10**9)

def read_tree_objects(tree_dir):
    result = {}
    for p in sorted((tree_dir/'cognitive-base').glob('*.yaml')):
        result[yaml.safe_load(p.read_text(encoding='utf-8'))['id']] = p
    return result

def load_transactions(home, commit):
    """All transaction receipts recorded in the approved history (latest wins)."""
    txs = {}
    for rev in store.history(home):
        for path in store.ls_tree_files(home, rev):
            if path.startswith('transactions/') and path.endswith('.json'):
                try:
                    record = json.loads(store.git(home, 'show', f'{rev}:{path}').stdout)
                except (json.JSONDecodeError, CCHError):
                    continue
                if isinstance(record, dict) and record.get('transaction_id'):
                    txs[record['transaction_id']] = {**record, 'revision': rev}
    return txs

def find_transaction(home, proposal_id, proposal_version, proposal_hash):
    for record in load_transactions(home, store.approved_commit(home)).values():
        if (record.get('proposal_id') == proposal_id and record.get('proposal_version') == proposal_version
                and record.get('proposal_hash') == proposal_hash):
            return record
    return None

def check_applicability(envelope, base):
    """Pure checks of operations against one loaded Base. Raises before any write."""
    ops = envelope.get('operations')
    if not isinstance(ops, list) or not ops:
        raise CCHError('INVALID_PROPOSAL', 'operations must be a non-empty list')
    known = set(base.objects)
    creating = set()
    for op in ops:
        kind = op.get('op')
        oid = op.get('object_id')
        if kind not in {'create', 'replace', 'transition'}:
            raise CCHError('INVALID_PROPOSAL', f'unknown op: {kind}')
        if not isinstance(oid, str):
            raise CCHError('INVALID_PROPOSAL', 'object_id required')
        after = op.get('after')
        if kind in {'create', 'replace'}:
            if not isinstance(after, dict):
                raise CCHError('INVALID_PROPOSAL', f'{oid}: after must be a full object mapping')
            Base.validate_object(after)
            if after['id'] != oid:
                raise CCHError('INVALID_PROPOSAL', f'{oid}: after.id mismatch')
            _check_object_scope(oid, after, envelope)
        if kind == 'create':
            if oid in known or oid in creating:
                raise CCHError('INVALID_PROPOSAL', f'{oid}: create target already exists')
            creating.add(oid)
        else:
            if oid not in base.objects:
                raise CCHError('INVALID_PROPOSAL', f'{oid}: {kind} target not found')
            expected = op.get('expected_object_hash')
            current = object_hash(base.objects[oid])
            if expected != current:
                raise CCHError('SOURCE_CHANGED', f'{oid}: expected_object_hash does not match current object')
            if kind == 'transition':
                after = _apply_transition(base.objects[oid], op)
                Base.validate_object(after)
                _check_object_scope(oid, after, envelope)
    # Cross-candidate and existing relations must all resolve after the batch.
    resolved = known | creating
    for op in ops:
        after = op['after'] if op['op'] in {'create', 'replace'} else _apply_transition(base.objects[op['object_id']], op)
        for rel in after['relations']['supersedes'] + after['relations']['depends_on'] + \
                   after['relations']['conflicts_with'] + after['relations']['derived_from']:
            if rel not in resolved:
                raise CCHError('INVALID_PROPOSAL', f"{op['object_id']}: unresolved relation target {rel}")
    _validate_evidence_assets(envelope, base)

def _check_object_scope(oid, after, envelope):
    pscope = envelope.get('scope') or {}
    domain, project = pscope.get('domain'), pscope.get('project')
    oscope = after['scope']
    if oscope['domain'] == '*' or oscope['project'] == '*':
        if 'GLOBAL_SCOPE' not in (envelope.get('risk_flags') or []):
            raise CCHError('SCOPE_VIOLATION', f'{oid}: global-scope object requires explicit GLOBAL_SCOPE risk flag')
    elif oscope['domain'] != domain or oscope['project'] != project:
        raise CCHError('SCOPE_VIOLATION', f'{oid}: object scope outside proposal scope')

def _apply_transition(current, op):
    after = json.loads(canon(current))  # deep copy through canonical JSON
    changed = False
    if 'status' in op:
        pair = op['status']
        if not (isinstance(pair, dict) and set(pair) == {'from', 'to'}):
            raise CCHError('INVALID_PROPOSAL', 'transition.status must be {from, to}')
        if current['status'] != pair['from']:
            raise CCHError('SOURCE_CHANGED', f"{op['object_id']}: status is {current['status']}, not {pair['from']}")
        after['status'] = pair['to']; changed = True
    for key in ['valid_until', 'valid_from', 'ttl_hours']:
        if key in op:
            pair = op[key]
            if not (isinstance(pair, dict) and set(pair) == {'from', 'to'}):
                raise CCHError('INVALID_PROPOSAL', f'transition.{key} must be {{from, to}}')
            if current['lifecycle'].get(key) != pair['from']:
                raise CCHError('SOURCE_CHANGED', f"{op['object_id']}: lifecycle.{key} is {current['lifecycle'].get(key)}, not {pair['from']}")
            after['lifecycle'][key] = pair['to']; changed = True
    if not changed:
        raise CCHError('INVALID_PROPOSAL', 'transition changes nothing')
    if op.get('rationale'):
        after['rationale'] = op['rationale']
    return after

def _validate_evidence_assets(envelope, base):
    seen = set()
    for asset in envelope.get('evidence_assets') or []:
        eid = asset.get('id')
        if not isinstance(eid, str) or not eid.startswith('EV-'):
            raise CCHError('INVALID_PROPOSAL', 'evidence asset id must start with EV-')
        if eid in seen:
            raise CCHError('INVALID_PROPOSAL', f'duplicate evidence asset {eid}')
        if eid in base.evidence:
            raise CCHError('INVALID_PROPOSAL', f'{eid}: evidence id already exists')
        seen.add(eid)
        if not (isinstance(asset.get('summary'), str) and asset['summary'].strip()):
            raise CCHError('INVALID_PROPOSAL', f'{eid}: summary required')
        if not (isinstance(asset.get('source_text'), str) and asset['source_text'].strip()):
            raise CCHError('INVALID_PROPOSAL', f'{eid}: source_text required')
    created = set()
    for op in envelope['operations']:
        if op['op'] == 'create':
            created.add(op['object_id'])
    final_ids = set(base.objects) | created
    for op in envelope['operations']:
        if op['op'] == 'transition':
            continue
        for ref in op['after']['provenance']['evidence_refs']:
            if ref not in base.evidence and ref not in seen:
                raise CCHError('INVALID_PROPOSAL', f"{op['object_id']}: unknown evidence ref {ref}")

def build_tree(home, envelope, tx_receipt=None, source_dir=None):
    """Materialize the post-state tree of a proposal on top of its base revision."""
    revision = envelope['base_revision']
    tree_dir = Path(tempfile.mkdtemp(prefix='cch-tree-'))
    shutil.rmtree(tree_dir)
    if revision == '0'*40:
        # First publish on an empty store: start from an empty skeleton, no parent snapshot.
        (tree_dir/'cognitive-base').mkdir(parents=True)
        base = _EmptyBase()
    else:
        base_dir = materialize(home, revision)
        shutil.copytree(base_dir, tree_dir, ignore=shutil.ignore_patterns('manifest.json'))
        base = Base(tree_dir)
    check_applicability(envelope, base)
    paths = read_tree_objects(tree_dir)
    for op in envelope['operations']:
        oid = op['object_id']
        if op['op'] == 'transition':
            after = _apply_transition(base.objects[oid], op)
        else:
            after = op['after']
        (tree_dir/f'cognitive-base/{oid}.yaml').write_text(dump_object(after), encoding='utf-8')
    if envelope.get('evidence_assets'):
        (tree_dir/'evidence').mkdir(exist_ok=True)  # git trees never store empty dirs
        index_path = tree_dir/'evidence/evidence-index.yaml'
        index = yaml.safe_load(index_path.read_text(encoding='utf-8')) if index_path.exists() else {}
        if index is None:
            index = {}
        for asset in envelope['evidence_assets']:
            eid = asset['id']
            (tree_dir/f'evidence/{eid}.md').write_text(asset['source_text'], encoding='utf-8')
            index[eid] = {'path': f'evidence/{eid}.md', 'summary': asset['summary']}
            provenance = asset.get('source')
            if provenance:
                record = {'evidence_id': eid, 'proposal_id': envelope['proposal_id'],
                          'created_at': envelope.get('requested_at'), **provenance}
                pdir = tree_dir/'provenance'
                pdir.mkdir(exist_ok=True)
                (pdir/f'{eid}.json').write_text(canon(record) + '\n', encoding='utf-8')
        index_path.write_text(yaml.safe_dump(index, allow_unicode=True, sort_keys=True), encoding='utf-8')
    if tx_receipt:
        tdir = tree_dir/'transactions'
        tdir.mkdir(exist_ok=True)
        (tdir/f"{tx_receipt['transaction_id']}.json").write_text(canon(tx_receipt) + '\n', encoding='utf-8')
    Base(tree_dir)  # full-schema, relation, evidence and path validation of the final tree
    return tree_dir

def apply(home, envelope, approval, reviewer_bundle_hash=None):
    """Publish an approved proposal. Returns ALREADY_APPLIED or APPLIED records."""
    proposal_hash = canon_digest(envelope)
    with store.publish_lock(home):
        record = find_transaction(home, envelope['proposal_id'], envelope.get('proposal_version'), proposal_hash)
        if record:
            return {'status': 'ALREADY_APPLIED', **{k: record[k] for k in
                    ['revision', 'transaction_id', 'proposal_id', 'proposal_version', 'proposal_hash']}}
        if not isinstance(approval, dict) or approval.get('action') != 'approve':
            raise CCHError('UNAPPROVED', 'apply requires a valid approval record')
        if approval.get('proposal_hash') != proposal_hash:
            raise CCHError('APPROVAL_MISMATCH', 'approval binds a different proposal hash')
        if reviewer_bundle_hash and approval.get('review_bundle_hash') != reviewer_bundle_hash:
            raise CCHError('APPROVAL_MISMATCH', 'approval binds a different review bundle')
        current = store.approved_commit(home)
        if envelope['base_revision'] == '0'*40:
            if current is not None:
                raise CCHError('STALE_BASE', 'store is no longer empty; refresh the proposal against the current revision')
        elif current != envelope['base_revision']:
            raise CCHError('STALE_BASE', f"proposal targets {envelope['base_revision'][:12]}, approved is {(current or 'EMPTY')[:12]}")
        parent = current
        tx_id = 'TX-' + uuid.uuid4().hex[:12]
        receipt = {'transaction_id': tx_id, 'proposal_id': envelope['proposal_id'],
                   'proposal_version': envelope.get('proposal_version'), 'proposal_hash': proposal_hash,
                   'review_bundle_hash': approval.get('review_bundle_hash'),
                   'reviewer': approval.get('reviewer'), 'applied_at': now_iso(),
                   'operations': [{'op': op['op'], 'object_id': op['object_id']} for op in envelope['operations']]}
        tree_dir = build_tree(home, envelope, tx_receipt=receipt)
        try:
            tree = store.write_tree_from_dir(home, tree_dir)
            commit = store.make_commit(home, tree, parent, f'CCH {tx_id} {envelope["proposal_id"]}')
            store.cas_update_ref(home, commit, current)
        finally:
            shutil.rmtree(tree_dir, ignore_errors=True)
        return {'status': 'APPLIED', 'revision': commit, 'transaction_id': tx_id,
                'proposal_id': envelope['proposal_id'], 'proposal_version': envelope.get('proposal_version'),
                'proposal_hash': proposal_hash}
