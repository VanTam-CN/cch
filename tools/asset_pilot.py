#!/usr/bin/env python3
"""Gold Pilot evaluator for M2 Artifact Intelligence (proposal section 11).

Runs the frozen gold task set against an indexed cch-home and checks the
M2 gates mechanically:

  1. traceability = 100%: every recommended asset resolves to a real file
     with a non-empty sha256;
  2. stale leakage = 0: no superseded asset is recommended without the
     SUPERSEDED flag;
  3. determinism: every query executed twice must return identical ranking;
  4. recall@k: reported per task; enforced against --min-recall when given
     (freeze the threshold only after the gold set exists, per proposal);
  5. no Cognitive-Base writes: store.git HEAD is hashed before and after.

Usage:
  python tools/asset_pilot.py --home ./cch-home --gold gold.json \
      [--min-recall 0.6] [--top-k 10] [--report output/pilot-report.json]

Gold format (JSON):
  {"tasks": [{"query": "...", "domain": "...", "project": "...",
              "topics": ["..."],
              "relevant_sha256": ["<sha256>", ...],
              "superseded_sha256": ["<sha256>", ...]}]}
"""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cch.artifact.index import search  # noqa: E402


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _store_head(home: Path) -> str | None:
    head = home/'store.git'/'HEAD'
    refs = home/'store.git'/'refs'/'heads'/'cch-approved'
    if not head.is_file():
        return None
    resolved = head.read_text('utf-8').strip()
    if resolved.startswith('ref:') and (home/'store.git'/resolved[4:].strip()).is_file():
        return (home/'store.git'/resolved[4:].strip()).read_text('utf-8').strip()
    if refs.is_file():
        return refs.read_text('utf-8').strip()
    return resolved


def evaluate_task(task: dict, home: Path, top_k: int) -> dict:
    """Run one gold task twice; score recall, leakage, traceability."""
    # Pilot semantics: auto-selection view. Superseded assets are excluded
    # by default (use --include-superseded on cch-asset search for the
    # human-review view); a superseded asset reaching Top-K here is a
    # direct auto-selection leak, not a flagged recommendation.
    run1 = search(home, task['query'], task.get('domain'), task.get('project'),
                  task.get('topics') or [], limit=top_k, strict_scope=True,
                  include_superseded=False)
    run2 = search(home, task['query'], task.get('domain'), task.get('project'),
                  task.get('topics') or [], limit=top_k, strict_scope=True,
                  include_superseded=False)
    deterministic = run1['results'] == run2['results']

    relevant = set(task.get('relevant_sha256') or [])
    superseded = set(task.get('superseded_sha256') or [])
    hits = [r for r in run1['results'] if r['asset']['sha256'] in relevant]
    recall = len(hits)/len(relevant) if relevant else None

    leakage = []
    untraceable = []
    for r in run1['results']:
        asset = r['asset']
        if asset['sha256'] in superseded:
            leakage.append(asset['asset_id'])
        source = Path(asset['source_file'])
        if not asset['sha256'] or not source.is_file():
            untraceable.append(asset['asset_id'])

    gold_missing = sorted(relevant -
                          {r['asset']['sha256'] for r in run1['results']})
    return {'query': task['query'], 'scope': {'domain': task.get('domain'),
            'project': task.get('project'), 'topics': task.get('topics') or []},
            'recall_at_k': recall, 'hits': [h['asset']['asset_id'] for h in hits],
            'gold_count': len(relevant),
            'gold_not_retrieved': len(gold_missing),
            'superseded_leakage': leakage,
            'untraceable_assets': untraceable,
            'deterministic': deterministic,
            'top_ids': [r['asset']['asset_id'] for r in run1['results']]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--gold', required=True, type=Path)
    parser.add_argument('--top-k', type=int, default=10)
    parser.add_argument('--min-recall', type=float, default=None,
                        help='enforce mean recall threshold; omit to only report')
    parser.add_argument('--report', type=Path, default=None)
    args = parser.parse_args()

    gold_path = Path(args.gold)
    gold = json.loads(gold_path.read_text('utf-8'))
    tasks = gold.get('tasks') or []
    if not tasks:
        print(json.dumps({'error': 'VALIDATION_ERROR',
                          'message': 'gold file has no tasks'}, ensure_ascii=False))
        return 2

    head_before = _store_head(args.home)
    results = [evaluate_task(t, args.home, args.top_k) for t in tasks]
    head_after = _store_head(args.home)

    recalls = [r['recall_at_k'] for r in results if r['recall_at_k'] is not None]
    mean_recall = sum(recalls)/len(recalls) if recalls else None
    leakage = sum(len(r['superseded_leakage']) for r in results)
    untraceable = sum(len(r['untraceable_assets']) for r in results)
    non_deterministic = [r['query'] for r in results if not r['deterministic']]

    gates = {
        'traceability_100': untraceable == 0,
        'unflagged_stale_leakage_zero': leakage == 0,
        'deterministic_ranking': not non_deterministic,
        'no_cognitive_base_writes': head_before == head_after,
    }
    if args.min_recall is not None:
        gates['recall_threshold'] = (mean_recall is not None
                                     and mean_recall >= args.min_recall)
    passed = all(gates.values())

    report = {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'gold_file': str(gold_path), 'gold_sha256': _sha256_file(gold_path),
              'top_k': args.top_k, 'min_recall_threshold': args.min_recall,
              'mean_recall_at_k': mean_recall,
              'gates': gates, 'passed': passed,
              'totals': {'tasks': len(tasks), 'superseded_leakage': leakage,
                         'untraceable_assets': untraceable,
                         'non_deterministic_queries': non_deterministic},
              'tasks': results}
    text_out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text_out + '\n', 'utf-8')
    print(text_out)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
