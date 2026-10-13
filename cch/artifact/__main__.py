"""CLI entry: python -m cch.artifact <subcommand> (asset intelligence; no Cognitive-Base writes)."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog='cch-asset', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    scan_p = sub.add_parser('scan', help='scan an authorized directory for reusable visual assets')
    scan_p.add_argument('directory', type=Path)
    scan_p.add_argument('--home', required=True, type=Path)
    scan_p.add_argument('--domain', default='')
    scan_p.add_argument('--project', default='')
    scan_p.add_argument('--topics', default='', help='comma separated topics')
    scan_p.add_argument('--wps-render', action='store_true',
                        help='render PPTX/DOCX full pages via WPS cloud (kdocs-cli) into page-level candidates')

    p = sub.add_parser('stats', help='index summary'); p.add_argument('--home', required=True, type=Path)
    p = sub.add_parser('list', help='list assets (gold-labeling aid: id, sha256, locator, status)')
    p.add_argument('--home', required=True, type=Path)
    p.add_argument('--state', default='all', choices=['pending', 'indexed', 'all'])
    p = sub.add_parser('inspect', help='show one asset record'); p.add_argument('asset_id'); p.add_argument('--home', required=True, type=Path)
    p = sub.add_parser('supersede', help='mark an asset superseded (human decision)')
    p.add_argument('asset_id'); p.add_argument('--by', dest='by', default=None); p.add_argument('--home', required=True, type=Path)

    p = sub.add_parser('export-job', help='freeze pending assets into a VLM exchange job')
    p.add_argument('--home', required=True, type=Path)
    p.add_argument('--assets', default='', help='comma separated asset ids; default all pending')
    p.add_argument('--limit', type=int, default=20)
    p = sub.add_parser('import-result', help='validate a returned VLM result into the index')
    p.add_argument('job_id'); p.add_argument('--result', required=True, type=Path); p.add_argument('--home', required=True, type=Path)

    p = sub.add_parser('export-enrich-job', help='freeze indexed assets into an L2 concepts enrichment job (/2)')
    p.add_argument('--home', required=True, type=Path)
    p.add_argument('--assets', default='', help='comma separated asset ids; default all indexed')
    p.add_argument('--limit', type=int, default=99)
    p = sub.add_parser('import-enrich-result', help='validate a returned /2 concepts result into the index')
    p.add_argument('job_id'); p.add_argument('--result', required=True, type=Path); p.add_argument('--home', required=True, type=Path)
    p = sub.add_parser('export-alias-job', help='freeze indexed assets into an L3 aliases enrichment job (/3)')
    p.add_argument('--home', required=True, type=Path)
    p.add_argument('--assets', default='', help='comma separated asset ids; default all indexed')
    p.add_argument('--limit', type=int, default=99)
    p = sub.add_parser('import-alias-result', help='validate a returned /3 aliases result into the index')
    p.add_argument('job_id'); p.add_argument('--result', required=True, type=Path); p.add_argument('--home', required=True, type=Path)

    p = sub.add_parser('pack', help='materialize a job into a self-contained pack (dir or .zip)')
    p.add_argument('job_id'); p.add_argument('--out', required=True, type=Path); p.add_argument('--home', required=True, type=Path)

    p = sub.add_parser('search', help='task-driven deterministic asset retrieval')
    p.add_argument('query'); p.add_argument('--home', required=True, type=Path)
    p.add_argument('--domain', default=None); p.add_argument('--project', default=None)
    p.add_argument('--topics', default=''); p.add_argument('--limit', type=int, default=10)
    p.add_argument('--expanded-scope', action='store_true',
                   help='keep cross-project assets instead of excluding scope mismatches')
    p.add_argument('--include-superseded', action='store_true',
                   help='return superseded assets too (history/evolution comparison)')

    args = parser.parse_args()
    try:
        if args.command == 'scan':
            from .scan import scan_directory
            from .index import connect, register_candidate
            cands = scan_directory(args.directory)
            renderers = []
            if args.wps_render:
                import hashlib as _hashlib
                import cch.artifact.render as render_mod
                render_document_cached = render_mod.render_document_cached
                CLI = render_mod.CLI
                from .index import connect as _connect
                _probe = _connect(args.home)
                _probe.close()
                RENDER_EXTS = {'.pptx', '.ppt', '.docx', '.doc'}
                doc_paths = sorted(str(p) for p in Path(args.directory).rglob('*')
                                   if p.is_file() and not p.is_symlink()
                                   and p.suffix.lower() in RENDER_EXTS)
                if doc_paths:
                    import subprocess as _sp
                    _auth = _sp.run([CLI, 'auth', 'status'], capture_output=True, text=True)
                    if _auth.returncode != 0 or '"authenticated": true' not in _auth.stdout.replace("'", '"'):
                        print(json.dumps({'error': 'WPS_RENDER_UNAVAILABLE',
                                          'message': 'kdocs-cli not authenticated; run: kdocs-cli auth login'},
                                         ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
                for doc_name in doc_paths:
                    doc_path = Path(doc_name)
                    doc_sha = _hashlib.sha256(doc_path.read_bytes()).hexdigest()
                    cache_dir = args.home / 'artifact' / 'render-cache' / doc_sha
                    try:
                        cands.extend(render_document_cached(doc_path, cache_dir))
                        renderers.append(render_mod.LAST_RENDERER)
                    except Exception as exc:
                        print(json.dumps({'error': 'RENDER_FAILED',
                                          'message': f'{doc_name}: {str(exc)[:300]}'},
                                         ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
            conn = connect(args.home)
            scope = {'domain': args.domain, 'project': args.project,
                     'topics': [t.strip() for t in args.topics.split(',') if t.strip()]}
            registered = []
            import hashlib
            for c in cands:
                doc_path = Path(c.source_path)
                doc_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()
                registered.append({'asset_id': register_candidate(conn, c, doc_hash, _mtime(doc_path), scope),
                                   'kind': c.kind, 'sha256': c.sha256})
            emit({'scanned': len(cands), 'registered': registered,
                  'renderers': renderers})
        elif args.command == 'stats':
            from .index import stats
            emit(stats(args.home))
        elif args.command == 'list':
            from .index import list_assets
            emit(list_assets(args.home, args.state))
        elif args.command == 'inspect':
            from .index import get_asset
            emit(get_asset(args.home, args.asset_id))
        elif args.command == 'supersede':
            from .index import mark_superseded
            emit(mark_superseded(args.home, args.asset_id, args.by))
        elif args.command == 'export-job':
            from .exchange import export_job
            ids = [a.strip() for a in args.assets.split(',') if a.strip()]
            emit(export_job(args.home, ids or None, args.limit))
        elif args.command == 'import-result':
            from .exchange import import_result
            emit(import_result(args.home, args.job_id, args.result))
        elif args.command == 'export-enrich-job':
            from .enrich import export_enrich_job
            ids = [a.strip() for a in args.assets.split(',') if a.strip()]
            emit(export_enrich_job(args.home, ids or None, args.limit))
        elif args.command == 'import-enrich-result':
            from .enrich import import_enrich_result
            emit(import_enrich_result(args.home, args.job_id, args.result))
        elif args.command == 'export-alias-job':
            from .alias_enrich import export_alias_job
            ids = [a.strip() for a in args.assets.split(',') if a.strip()]
            emit(export_alias_job(args.home, ids or None, args.limit))
        elif args.command == 'import-alias-result':
            from .alias_enrich import import_alias_result
            emit(import_alias_result(args.home, args.job_id, args.result))
        elif args.command == 'pack':
            from .pack import pack_job
            emit(pack_job(args.home, args.job_id, args.out))
        elif args.command == 'search':
            from .index import search
            topics = [t.strip() for t in args.topics.split(',') if t.strip()]
            emit(search(args.home, args.query, args.domain, args.project, topics,
                        args.limit, strict_scope=not args.expanded_scope,
                        include_superseded=args.include_superseded))
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str) and ': ' in code:
            err_code, message = code.split(': ', 1)
            print(json.dumps({'error': err_code, 'message': message}, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(2)
        raise


def _mtime(path: Path) -> str:
    from datetime import datetime, timezone
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='seconds')


if __name__ == '__main__':
    main()
