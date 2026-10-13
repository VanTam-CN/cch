"""Human review bundles and approval records bound to exact hashes.

A bundle shows full old/new object content, evidence quotes with source
locators and hashes, uncertainty and conflict notes, and the downstream
objects that would enter NEEDS_REVIEW. Approvals bind proposal_hash,
review_bundle_hash and base_revision; they never mutate object fields.
"""
from __future__ import annotations
import json
import shutil
from pathlib import Path
from .core import CCHError, governance, instant
from .publisher import build_tree, object_hash, read_tree_objects
from .snapshots import load_base
from .store import Home, approved_commit, canon_digest, now_iso

def _describe_change(op, before, after):
    lines = [f"### {op['op']} `{op['object_id']}`"]
    if op['op'] != 'create' and before is not None:
        lines.append(f"- 旧 status/confidence/authority: {before['status']} / {before['confidence']} / {before['authority']}")
        lines.append(f"- 旧 object hash: `{object_hash(before)}`")
    lines.append(f"- 新 status/confidence/authority: {after['status']} / {after['confidence']} / {after['authority']}")
    b_life = (before or {}).get('lifecycle', {}); a_life = after['lifecycle']
    lines.append(f"- 有效期: {b_life.get('valid_from')}~{b_life.get('valid_until')} (ttl {b_life.get('ttl_hours')}) → "
                 f"{a_life.get('valid_from')}~{a_life.get('valid_until')} (ttl {a_life.get('ttl_hours')})")
    if op['op'] == 'create' or (before or {}).get('content') != after.get('content'):
        lines.append(f"- 新 content: {after['content']}")
    elif before:
        lines.append(f"- content: （未变化）{before['content']}")
    if after.get('rationale'):
        lines.append(f"- rationale: {after['rationale']}")
    for rel in ['supersedes', 'depends_on', 'conflicts_with', 'derived_from']:
        old = (before or {}).get('relations', {}).get(rel, [])
        if old != after['relations'][rel]:
            lines.append(f"- 关系 {rel}: {old} → {after['relations'][rel]}")
    refs_new = after['provenance']['evidence_refs']
    if refs_new:
        lines.append(f"- evidence_refs: {refs_new}")
    if after['authority'] == 'AI_INFERRED' or after['confidence'] == 'TENTATIVE':
        lines.append('- ⚠ 不确定性保留：本对象以 AI_INFERRED/TENTATIVE 入库；批准保存不等于确认属实。')
    return '\n'.join(lines)

def render_bundle(home, pid, version):
    from .proposals import read_proposal, read_status, write_status
    envelope = read_proposal(home, pid, version)
    if envelope['base_revision'] == '0'*40:
        before_base = None
    else:
        _, before_base = load_base(home, envelope['base_revision'])
    tree_dir = build_tree(home, envelope)
    try:
        from .core import Base
        after_base = Base(tree_dir)
    finally:
        shutil.rmtree(tree_dir, ignore_errors=True)
    scope = envelope['scope']
    as_of = instant(envelope.get('requested_at') or now_iso())
    pre_states = governance(before_base, scope['domain'], scope['project'], scope['topic'], as_of) if before_base else {}
    post_states = governance(after_base, scope['domain'], scope['project'], scope['topic'], as_of)
    changed_ids = [op['object_id'] for op in envelope['operations']]
    downstream = sorted(oid for oid, s in post_states.items()
                        if oid not in changed_ids and pre_states.get(oid, {}).get('effective_status') != s['effective_status'])
    lines = [f'# Review Bundle — {pid} v{version}', '',
             f"- 理由：{envelope['reason']}",
             f"- 基线 revision：`{envelope['base_revision'][:12]}`（当前 approved：`{(approved_commit(home) or 'EMPTY')[:12]}`）",
             f"- 作用域：{scope['domain']} / {scope['project']}" + (f" topic={scope['topic']}" if scope['topic'] else ''),
             f"- 来源：{envelope.get('origin', {}).get('kind')}" +
                 (f" job={envelope['origin'].get('job_id')}" if envelope.get('origin', {}).get('job_id') else ''),
             f"- 提交者：{envelope.get('submitter', {}).get('agent_id')}",
             f"- 提案 hash：`{canon_digest(envelope)}`", '']
    if envelope.get('risk_flags'):
        lines.append(f"**风险标记**: {', '.join(envelope['risk_flags'])}")
        lines.append('')
    for op in envelope['operations']:
        lines.append(_describe_change(op, (before_base.objects if before_base else {}).get(op['object_id']),
                                      after_base.objects.get(op['object_id'])))
        lines.append('')
    assets = envelope.get('evidence_assets') or []
    if assets:
        lines.append('## 证据资产')
        for asset in assets:
            src = asset.get('source') or {}
            lines.append(f"- **{asset['id']}**: {asset['summary']}")
            lines.append(f"  - 原文：> {asset['source_text']}")
            if src:
                lines.append(f"  - 来源：{src.get('source_name')} block={src.get('block_id')} "
                             f"spans={src.get('spans')} sha256=`{str(src.get('source_sha256'))[:16]}…`")
        lines.append('')
    if downstream:
        lines.append('## 将进入复核的下游对象（派生状态，不自动写入）')
        for oid in downstream:
            lines.append(f"- `{oid}`: {pre_states[oid]['effective_status']} → {post_states[oid]['effective_status']}"
                         f"（issues: {post_states[oid]['issues']}）")
        lines.append('')
    lines.append('## 统计')
    lines.append(f"- create {sum(1 for o in envelope['operations'] if o['op'] == 'create')}, "
                 f"replace {sum(1 for o in envelope['operations'] if o['op'] == 'replace')}, "
                 f"transition {sum(1 for o in envelope['operations'] if o['op'] == 'transition')}, "
                 f"evidence {len(assets)}")
    text = '\n'.join(lines) + '\n'
    directory = home.reviews/pid/f'v{version}'
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'bundle.md').write_text(text, encoding='utf-8')
    bundle_hash = canon_digest({'proposal_id': pid, 'proposal_version': version,
                                'proposal_hash': canon_digest(envelope), 'bundle': text})
    (directory/'bundle.hash').write_text(bundle_hash + '\n', encoding='utf-8')
    status = read_status(home, pid, version)
    if status['status'] == 'SUBMITTED':
        write_status(home, pid, version, 'READY', bundle_hash=bundle_hash)
    return {'bundle_path': str(directory/'bundle.md'), 'bundle_hash': bundle_hash,
            'downstream': downstream, 'review_command_needed': True}

def _review_dir(home, pid, version):
    directory = home.reviews/pid/f'v{version}'
    if not (directory/'bundle.md').is_file():
        raise CCHError('NOT_FOUND', f'no review bundle rendered for {pid} v{version}; run review render first')
    return directory

def approve(home, pid, version, reviewer, confirm_confidence=False):
    """Record approval of the exact rendered bundle. Not identity authentication."""
    from .proposals import read_proposal, read_status, write_status
    if not reviewer or not isinstance(reviewer, str):
        raise CCHError('VALIDATION_ERROR', 'reviewer name required (recorded, not authenticated)')
    directory = _review_dir(home, pid, version)
    envelope = read_proposal(home, pid, version)
    status = read_status(home, pid, version)
    if status['status'] == 'APPLIED':
        raise CCHError('ALREADY_APPLIED', 'proposal version already applied')
    approval = {'action': 'approve', 'reviewer': reviewer, 'approved_at': now_iso(),
                'proposal_id': pid, 'proposal_version': version,
                'proposal_hash': canon_digest(envelope),
                'review_bundle_hash': (directory/'bundle.hash').read_text(encoding='utf-8').strip(),
                'base_revision': envelope['base_revision'],
                'confirmed_truth': bool(confirm_confidence),
                'note': 'reviewer string is a record, not authentication; cooperative mode'}
    (directory/'approval.json').write_text(json.dumps(approval, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    write_status(home, pid, version, 'APPROVED', reviewer=reviewer)
    return approval

def reject(home, pid, version, reviewer, reason):
    from .proposals import read_status, write_status
    if not reason or not isinstance(reason, str):
        raise CCHError('VALIDATION_ERROR', 'rejection reason required')
    directory = _review_dir(home, pid, version)
    record = {'action': 'reject', 'reviewer': reviewer, 'rejected_at': now_iso(),
              'proposal_id': pid, 'proposal_version': version, 'reason': reason}
    (directory/'rejection.json').write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    write_status(home, pid, version, 'REJECTED', reviewer=reviewer, reason=reason)
    return record

def load_approval(home, pid, version):
    path = home.reviews/pid/f'v{version}'/'approval.json'
    if not path.is_file():
        raise CCHError('UNAPPROVED', f'{pid} v{version} has no approval record')
    return json.loads(path.read_text(encoding='utf-8'))

def load_bundle_hash(home, pid, version):
    directory = _review_dir(home, pid, version)
    return (directory/'bundle.hash').read_text(encoding='utf-8').strip()
