"""Store lifecycle: init, empty-state behavior, export/restore roundtrip with
source verification. Taskbook §14 F."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import bootstrap_envelope, make_object, seeded_home
from cch import agent_io, store
from cch.core import CCHError
from cch.store import Home, approved_commit

class StoreTests(unittest.TestCase):
    def test_empty_store_reports_structured_empty_not_fake_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Home(Path(tmp))
            store.store_init(home)
            status = store.store_status(home)
            self.assertEqual(status['state'], 'EMPTY')
            self.assertEqual(status['mode'], 'cooperative')
            with self.assertRaises(CCHError) as exc:
                agent_io.agent_context(home, '查询', 'demo', 'hub')
            self.assertEqual(exc.exception.code, 'EMPTY_STORE')

    def test_double_init_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Home(Path(tmp))
            store.store_init(home)
            with self.assertRaises(CCHError):
                store.store_init(home)

class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self.temp.name))
        source_path = Path(self.temp.name)/'note.md'
        source_path.write_text('# 笔记\n决定：连接池上限 200，已确认。\n', encoding='utf-8')
        from cch import sources
        self.source_id = sources.register(self.home, source_path, 'demo', 'hub')['source_id']

    def tearDown(self):
        self.temp.cleanup()

    def _add_referencing_object(self):
        """Publish an object whose provenance references the registered source."""
        from cch import proposals, reviews, publisher
        from cch.sources import inspect
        meta = inspect(self.home, self.source_id)
        obj = make_object('FACT-b1000000001', content='恢复测试对象 引用来源')
        obj['provenance'] = {'evidence_refs': ['EV-b1000000001']}
        env = bootstrap_envelope('PR-b1000000001', [obj],
                                 base_revision=approved_commit(self.home),
                                 evidence_assets=[{
                                     'id': 'EV-b1000000001', 'summary': '笔记摘录',
                                     'source_text': '决定：连接池上限 200，已确认。',
                                     'source': {'source_id': self.source_id,
                                                'source_sha256': meta['sha256'],
                                                'source_name': meta['name'],
                                                'block_id': 'BLK-x', 'spans': [[0, 5]]}}],
                                 submitter={'agent_id': 't'})
        proposals.freeze(self.home, env)
        reviews.render_bundle(self.home, 'PR-b1000000001', 1)
        approval = reviews.approve(self.home, 'PR-b1000000001', 1, 'reviewer')
        return publisher.apply(self.home, env, approval,
                               reviews.load_bundle_hash(self.home, 'PR-b1000000001', 1))

    def test_export_restore_roundtrip_and_recompile(self):
        result = self._add_referencing_object()
        package = Path(self.temp.name)/'export'
        store.store_export(self.home, package)
        with tempfile.TemporaryDirectory() as fresh:
            new_home = Home(Path(fresh)/'home2')
            restored = store.store_restore(new_home, package)
            self.assertEqual(restored['restored_revision'], result['revision'])
            self.assertEqual(approved_commit(new_home), result['revision'])
            self.assertEqual(restored['sources_restored'], 1)
            run = agent_io.agent_context(new_home, '恢复测试 对象', 'demo', 'hub')
            self.assertGreater(run['receipt']['tokens'], 0)

    def test_restore_refuses_nonempty_target(self):
        store.store_export(self.home, Path(self.temp.name)/'export2')
        with tempfile.TemporaryDirectory() as fresh:
            target = Home(Path(fresh)/'home3')
            store.store_init(target)
            with self.assertRaises(CCHError) as exc:
                store.store_restore(target, Path(self.temp.name)/'export2')
            self.assertEqual(exc.exception.code, 'TARGET_NOT_EMPTY')

    def test_restore_rejects_package_missing_source(self):
        self._add_referencing_object()
        package = Path(self.temp.name)/'export3'
        store.store_export(self.home, package)
        # remove the source blob: a "complete" restore must fail honestly
        for blob in (package/'sources').iterdir():
            blob.unlink()
        with tempfile.TemporaryDirectory() as fresh:
            with self.assertRaises(CCHError) as exc:
                store.store_restore(Home(Path(fresh)/'home4'), package)
            self.assertEqual(exc.exception.code, 'VALIDATION_ERROR')

if __name__ == '__main__':
    unittest.main()
