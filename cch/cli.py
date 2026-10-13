"""CLI surface: M1 read-only compilation plus the v0.2 store/publish/ingest groups.

--base keeps the original read-only commands byte-compatible. --home targets a
cch-home working directory with store/agent/source/ingest/proposal/review/apply.
Machine-readable output is JSON with stable error codes and exit 2 on failure.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from .core import (Base, CCHError, compile_context, config_load, instant, governance, model_view, retrieve)

def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.cch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))

def build_parser():
    parser = argparse.ArgumentParser(prog='cch', description='Cognitive Context Hub compiler and controlled publisher')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--base', type=Path, help='read-only cognitive base directory (M1 mode)')
    mode.add_argument('--home', type=Path, help='cch-home working directory (v0.2 mode)')
    parser.add_argument('--config', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    # M1 read-only commands (unchanged behavior)
    sub.add_parser('validate')
    for command in ['context', 'search', 'inspect', 'evidence']:
        p = sub.add_parser(command)
        p.add_argument('--domain', required=True)
        p.add_argument('--project', required=True)
        p.add_argument('--topic', action='append', default=[])
        p.add_argument('--as-of', required=True, help='Explicit ISO time/date; date means midnight UTC')
        if command in ['context', 'search']:
            p.add_argument('--query', required=True)
        else:
            p.add_argument('id')
        if command == 'context':
            p.add_argument('--budget', type=int, default=16000)
            p.add_argument('--evidence', choices=['auto', 'none', 'summary', 'full'], default='auto')
            p.add_argument('--output', type=Path)
            p.add_argument('--audit', type=Path)
    # v0.2 command groups
    store_p = sub.add_parser('store'); store_sub = store_p.add_subparsers(dest='sub', required=True)
    store_sub.add_parser('init')
    store_sub.add_parser('status')
    p = store_sub.add_parser('export'); p.add_argument('--to', required=True, type=Path)
    p = store_sub.add_parser('restore'); p.add_argument('--from', dest='frm', required=True, type=Path)

    agent_p = sub.add_parser('agent'); agent_sub = agent_p.add_subparsers(dest='sub', required=True)
    p = agent_sub.add_parser('context')
    p.add_argument('--domain', required=True); p.add_argument('--project', required=True)
    p.add_argument('--topic', action='append', default=[])
    p.add_argument('--query', required=True); p.add_argument('--budget', type=int, default=16000)
    p.add_argument('--evidence', choices=['auto', 'none', 'summary', 'full'], default='auto')
    p.add_argument('--as-of', help='default: current time, taken once and recorded in the audit')
    p.add_argument('--revision', help='explicit approved commit for historical reproduction')
    p = agent_sub.add_parser('check')
    p.add_argument('--receipt', required=True, help='receipt.json path or run id')
    p.add_argument('--domain'); p.add_argument('--project')
    p = agent_sub.add_parser('pack')
    p.add_argument('--run-dir', required=True, type=Path); p.add_argument('--to', required=True, type=Path)

    source_p = sub.add_parser('source'); source_sub = source_p.add_subparsers(dest='sub', required=True)
    p = source_sub.add_parser('add'); p.add_argument('file', type=Path)
    p.add_argument('--domain', required=True); p.add_argument('--project', required=True)
    p.add_argument('--topic', action='append', default=[])
    p.add_argument('--declared-date', help='fact date declared by the source itself; NOT the import time')
    p = source_sub.add_parser('inspect'); p.add_argument('id')

    ingest_p = sub.add_parser('ingest'); ingest_sub = ingest_p.add_subparsers(dest='sub', required=True)
    p = ingest_sub.add_parser('prepare'); p.add_argument('source_id')
    p = ingest_sub.add_parser('export-job'); p.add_argument('source_id'); p.add_argument('--chunk', type=int, default=0)
    p = ingest_sub.add_parser('import-result'); p.add_argument('job_id'); p.add_argument('--result', type=Path)
    p = ingest_sub.add_parser('status'); p.add_argument('source_id', nargs='?')

    prop_p = sub.add_parser('proposal'); prop_sub = prop_p.add_subparsers(dest='sub', required=True)
    p = prop_sub.add_parser('create')
    p.add_argument('--file', type=Path, help='manual submission envelope JSON (untrusted input)')
    p.add_argument('--from-job')
    p.add_argument('--candidate', action='append', default=[])
    p.add_argument('--id'); p.add_argument('--reason'); p.add_argument('--agent-id', default='local-agent')
    prop_sub.add_parser('list')
    p = prop_sub.add_parser('show'); p.add_argument('id'); p.add_argument('--version', type=int)
    p = prop_sub.add_parser('refresh'); p.add_argument('id')

    rev_p = sub.add_parser('review'); rev_sub = rev_p.add_subparsers(dest='sub', required=True)
    p = rev_sub.add_parser('render'); p.add_argument('id'); p.add_argument('--version', type=int)
    p = rev_sub.add_parser('approve'); p.add_argument('id'); p.add_argument('--version', type=int)
    p.add_argument('--reviewer', required=True)
    p.add_argument('--confirm-truth', action='store_true',
                   help='record that the user separately confirmed truth; does NOT mutate object fields')
    p = rev_sub.add_parser('reject'); p.add_argument('id'); p.add_argument('--version', type=int)
    p.add_argument('--reviewer', required=True); p.add_argument('--reason', required=True)

    p = sub.add_parser('apply'); p.add_argument('id'); p.add_argument('--version', type=int)
    from .authoring import cli as authoring_cli
    authoring_cli.add_parser(sub)
    return parser

def run_home(args):
    from . import agent_io, ingest, proposals, publisher, reviews, sources, store
    if args.command == "author":
        from .authoring import cli as authoring_cli
        home = store.Home(args.home)
        return emit(authoring_cli.run(args, home))
    home = store.Home(args.home)
    config = args.config
    if args.command == 'store':
        if args.sub == 'init':
            return emit(store.store_init(home))
        if args.sub == 'status':
            return emit(store.store_status(home))
        if args.sub == 'export':
            return emit(store.store_export(home, args.to))
        if args.sub == 'restore':
            return emit(store.store_restore(home, args.frm))
    if args.command == 'agent':
        if args.sub == 'context':
            as_of = instant(args.as_of) if args.as_of else None
            return emit(agent_io.agent_context(home, args.query, args.domain, args.project, args.topic,
                                               as_of=as_of, budget=args.budget, config_path=config,
                                               evidence=args.evidence, revision=args.revision))
        if args.sub == 'check':
            target = Path(args.receipt)
            if not target.is_file() and (home.runs/args.receipt/'receipt.json').is_file():
                target = home.runs/args.receipt/'receipt.json'
            return emit(agent_io.agent_check(home, target, args.domain, args.project, config_path=config))
        if args.sub == 'pack':
            return emit(agent_io.export_pack(home, args.run_dir, args.to))
    if args.command == 'source':
        if args.sub == 'add':
            return emit(sources.register(home, args.file, args.domain, args.project, args.topic, args.declared_date))
        if args.sub == 'inspect':
            return emit(sources.inspect(home, args.id))
    if args.command == 'ingest':
        if args.sub == 'prepare':
            return emit(ingest.prepare(home, args.source_id))
        if args.sub == 'export-job':
            return emit(ingest.export_job(home, args.source_id, args.chunk, config))
        if args.sub == 'import-result':
            result = args.result or home.jobs/args.job_id/'result.json'
            return emit(ingest.import_result(home, args.job_id, result, config))
        if args.sub == 'status':
            return emit(ingest.status(home, args.source_id) if args.source_id else {'sources': ingest.status(home)})
    if args.command == 'proposal':
        if args.sub == 'create':
            if args.file:
                envelope = json.loads(Path(args.file).read_text(encoding='utf-8'))
                return emit(proposals.freeze(home, envelope))
            if args.from_job:
                return emit(ingest.build_proposal(home, args.from_job, args.candidate,
                                                  proposal_id=args.id, reason=args.reason, agent_id=args.agent_id))
            raise CCHError('VALIDATION_ERROR', 'proposal create requires --file or --from-job')
        if args.sub == 'list':
            return emit(proposals.list_proposals(home))
        if args.sub == 'show':
            version = args.version or proposals.latest_version(home, args.id)
            envelope = proposals.read_proposal(home, args.id, version)
            return emit({'proposal': envelope, 'status': proposals.read_status(home, args.id, version),
                         'proposal_hash': proposals.proposal_hash(envelope)})
        if args.sub == 'refresh':
            return emit(proposals.refresh(home, args.id))
    if args.command == 'review':
        from .proposals import latest_version
        version = args.version or latest_version(home, args.id)
        if args.sub == 'render':
            return emit(reviews.render_bundle(home, args.id, version))
        if args.sub == 'approve':
            return emit(reviews.approve(home, args.id, version, args.reviewer, confirm_confidence=args.confirm_truth))
        if args.sub == 'reject':
            return emit(reviews.reject(home, args.id, version, args.reviewer, args.reason))
    if args.command == 'apply':
        from .proposals import latest_version
        from .reviews import load_approval, load_bundle_hash
        version = args.version or latest_version(home, args.id)
        envelope = proposals.read_proposal(home, args.id, version)
        status = proposals.read_status(home, args.id, version)
        if status['status'] == 'APPLIED':
            record = publisher.find_transaction(home, args.id, version, proposals.proposal_hash(envelope))
            if record:
                return emit({'status': 'ALREADY_APPLIED', **{k: record[k] for k in
                        ['revision', 'transaction_id', 'proposal_id', 'proposal_version', 'proposal_hash']}})
        approval = load_approval(home, args.id, version)
        bundle_hash = load_bundle_hash(home, args.id, version)
        result = publisher.apply(home, envelope, approval, bundle_hash)
        if result['status'] == 'APPLIED':
            proposals.write_status(home, args.id, version, 'APPLIED',
                                   transaction_id=result.get('transaction_id'), revision=result.get('revision'))
        return emit(result)
    raise CCHError('VALIDATION_ERROR', f'unknown command {args.command} {getattr(args, "sub", "")}')

def main():
    args = build_parser().parse_args()
    try:
        if args.home:
            return run_home(args)
        base = Base(args.base)
        cfg = config_load(args.config)
        if args.command == 'validate':
            return emit({'valid': True, 'objects': len(base.objects), 'evidence': len(base.evidence),
                         'source_hash': base.source_hash})
        if not args.domain or not args.project or '*' in (args.domain, args.project):
            raise CCHError('VALIDATION_ERROR', 'Explicit scope required')
        as_of = instant(args.as_of)
        states = governance(base, args.domain, args.project, args.topic, as_of)
        if args.command == 'context':
            destinations = [p.resolve() for p in [args.output, args.audit] if p]
            if any(p.is_relative_to(base.root) for p in destinations):
                raise CCHError('WRITE_PROTECTED', 'Outputs must be outside Cognitive Base root')
            if len(destinations) != len(set(destinations)):
                raise CCHError('OUTPUT_CONFLICT', 'Context and audit require different paths')
            context, audit = compile_context(base, args.query, args.domain, args.project, args.topic,
                                             as_of, args.budget, cfg, args.evidence)
            audit_text = json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
            # Every call emits an audit: stderr by default, or an explicit file.
            if args.audit:
                atomic_write(args.audit, audit_text)
            else:
                sys.stderr.write(audit_text)
            if args.output:
                atomic_write(args.output, context)
            else:
                sys.stdout.write(context)
        elif args.command == 'search':
            emit(retrieve(args.query, base, states, cfg))
        else:
            if args.id not in states:
                raise CCHError('NOT_FOUND', 'Object not found in requested scope')
            if args.command == 'inspect':
                emit(model_view(args.id, base, states))
            else:
                refs = base.objects[args.id]['provenance']['evidence_refs']
                emit({'object': args.id, 'governance': states[args.id],
                      'evidence': [{'id': r, 'summary': base.evidence[r]['summary'],
                                    'source_text': base.evidence_text[r]} for r in refs]})
    except (CCHError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
