"""M2-B.1 L3 tests: /3 aliases contract, atomicity, governance-neutrality,
FTS visibility, L2.1 evidence-normalization inheritance, and contract
isolation from /1 and /2.
"""
from __future__ import annotations
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cch.artifact.index import (connect, register_candidate, apply_semantics,
                                apply_concept_enrichment, validate_aliases,
                                search)
from cch.artifact.scan import AssetCandidate
from cch.artifact import alias_enrich, enrich


def _cand(content: str, source: str) -> AssetCandidate:
    data = content.encode('utf-8')
    return AssetCandidate(
        kind='image', source_path=source, container=None, page=None,
        embedded_ref=None, mime='image/png', size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest())


class AliasValidation(unittest.TestCase):
    def test_range_dedup_and_lowinfo(self):
        ok = ['项目组有哪些单位的人参与', '参与单位和人员组成',
              '任务分工和课题负责人是谁']
        self.assertEqual(validate_aliases(ok), ok)
        dup = ['核心网要做成什么样', '核心网要做成什么样',
               '核心网设备有哪些', '核心网设备怎么联调',
               '核心网验收标准是什么']
        self.assertEqual(len(validate_aliases(dup)), 4)
        for bad in (['只有三个字'],
                    ['这是一个远远超过二十四个字符长度的别名样例用来测试上限拒绝逻辑', '正常别名要够四字', '再来一条正常别名'],
                    ['三条才够', '还差一条'],
                    ['这个素材', '相关内容', '正常别名要四字以上'],
                    '不是列表', [1, 2, 3]):
            with self.assertRaises(SystemExit):
                validate_aliases(bad)


class _Fixture(unittest.TestCase):
    def _home(self):
        home = Path(tempfile.mkdtemp(prefix='cch-alias-'))
        conn = connect(home)
        cand = _cand('人员分工表', '/lib/staff-alias.png')
        aid = register_candidate(conn, cand, 'doc-staff-alias',
                                 '2026-09-14T00:00:00+00:00',
                                 {'domain': 'd', 'project': 'p',
                                  'topics': ['详细设计']})
        apply_semantics(conn, aid, {'title': '项目组成人员情况表',
                                    'summary': '列出姓名、年龄、职务、工作单位与任务分工'},
                        job_id='J-0', extraction_method='test')
        conn.commit()
        return home, aid


class AliasContract(_Fixture):
    def setUp(self):
        self.home, self.aid = self._home()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_export_import_receipt_and_governance(self):
        job = alias_enrich.export_alias_job(self.home)
        self.assertEqual(job['assets'], 1)
        job_doc = json.loads(Path(job['job_path']).read_text('utf-8'))
        self.assertEqual(job_doc['schema_version'], 'asset-exchange-job/3')
        aliases = ['项目组有哪些单位的人参与分工', '参与单位和人员组成',
                   '任务分工和课题负责人是谁']
        result = {'schema_version': 'asset-exchange-result/3',
                  'job_id': job['job_id'],
                  'assets': [{'asset_id': self.aid,
                              'sha256': job_doc['assets'][0]['sha256'],
                              'aliases': aliases}]}
        rp = self.home/'r3.json'
        rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        receipt = alias_enrich.import_alias_result(self.home, job['job_id'], rp)
        self.assertEqual(receipt['applied'], [self.aid])
        self.assertEqual(receipt['enrichment_method'], 'vlm-aliases-v1')
        self.assertEqual(receipt['semantic_schema_version'], 3)
        conn = connect(self.home)
        row = conn.execute('SELECT * FROM assets WHERE asset_id=?', (self.aid,)).fetchone()
        self.assertEqual(json.loads(row['aliases']), aliases)
        self.assertEqual(row['alias_method'], 'vlm-aliases-v1')
        self.assertEqual(row['alias_job_id'], job['job_id'])
        # Governance neutrality: status unknown, still indexed, /2
        # concepts column untouched (empty in this fixture).
        self.assertEqual(row['asset_status'], 'unknown')
        self.assertEqual(row['index_state'], 'indexed')
        self.assertEqual(json.loads(row['concepts']), [])
        # FTS visibility: aliases are searchable text.
        fts = conn.execute('SELECT aliases FROM assets_fts WHERE asset_id=?',
                           (self.aid,)).fetchone()
        self.assertIn('参与单位和人员组成', fts['aliases'])

    def test_alias_bridging_recalls_colloquial_query(self):
        job = alias_enrich.export_alias_job(self.home)
        job_doc = json.loads(Path(job['job_path']).read_text('utf-8'))
        result = {'schema_version': 'asset-exchange-result/3',
                  'job_id': job['job_id'],
                  'assets': [{'asset_id': self.aid,
                              'sha256': job_doc['assets'][0]['sha256'],
                              'aliases': ['项目组有哪些单位的人参与分工',
                                          '参与单位和人员组成',
                                          '任务分工怎么安排的']}]}
        rp = self.home/'r3.json'
        rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        alias_enrich.import_alias_result(self.home, job['job_id'], rp)
        res = search(self.home, '有哪些单位的人参与分工怎么排',
                     domain='d', project='p', limit=5)
        self.assertTrue(res['results'])
        top = res['results'][0]
        self.assertEqual(top['asset']['asset_id'], self.aid)
        self.assertTrue(any(w.startswith('concept-evidence=') for w in top['why']),
                        top['why'])

    def test_invalid_aliases_leave_no_partial_state(self):
        job = alias_enrich.export_alias_job(self.home)
        job_doc = json.loads(Path(job['job_path']).read_text('utf-8'))
        result = {'schema_version': 'asset-exchange-result/3',
                  'job_id': job['job_id'],
                  'assets': [{'asset_id': self.aid,
                              'sha256': job_doc['assets'][0]['sha256'],
                              'aliases': ['只有一条不够']}]}
        rp = self.home/'bad3.json'
        rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        with self.assertRaises(SystemExit):
            alias_enrich.import_alias_result(self.home, job['job_id'], rp)
        conn = connect(self.home)
        row = conn.execute('SELECT aliases FROM assets WHERE asset_id=?',
                           (self.aid,)).fetchone()
        self.assertEqual(json.loads(row['aliases']), [])
        self.assertFalse((self.home/'artifact'/'jobs'/job['job_id']/'receipt.json').exists())


class ContractIsolation(_Fixture):
    def setUp(self):
        self.home, self.aid = self._home()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_2_and_3_reject_each_other(self):
        job2 = enrich.export_enrich_job(self.home)
        job3 = alias_enrich.export_alias_job(self.home)
        wrong_for_2 = {'schema_version': 'asset-exchange-result/3',
                       'job_id': job2['job_id'], 'assets': []}
        p2 = self.home/'w2.json'
        p2.write_text(json.dumps(wrong_for_2, ensure_ascii=False), 'utf-8')
        with self.assertRaises(SystemExit):
            enrich.import_enrich_result(self.home, job2['job_id'], p2)
        wrong_for_3 = {'schema_version': 'asset-exchange-result/2',
                       'job_id': job3['job_id'], 'assets': []}
        p3 = self.home/'w3.json'
        p3.write_text(json.dumps(wrong_for_3, ensure_ascii=False), 'utf-8')
        with self.assertRaises(SystemExit):
            alias_enrich.import_alias_result(self.home, job3['job_id'], p3)


class AliasEvidenceNormalization(unittest.TestCase):
    # L3 inherits the L2.1 guard: restating one query anchor across
    # several aliases must not multiply the concept bonus.
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='cch-aliasnorm-'))
        conn = connect(self.home)
        def make(hint, concepts, aliases):
            cand = _cand(hint, '/lib/' + hint + '.png')
            aid = register_candidate(conn, cand, 'doc-' + hint,
                                     '2026-09-14T00:00:00+00:00',
                                     {'domain': 'd', 'project': 'p',
                                      'topics': ['详细设计']})
            apply_semantics(conn, aid, {'title': '样例资产' + hint,
                                        'summary': '样例摘要内容' + hint},
                            job_id='J-' + hint, extraction_method='test')
            apply_concept_enrichment(conn, aid, concepts,
                                     'vlm-concepts-v1', 'J-cn-' + hint)
            import json as _json
            conn.execute(
                'UPDATE assets SET aliases=?, alias_method=?, alias_job_id=?'
                ' WHERE asset_id=?',
                (_json.dumps(aliases, ensure_ascii=False),
                 'vlm-aliases-v1', 'J-al-' + hint, aid))
            return aid
        # A restates the anchor 核心网 across concepts AND aliases;
        # B states it once in one concept, aliases avoid the anchor.
        self.a = make('resta', ['核心网设备', '核心网组网', '核心网样机'],
                      ['核心网要做成什么样', '核心网怎么部署', '核心网设备有哪些'])
        self.b = make('onceb', ['核心网架构', '承载网组网', '样机研制'],
                      ['网络架构怎么搭建', '承载方案有哪些', '样机进展如何'])
        for i in range(2):
            make('fill' + str(i), ['填充概念一', '填充概念二', '填充概念三'],
                 ['填充别名一三四', '填充别名二三四', '填充别名三三四'])
        conn.commit()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_alias_stuffing_cannot_multiply_bonus(self):
        r1 = search(self.home, '核心网', domain='d', project='p', limit=10)
        r2 = search(self.home, '核心网', domain='d', project='p', limit=10)
        ids1 = [x['asset']['asset_id'] for x in r1['results']]
        ids2 = [x['asset']['asset_id'] for x in r2['results']]
        self.assertEqual(ids1, ids2)  # deterministic
        by_id = {x['asset']['asset_id']: x for x in r1['results']}
        self.assertIn(self.a, by_id)
        self.assertIn(self.b, by_id)
        # Same single query evidence unit => equal score; A's richer
        # alias restatement of 核心网 must not earn more.
        self.assertEqual(by_id[self.a]['score'], by_id[self.b]['score'])
        self.assertTrue(any(w.startswith('concept-evidence=')
                            for w in by_id[self.a]['why']))


if __name__ == '__main__':
    unittest.main()
