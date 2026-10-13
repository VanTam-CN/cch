"""Agent protocol tests: receipts, run isolation, freshness semantics including
time-expiry with unchanged revision. Taskbook §14 B/§11."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import bootstrap_envelope, make_object, publish, seeded_home
from cch import agent_io
from cch.store import canon_digest

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_run_dir_contains_receipt_audit_context(self):
        run = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW)
        run_dir = Path(run['run_dir'])
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()),
                         ['audit.json', 'context.md', 'receipt.json'])
        receipt = json.loads((run_dir/'receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['receipt_hash'],
                         canon_digest({k: receipt[k] for k in agent_io.RECEIPT_FIELDS}))
        self.assertEqual(receipt['scope']['domain'], 'demo')

    def test_query_required_scope_not_guessed(self):
        with self.assertRaises(Exception):
            agent_io.agent_context(self.home, '  ', 'demo', 'hub')
        with self.assertRaises(Exception):
            agent_io.agent_context(self.home, 'q', 'demo', '*')

    def test_fresh_when_nothing_changed(self):
        run = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW)
        check = agent_io.agent_check(self.home, run['receipt'], now=NOW + timedelta(hours=1))
        self.assertEqual(check['status'], 'FRESH')

    def test_revision_change_detected(self):
        run = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW)
        publish(self.home, bootstrap_envelope('PR-d1000000001', [make_object('FACT-d1000000001')],
                                              base_revision=run['receipt']['knowledge_revision']))
        check = agent_io.agent_check(self.home, run['receipt'], now=NOW)
        self.assertEqual(check['status'], 'REVISION_CHANGED')

    def test_unchanged_revision_but_validity_expired_requires_recompile(self):
        from cch.store import approved_commit
        expiring = make_object('FACT-d1000000004',
                               lifecycle={'valid_from': '2026-09-01', 'valid_until': '2026-09-08', 'ttl_hours': None})
        publish(self.home, bootstrap_envelope('PR-d1000000004', [expiring],
                                              base_revision=approved_commit(self.home)))
        run = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW)
        self.assertEqual(agent_io.agent_check(self.home, run['receipt'], now=NOW)['status'], 'FRESH')
        check = agent_io.agent_check(self.home, run['receipt'],
                                     now=datetime(2026, 9, 9, tzinfo=timezone.utc))
        self.assertEqual(check['status'], 'RECOMPILE_REQUIRED')

    def test_future_object_entering_validity_detected(self):
        from cch.store import approved_commit
        future = make_object('FACT-d1000000002', lifecycle={'valid_from': '2026-10-01',
                                                            'valid_until': None, 'ttl_hours': None})
        publish(self.home, bootstrap_envelope('PR-d1000000002', [future],
                                              base_revision=approved_commit(self.home)))
        run = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW)
        self.assertEqual(agent_io.agent_check(self.home, run['receipt'], now=NOW)['status'], 'FRESH')
        check = agent_io.agent_check(self.home, run['receipt'],
                                     now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(check['status'], 'RECOMPILE_REQUIRED')

    def test_pack_export_is_self_contained(self):
        run = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW)
        target = Path(self.temp.name)/'pack'
        pack = agent_io.export_pack(self.home, run['run_dir'], target)
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ['README.txt', 'audit.json', 'context.md', 'receipt.json'])
        self.assertEqual(pack['knowledge_revision'], run['receipt']['knowledge_revision'])
        with self.assertRaises(Exception):
            agent_io.export_pack(self.home, run['run_dir'], target)  # refuses non-empty target

    def test_historical_revision_reproducible(self):
        first = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW)
        publish(self.home, bootstrap_envelope('PR-d1000000003', [make_object('FACT-d1000000003')],
                                              base_revision=first['receipt']['knowledge_revision']))
        old = agent_io.agent_context(self.home, '连接池', 'demo', 'hub', as_of=NOW,
                                     revision=first['receipt']['knowledge_revision'])
        self.assertEqual(Path(old['context_path']).read_text(encoding='utf-8'),
                         Path(first['context_path']).read_text(encoding='utf-8'))

if __name__ == '__main__':
    unittest.main()
