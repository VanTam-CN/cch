"""Tests for the Gold Pilot evaluator's gate logic (proposal section 11).

Builds a tiny real index through the CLI, then proves: all gates pass on a
well-formed gold task; a gold file that claims an un-superseded asset is
superseded produces leakage > 0 and exit 1; recall and determinism are
scored mechanically.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_cli(home: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, '-m', 'cch.artifact', *args,
                           '--home', str(home)],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def run_pilot(home: Path, gold: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, 'tools/asset_pilot.py', '--home', str(home),
         '--gold', str(gold)],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout)


class PilotGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix='cch-pilot-home-'))
        cls.lib = Path(tempfile.mkdtemp(prefix='cch-pilot-lib-'))
        # Bytes need not be valid images: the scanner hashes content and
        # registers candidates by extension, which is all this test needs.
        (cls.lib/'甲-架构图.png').write_bytes(b'synthetic-alpha-arch')
        (cls.lib/'乙-架构图.png').write_bytes(b'synthetic-beta-arch')
        code, out, err = run_cli(cls.home, 'scan', str(cls.lib),
                                 '--domain', 'demo', '--project', 'pilot')
        assert code == 0, err
        code, out, _ = run_cli(cls.home, 'export-job')
        assert code == 0
        job = json.loads(out)
        job_json = json.loads(Path(job['job_path']).read_text('utf-8'))
        cls.sha = {}
        assets = []
        for frozen in job_json['assets']:
            name = '总体架构图甲' if '甲' in frozen['locator']['file'] else '总体架构图乙'
            cls.sha[name] = frozen['sha256']
            assets.append({'asset_id': frozen['asset_id'], 'sha256': frozen['sha256'],
                           'title': name, 'summary': name + '描述',
                           'extracted_text': ['数据服务中心'], 'entities': ['网管'],
                           'visual_type': 'architecture-diagram'})
        result = {'schema_version': 'asset-exchange-result/1',
                  'job_id': job['job_id'], 'assets': assets}
        rp = cls.home/'artifact'/'jobs'/job['job_id']/'result.json'
        rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        code, _, err = run_cli(cls.home, 'import-result', job['job_id'], '--result', str(rp))
        assert code == 0, err
        # Mark 乙 superseded (human decision), keep 甲 current.
        superseded_id = next(a['asset_id'] for a in assets if a['title'] == '总体架构图乙')
        code, _, err = run_cli(cls.home, 'supersede', superseded_id)
        assert code == 0, err

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.lib, ignore_errors=True)

    def test_01_gates_pass_and_recall_scored(self):
        # 乙 is human-superseded, so the pilot excludes it from top-k and
        # gold must not count it as relevant. 甲 is the only relevant doc;
        # the flagged-乙 entry is still surfaced for audit visibility.
        gold = self.home/'gold.json'
        gold.write_text(json.dumps({'tasks': [{
            'query': '总体架构', 'domain': 'demo', 'project': 'pilot',
            'relevant_sha256': [self.sha['总体架构图甲']],
            'superseded_sha256': [self.sha['总体架构图乙']]}]}, ensure_ascii=False), 'utf-8')
        code, report = run_pilot(self.home, gold)
        self.assertEqual(code, 0, json.dumps(report, ensure_ascii=False))
        self.assertTrue(report['gates']['traceability_100'])
        self.assertTrue(report['gates']['unflagged_stale_leakage_zero'])
        self.assertTrue(report['gates']['deterministic_ranking'])
        self.assertTrue(report['gates']['no_cognitive_base_writes'])
        self.assertEqual(report['mean_recall_at_k'], 1.0)
        task = report['tasks'][0]
        self.assertEqual(task['superseded_leakage'], [])

    def test_02_false_gold_supersession_fails_gate(self):
        # 甲 is NOT superseded in the index; gold claims it is. The pilot
        # must treat the unflagged recommendation as leakage -> exit 1.
        gold = self.home/'gold-bad.json'
        gold.write_text(json.dumps({'tasks': [{
            'query': '总体架构', 'domain': 'demo', 'project': 'pilot',
            'relevant_sha256': [self.sha['总体架构图甲']],
            'superseded_sha256': [self.sha['总体架构图甲']]}]}, ensure_ascii=False), 'utf-8')
        code, report = run_pilot(self.home, gold)
        self.assertEqual(code, 1)
        self.assertFalse(report['gates']['unflagged_stale_leakage_zero'])
        self.assertEqual(report['totals']['superseded_leakage'], 1)

    def test_03_empty_gold_rejected(self):
        gold = self.home/'gold-empty.json'
        gold.write_text('{"tasks": []}', 'utf-8')
        code, _ = run_pilot(self.home, gold)
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
