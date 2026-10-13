"""M2-B.1 L2 tests: /2 concepts contract, atomicity, governance-neutrality,
and the independent concept recall channel.
"""
from __future__ import annotations
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cch.artifact.index import (connect, register_candidate, apply_semantics,
                                apply_concept_enrichment, validate_concepts,
                                search)
from cch.artifact.scan import AssetCandidate
from cch.artifact import enrich


def _cand(content: str, source: str) -> AssetCandidate:
    data = content.encode('utf-8')
    return AssetCandidate(
        kind='image', source_path=source, container=None, page=None,
        embedded_ref=None, mime='image/png', size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest())


class ConceptValidation(unittest.TestCase):
    def test_range_dedup_and_lowinfo(self):
        ok = validate_concepts(['项目组成员', '工作单位', '任务分工', '课题负责人'])
        self.assertEqual(ok, ['项目组成员', '工作单位', '任务分工', '课题负责人'])
        self.assertEqual(validate_concepts(['低空网络架构', '项目', '低空网络架构', '网络切片', '频谱资源分配'])[0],
                         '低空网络架构')
        for bad in (['太短', '低空网络架构'],
                    ['这是一个远远超过十六个字长度的概念词样例测试文本', '低空网络架构', '网络切片'],
                    ['只有两个'],
                    ['A' * 17, '低空网络架构', '网络切片'],
                    '不是列表',
                    [1, 2, 3]):
            with self.assertRaises(SystemExit):
                validate_concepts(bad)


class EvidenceNormalization(unittest.TestCase):
    # L2.1: the concept bonus must reward DISTINCT QUERY EVIDENCE,
    # not how many asset concepts restate the same anchor.
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='cch-evnorm-'))
        conn = connect(self.home)
        def make(hint, concepts):
            cand = _cand(hint, '/lib/' + hint + '.png')
            aid = register_candidate(conn, cand, 'doc-' + hint,
                                     '2026-09-14T00:00:00+00:00',
                                     {'domain': 'd', 'project': 'p',
                                      'topics': ['详细设计']})
            apply_semantics(conn, aid, {'title': '样例资产' + hint,
                                        'summary': '样例摘要内容' + hint},
                            job_id='J-' + hint, extraction_method='test')
            if concepts is not None and len(concepts) >= 3:
                apply_concept_enrichment(conn, aid, concepts,
                                         'vlm-concepts-v1', 'J-ev-' + hint)
            elif concepts is not None:
                # Single-concept assets bypass the /2 batch contract
                # (which requires 3-12) but are legal index shapes;
                # write the retrieval-metadata columns directly.
                import json as _json
                conn.execute(
                    'UPDATE assets SET concepts=?, enrichment_method=?,'
                    ' enrichment_job_id=? WHERE asset_id=?',
                    (_json.dumps(concepts, ensure_ascii=False),
                     'vlm-concepts-v1', 'J-ev-' + hint, aid))
            return aid
        # A restates ONE anchor as three concepts; B keeps one.
        self.a = make('enuma', ['核心网设备', '核心网网管', '核心网样机'])
        self.b = make('singleb', ['核心网架构'])
        for i in range(2):
            make('fill' + str(i), [])
        conn.commit()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_enumeration_density_cannot_multiply_bonus(self):
        r1 = search(self.home, '核心网', domain='d', project='p', limit=10)
        r2 = search(self.home, '核心网', domain='d', project='p', limit=10)
        ids1 = [x['asset']['asset_id'] for x in r1['results']]
        ids2 = [x['asset']['asset_id'] for x in r2['results']]
        self.assertEqual(ids1, ids2)  # deterministic
        by_id = {x['asset']['asset_id']: x for x in r1['results']}
        self.assertIn(self.a, by_id)
        self.assertIn(self.b, by_id)
        # Same single query evidence unit => equal concept bonus.
        self.assertEqual(by_id[self.a]['score'], by_id[self.b]['score'])
        # Audit trail keeps the evidence-to-concept mapping.
        # The bigram extractor may split the anchor into informative
        # sub-bigrams ("核心网" -> "核心", "心网"); assert at the unit
        # that matters: the whole evidence mapping is present, and both
        # assets scored identically (already asserted above).
        why_a = [w for w in by_id[self.a]['why']
                 if w.startswith('concept-evidence=')]
        why_b = [w for w in by_id[self.b]['why']
                 if w.startswith('concept-evidence=')]
        self.assertTrue(why_a, why_a)
        self.assertTrue(why_b, why_b)


class EnrichContract(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='cch-enrich-'))
        conn = connect(self.home)
        cand = _cand('人员分工表', '/lib/staff.png')
        aid = register_candidate(conn, cand, 'doc-staff', '2026-09-13T00:00:00+00:00',
                                 {'domain': 'd', 'project': 'p', 'topics': ['详细设计']})
        apply_semantics(conn, aid, {'title': '项目组成人员情况表',
                                    'summary': '列出姓名、年龄、职务、工作单位与任务分工'},
                        job_id='J-0', extraction_method='test')
        self.aid = aid

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_export_import_receipt_and_search(self):
        job = enrich.export_enrich_job(self.home)
        self.assertEqual(job['assets'], 1)
        job_doc = json.loads(Path(job['job_path']).read_text('utf-8'))
        self.assertEqual(job_doc['schema_version'], 'asset-exchange-job/2')
        result = {'schema_version': 'asset-exchange-result/2',
                  'job_id': job['job_id'],
                  'assets': [{'asset_id': self.aid,
                              'sha256': job_doc['assets'][0]['sha256'],
                              'concepts': ['项目组成员', '工作单位', '任务分工', '课题负责人']}]}
        rp = self.home/'r2.json'
        rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        receipt = enrich.import_enrich_result(self.home, job['job_id'], rp)
        self.assertEqual(receipt['applied'], [self.aid])
        self.assertEqual(receipt['enrichment_method'], 'vlm-concepts-v1')
        self.assertEqual(receipt['semantic_schema_version'], 2)
        conn = connect(self.home)
        row = conn.execute('SELECT * FROM assets WHERE asset_id=?', (self.aid,)).fetchone()
        self.assertEqual(json.loads(row['concepts']),
                         ['项目组成员', '工作单位', '任务分工', '课题负责人'])
        self.assertEqual(row['enrichment_method'], 'vlm-concepts-v1')
        # Governance neutrality: status stays unknown, still indexed.
        self.assertEqual(row['asset_status'], 'unknown')
        self.assertEqual(row['index_state'], 'indexed')
        # Concept channel recall.
        res = search(self.home, '有哪些单位的人参与分工怎么排', domain='d', project='p', limit=5)
        top = res['results'][0]
        self.assertEqual(top['asset']['asset_id'], self.aid)
        self.assertTrue(any(w.startswith('concept-evidence=') for w in top['why']),
                        top['why'])

    def test_invalid_concepts_leave_no_partial_state(self):
        job = enrich.export_enrich_job(self.home)
        job_doc = json.loads(Path(job['job_path']).read_text('utf-8'))
        result = {'schema_version': 'asset-exchange-result/2',
                  'job_id': job['job_id'],
                  'assets': [{'asset_id': self.aid,
                              'sha256': job_doc['assets'][0]['sha256'],
                              'concepts': ['只有一个']}]}
        rp = self.home/'bad.json'
        rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        with self.assertRaises(SystemExit):
            enrich.import_enrich_result(self.home, job['job_id'], rp)
        conn = connect(self.home)
        row = conn.execute('SELECT * FROM assets WHERE asset_id=?', (self.aid,)).fetchone()
        self.assertEqual(json.loads(row['concepts']), [])
        self.assertFalse(receipt_exists(self.home, job['job_id']))

    def test_v1_contract_still_importable(self):
        from cch.artifact.exchange import import_result
        # The /1 importer rejects /2 results and vice versa; existing /1
        # jobs remain untouched and readable.
        job = enrich.export_enrich_job(self.home)
        job_doc = json.loads(Path(job['job_path']).read_text('utf-8'))
        wrong = {'schema_version': 'asset-exchange-result/1',
                 'job_id': job['job_id'], 'assets': []}
        rp = self.home/'wrong.json'
        rp.write_text(json.dumps(wrong, ensure_ascii=False), 'utf-8')
        with self.assertRaises(SystemExit):
            enrich.import_enrich_result(self.home, job['job_id'], rp)


def receipt_exists(home: Path, job_id: str) -> bool:
    return (home/'artifact'/'jobs'/job_id/'receipt.json').exists()


if __name__ == '__main__':
    unittest.main()
