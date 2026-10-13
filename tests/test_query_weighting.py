"""v0.4.0 (M2-B.1 QW) query-informativeness weighting dev fixtures.

Per experiment discipline, these fixtures are deliberately unrelated to
the N9/N14 gate failures in content (office-domain anchors) while
reproducing their STRUCTURE:
  - a compact keyword query and a padded colloquial query target the
    same asset;
  - adding no-information padding must not bury the true anchors;
  - a wordy page stuffed with high-df generic words must not outrank
    the anchor page just by matching more fragments;
  - per-bigram weights are corpus-derived (rare anchor >> generic).
No heldout targets, no failure-task vocabulary, no per-task rules.
"""
from __future__ import annotations
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from cch.artifact.index import (_corpus_weights, _query_concepts, connect,
                                register_candidate, apply_semantics, search)
from cch.artifact.scan import AssetCandidate


def _cand(content: str, source: str) -> AssetCandidate:
    data = content.encode('utf-8')
    return AssetCandidate(
        kind='image', source_path=source, container=None, page=None,
        embedded_ref=None, mime='image/png', size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest())


ANCHORS = ('合同归档', '审批时限')  # office-domain, unrelated to gate tasks
GENERIC = '项目 材料 内容 工作 页面 相关 总结 存档'


class QueryWeighting(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='cch-qw-'))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _asset(self, conn, hint: str, title: str, summary: str,
               topics: list[str]) -> str:
        cand = _cand(title + summary, f'/lib/{hint}.png')
        aid = register_candidate(conn, cand, 'doc-' + hint,
                                 '2026-09-14T00:00:00+00:00',
                                 {'domain': 'd', 'project': 'p',
                                  'topics': topics})
        apply_semantics(conn, aid, {'title': title, 'summary': summary},
                        job_id='J-' + hint, extraction_method='test')
        return aid

    def _seed(self, conn) -> str:
        # 8 filler pages carry the generic vocabulary so generic bigrams
        # reach df>=8 (the 0.25 weight floor); anchors stay df=1.
        for i in range(8):
            self._asset(conn, f'fill{i}', f'无关材料{i}',
                        f'{GENERIC} 无关{i}号示例',
                        ['其他'])
        # Wordy page: stuffed with the generic vocabulary, zero anchors.
        wordy = self._asset(conn, 'wordy', '综合汇总页',
                            '项目、材料、内容、工作、页面、相关均有涉及，'
                            '总结与存档情况较多',
                            ['综合'])
        # Anchor page: carries the anchors, minimal generic stuffing.
        target = self._asset(conn, 'anchor', '归档审批页',
                            '合同归档流程说明与审批时限要求',
                            ['归档'])
        return target, wordy

    def test_01_weights_are_corpus_derived(self):
        conn = connect(self.home)
        self._seed(conn)
        bgs = _query_concepts('合同归档 审批时限 项目内容材料')['bigrams']
        # v0.4.0 numeral-measure class rule: "那一页" is counting noise,
        # never a content anchor ("一页" must not become a query bigram).
        self.assertNotIn('一页', bgs)
        # RC fix (77244cc review): the class covers pure navigation
        # measures ONLY; content-bearing numeral phrases must survive.
        noise = _query_concepts('帮我找那一页 有一张图讲归档')['bigrams']
        self.assertNotIn('一页', noise)
        self.assertNotIn('一张', noise)
        keep = _query_concepts('三步实施路线 三项审批要求 两类归档方式 一套备份机制')['bigrams']
        for anchor in ('三步', '三项', '两类', '一套'):
            self.assertIn(anchor, keep)
        w = _corpus_weights(conn, bgs)
        self.assertEqual(w['合同'], 2.0)     # df=1 rare anchor: full unit
        self.assertEqual(w['归档'], 2.0)
        self.assertEqual(w['项目'], 0.25)    # df>=8 generic: floor
        self.assertEqual(w['材料'], 0.25)
        self.assertGreater(w['合同'], w['项目'])

    def test_02_padding_does_not_bury_anchors(self):
        conn = connect(self.home)
        target, _ = self._seed(conn)
        compact = '合同归档 审批时限'
        padded = ('这个项目内容材料工作相关的页面总结存档很多，'
                  '帮我找之前合同归档和审批时限的那一页')
        r1 = search(self.home, compact, domain='d', project='p', limit=5)
        r2 = search(self.home, padded, domain='d', project='p', limit=5)
        ids1 = [x['asset']['asset_id'] for x in r1['results']]
        ids2 = [x['asset']['asset_id'] for x in r2['results']]
        self.assertEqual(ids1[0], target)
        self.assertEqual(ids2[0], target)
        r2b = search(self.home, padded, domain='d', project='p', limit=5)
        self.assertEqual(ids2,
                         [x['asset']['asset_id'] for x in r2b['results']])

    def test_03_wordy_page_cannot_outrank_anchor_page(self):
        conn = connect(self.home)
        target, wordy = self._seed(conn)
        padded = ('这个项目内容材料工作相关的页面总结存档很多，'
                  '帮我找之前合同归档和审批时限的那一页')
        res = search(self.home, padded, domain='d', project='p', limit=10)
        ids = [x['asset']['asset_id'] for x in res['results']]
        self.assertIn(target, ids)
        self.assertIn(wordy, ids)
        self.assertLess(ids.index(target), ids.index(wordy))
        target_item = next(x for x in res['results']
                           if x['asset']['asset_id'] == target)
        self.assertTrue(any(w.startswith('qw-evidence=') and '合同=2.0' in w
                            for w in target_item['why']))


if __name__ == '__main__':
    unittest.main()
