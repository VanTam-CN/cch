"""Demonstrate visual-candidate reuse with fictional images and recorded semantics.

No model/provider requests or real project approvals. Existing output is preserved.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default='output/asset-demo')
    args = parser.parse_args()
    out = ROOT / args.out
    if out.exists():
        raise SystemExit('Output already exists; choose a new --out directory. Nothing was deleted.')
    out.mkdir(parents=True)
    home = out / 'cch-home'

    def asset(*arguments):
        call = subprocess.run(
            [sys.executable, '-m', 'cch.artifact', *map(str, arguments), '--home', str(home)],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        return json.loads(call.stdout)

    scanned = asset('scan', ROOT / 'examples' / 'assets', '--domain', 'demo', '--project', 'hub')
    assert scanned['scanned'] == 2, scanned
    exported = asset('export-job')
    job = json.loads(Path(exported['job_path']).read_text(encoding='utf-8'))
    by_name = {Path(item['locator']['file']).name: item for item in job['assets']}
    result = {'schema_version': 'asset-exchange-result/1', 'job_id': job['job_id'], 'assets': []}
    for version in (1, 2):
        item = by_name[f'architecture-v{version}.png']
        result['assets'].append({
            'asset_id': item['asset_id'], 'sha256': item['sha256'],
            'title': f'虚构系统架构 V{version}',
            'summary': 'Recorded synthetic description: DOCS → CCH → AGENT'
                       + ('; MEM box below CCH.' if version == 2 else '.'),
            'extracted_text': ['DOCS', 'CCH', 'AGENT'] + (['MEM'] if version == 2 else []),
            'entities': ['系统架构', 'CCH'], 'visual_type': 'architecture-diagram',
            'asset_status': 'unknown',
        })
    result_path = out / 'recorded-result.json'
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    receipt = asset('import-result', job['job_id'], '--result', result_path)
    assert len(receipt['applied']) == 2, receipt
    old = by_name['architecture-v1.png']['asset_id']
    newer = by_name['architecture-v2.png']['asset_id']
    asset('supersede', old, '--by', newer)
    active = asset('search', '系统架构', '--domain', 'demo', '--project', 'hub')
    history = asset('search', '系统架构', '--domain', 'demo', '--project', 'hub', '--include-superseded')
    active_ids = {item['asset']['asset_id'] for item in active['results']}
    history_ids = {item['asset']['asset_id'] for item in history['results']}
    assert newer in active_ids and old not in active_ids, active
    assert {old, newer}.issubset(history_ids), history
    report = {
        'fixture_only': True, 'model_requests': 0,
        'supersession': 'Script-supplied fictional human decision; not real project approval.',
        'registered': scanned, 'job_id': job['job_id'], 'import_receipt': receipt,
        'active_search': active, 'historical_search': history,
        'scope': {'domain': 'demo', 'project': 'hub'},
    }
    (out / 'asset-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('SYNTHETIC ASSET DEMO PASS')
    print('Two candidates; active search excludes V1; history retains both; hashes and locators preserved.')


if __name__ == '__main__':
    main()
