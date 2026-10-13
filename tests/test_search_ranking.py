"""Regression tests for the v0.3.5 unified search ranking.

Covers the three failure modes measured by the M2-B held-out pilot:
  1. bm25 direction inversion (strong lexical match lost to weak one);
  2. paraphrase queries missing topic-tagged assets entirely (recall);
  3. colloquial-vs-title vocabulary gap ("设计图" vs "架构图").
Every scenario asserts deterministic ordering across repeated runs.
"""
from __future__ import annotations
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from cch.artifact.index import connect, register_candidate, apply_semantics, search
from cch.artifact.scan import AssetCandidate


def _cand(content: str, source: str) -> AssetCandidate:
    data = content.encode('utf-8')
    return AssetCandidate(
        kind='image', source_path=source, container=None, page=None,
        embedded_ref=None, mime='image/png', size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest())


class SearchRanking(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='cch-rank-'))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _asset(self, conn, asset_id_hint: str, title: str, summary: str,
               topics: list[str]) -> str:
        cand = _cand(title + summary, f'/lib/{asset_id_hint}.png')
        aid = register_candidate(conn, cand, 'doc-' + asset_id_hint,
                                 '2026-09-12T00:00:00+00:00',
                                 {'domain': 'd', 'project': 'p', 'topics': topics})
        apply_semantics(conn, aid, {'title': title, 'summary': summary},
                        job_id='J-' + asset_id_hint, extraction_method='test')
        return aid

    def test_01_stronger_lexical_match_wins(self):
        conn = connect(self.home)
        # Filler assets keep the corpus large enough for FTS5 idf to be
        # informative (in a 2-row corpus every term idf degenerates to ~0).
        for i in range(2):
            self._asset(conn, f'fill{i}', f'无关材料{i}',
                        '与查询无关的示例素材内容', ['其他'])
        weak = self._asset(conn, 'weak', '其他页',
                           '验收指标说明页', ['文档'])
        strong = self._asset(conn, 'strong', '考核指标表',
                            '课题考核指标表，验收指标值逐项核对', ['文档'])
        q = '把课题一到课题三的考核指标表找出来，对一下验收指标值'
        r1 = search(self.home, q, domain='d', project='p', topics=['文档'], limit=5)
        r2 = search(self.home, q, domain='d', project='p', topics=['文档'], limit=5)
        self.assertEqual([x['asset']['asset_id'] for x in r1['results']],
                         [x['asset']['asset_id'] for x in r2['results']])
        self.assertEqual(r1['results'][0]['asset']['asset_id'], strong)
        self.assertIn(weak, [x['asset']['asset_id'] for x in r1['results']])

    def test_02_topic_channel_recalls_paraphrase_query(self):
        conn = connect(self.home)
        target = self._asset(conn, 'tgt', '05 成果归属判定方法',
                             '四问四答判定流程与典型示例', ['成果归属', '判定方法'])
        other = self._asset(conn, 'oth', '预算表',
                           '全年支出明细记录', ['财务'])
        res = search(self.home, '项目成果到底算谁的，圈层和架构怎么判',
                     domain='d', project='p',
                     topics=['成果归属', '判定方法'], limit=5)
        ids = [x['asset']['asset_id'] for x in res['results']]
        self.assertIn(target, ids)
        self.assertNotIn(other, ids)
        self.assertTrue(res['results'][0]['why'] and
                        'topic-channel' in res['results'][0]['why'])

    def test_03_title_topic_outranks_summary_topic(self):
        conn = connect(self.home)
        title_hit = self._asset(conn, 'tt', '成果归属判定方法页',
                               '流程示例说明', ['成果归属'])
        summary_hit = self._asset(conn, 'st', '培训某页',
                                 '讲述成果归属的判定', ['成果归属'])
        res = search(self.home, '成果怎么归属怎么判定',
                     domain='d', project='p', topics=['成果归属'], limit=5)
        ids = [x['asset']['asset_id'] for x in res['results']]
        self.assertIn(title_hit, ids)
        self.assertIn(summary_hit, ids)
        self.assertLess(ids.index(title_hit), ids.index(summary_hit))

    def test_04_synonym_expansion_bridges_vocabulary(self):
        conn = connect(self.home)
        target = self._asset(conn, 'arch', '平台系统架构图',
                            '分层组成与调用关系', ['系统架构'])
        res = search(self.home, '整体设计图', domain='d', project='p',
                     topics=['系统架构'], limit=5)
        ids = [x['asset']['asset_id'] for x in res['results']]
        self.assertIn(target, ids)
        self.assertIn('设计图->架构图', res['synonym_expansions'])

    def test_05_bigram_concept_recall_without_topic_tags(self):
        # M2-B.1 L1 regression: the v2-gate N2 failure shape. The asset
        # summary literally contains the 2-char concepts the user asks
        # with; the old 3-6 char windowing returned zero candidates.
        conn = connect(self.home)
        for i in range(2):
            self._asset(conn, f'fill{i}', f'无关材料{i}',
                        '与查询无关的示例素材内容', ['其他'])
        target = self._asset(conn, 'staff', '项目组成人员情况表',
                             '列出姓名、年龄、职务、工作单位与任务分工',
                             ['详细设计', '可研报告'])
        q = '这个项目都有哪些单位的人参与，分工是怎么排的'
        r1 = search(self.home, q, domain='d', project='p', limit=5)
        r2 = search(self.home, q, domain='d', project='p', limit=5)
        ids = [x['asset']['asset_id'] for x in r1['results']]
        self.assertEqual(ids, [x['asset']['asset_id'] for x in r2['results']])
        self.assertEqual(ids[0], target)
        # v0.4.0 (QW): the audit token for the surface bigram channel is
        # 'surface-bigram=' (the per-hit bonus moved into the unified
        # evidence post-pass; the channel itself remains pool-entry only).
        self.assertTrue(any(w.startswith('surface-bigram=') and '单位' in w
                            for x in r1['results'] for w in x['why']))

    def test_06_lowalt_concept_recall(self):
        # M2-B.1 L1 regression: the v2-gate N4 failure shape ("低空").
        conn = connect(self.home)
        for i in range(2):
            self._asset(conn, f'fill{i}', f'无关材料{i}',
                        '与查询无关的示例素材内容', ['其他'])
        target = self._asset(conn, 'lowalt', '可研任务4页',
                             '低空区域网络架构设计与电力低空业务研究内容',
                             ['详细设计', '可研报告'])
        q = '低空这一块立项做了哪些研究和安排'
        r1 = search(self.home, q, domain='d', project='p', limit=5)
        ids = [x['asset']['asset_id'] for x in r1['results']]
        self.assertEqual(ids[0], target)


if __name__ == '__main__':
    unittest.main()
