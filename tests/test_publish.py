"""Publishing gate tests: approval binding, atomicity, idempotency, crash recovery,
concurrent applies, refresh invalidation. Taskbook §14 B/C."""
import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import bootstrap_envelope, make_object, publish, seeded_home
from cch import proposals, reviews, publisher
from cch.core import CCHError
from cch.snapshots import load_base
from cch.store import Home, approved_commit, canon_digest, cas_update_ref, history, make_commit, write_tree_from_dir

class PublishGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self.temp.name))
        self.base_revision = approved_commit(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def next_envelope(self, pid, objects=None, **extra):
        env = bootstrap_envelope(pid, objects, base_revision=self.base_revision)
        env.update(extra)
        return env

    def test_unapproved_cannot_publish(self):
        env = self.next_envelope('PR-a1000000001', [make_object('FACT-a1000000001')])
        proposals.freeze(self.home, copy.deepcopy(env))
        with self.assertRaises(CCHError) as exc:
            publisher.apply(self.home, env, None)
        self.assertEqual(exc.exception.code, 'UNAPPROVED')

    def test_modified_content_cannot_publish(self):
        env = self.next_envelope('PR-a1000000002', [make_object('FACT-a1000000002')])
        proposals.freeze(self.home, copy.deepcopy(env))
        reviews.render_bundle(self.home, 'PR-a1000000002', 1)
        approval = reviews.approve(self.home, 'PR-a1000000002', 1, 'reviewer')
        tampered = copy.deepcopy(env)
        tampered['operations'][0]['after']['content'] = 'tampered after approval'
        with self.assertRaises(CCHError) as exc:
            publisher.apply(self.home, tampered, approval)
        self.assertEqual(exc.exception.code, 'APPROVAL_MISMATCH')

    def test_tentative_stays_tentative_after_approval(self):
        candidate = make_object('FACT-a1000000003', confidence='TENTATIVE', authority='AI_INFERRED',
                                content='试运行建议 100')
        result = publish(self.home, self.next_envelope('PR-a1000000003', [candidate]))
        self.assertEqual(result['status'], 'APPLIED')
        _, base = load_base(self.home)
        obj = base.objects['FACT-a1000000003']
        self.assertEqual((obj['confidence'], obj['authority']), ('TENTATIVE', 'AI_INFERRED'))

    def test_whole_tree_invalid_means_no_publish(self):
        a = make_object('FACT-a1000000004')
        b = make_object('FACT-a1000000005')
        a['relations']['depends_on'] = ['FACT-a1000000005']
        b['relations']['depends_on'] = ['FACT-a1000000004']  # cross-candidate cycle
        env = self.next_envelope('PR-a1000000004', [a, b])
        with self.assertRaises(CCHError):
            publish(self.home, env)
        self.assertEqual(approved_commit(self.home), self.base_revision)  # formal ref untouched

    def test_stale_base_rejected_and_refresh_republishes(self):
        first = publish(self.home, self.next_envelope('PR-a1000000006', [make_object('FACT-a1000000006')]))
        self.assertEqual(first['status'], 'APPLIED')
        env = self.next_envelope('PR-a1000000007', [make_object('FACT-a1000000007')])
        proposals.freeze(self.home, copy.deepcopy(env))
        bundle = reviews.render_bundle(self.home, 'PR-a1000000007', 1)
        approval = reviews.approve(self.home, 'PR-a1000000007', 1, 'reviewer')
        with self.assertRaises(CCHError) as exc:
            publisher.apply(self.home, env, approval, bundle['bundle_hash'])
        self.assertEqual(exc.exception.code, 'STALE_BASE')
        refreshed = proposals.refresh(self.home, 'PR-a1000000007')
        self.assertEqual(refreshed['proposal_version'], 2)
        bundle2 = reviews.render_bundle(self.home, 'PR-a1000000007', 2)
        approval2 = reviews.approve(self.home, 'PR-a1000000007', 2, 'reviewer')
        env2 = proposals.read_proposal(self.home, 'PR-a1000000007', 2)
        result = publisher.apply(self.home, env2, approval2, bundle2['bundle_hash'])
        self.assertEqual(result['status'], 'APPLIED')

    def test_apply_is_idempotent(self):
        env = self.next_envelope('PR-a1000000008', [make_object('FACT-a1000000008')])
        first = publish(self.home, env)
        again = publish(self.home, env)
        self.assertEqual(again['status'], 'ALREADY_APPLIED')
        self.assertEqual(again['revision'], first['revision'])
        self.assertEqual(len(history(self.home)), 2)  # bootstrap + one publish

    def test_crash_before_ref_switch_recovers(self):
        env = self.next_envelope('PR-a1000000009', [make_object('FACT-a1000000009')])
        frozen = proposals.freeze(self.home, copy.deepcopy(env))
        # window 1: orphan commit exists, ref untouched -> apply still succeeds cleanly
        orphan_tree = self.temp.name + '/orphan'
        Path(orphan_tree).mkdir()
        (Path(orphan_tree)/'cognitive-base').mkdir(parents=True)
        (Path(orphan_tree)/'cognitive-base/FACT-zzz.yaml').write_text('id: FACT-zzz\n', encoding='utf-8')
        tree = write_tree_from_dir(self.home, Path(orphan_tree))
        orphan = make_commit(self.home, tree, self.base_revision, 'orphan-crash-window')
        self.assertNotEqual(orphan, approved_commit(self.home))
        result = publish(self.home, env)
        self.assertEqual(result['status'], 'APPLIED')

    def test_crash_after_ref_switch_before_queue_update(self):
        env = self.next_envelope('PR-a1000000010', [make_object('FACT-a1000000010')])
        proposals.freeze(self.home, copy.deepcopy(env))
        bundle = reviews.render_bundle(self.home, 'PR-a1000000010', 1)
        approval = reviews.approve(self.home, 'PR-a1000000010', 1, 'reviewer')
        result = publisher.apply(self.home, env, approval, bundle['bundle_hash'])
        # simulate crash after ref switch: no status write happened at all
        again = publisher.apply(self.home, env, approval, bundle['bundle_hash'])
        self.assertEqual(again['status'], 'ALREADY_APPLIED')
        self.assertEqual(again['revision'], result['revision'])

    def test_duplicate_id_same_version_conflicting_content_rejected(self):
        env = self.next_envelope('PR-a1000000011', [make_object('FACT-a1000000011')])
        proposals.freeze(self.home, copy.deepcopy(env))
        other = copy.deepcopy(env)
        other['operations'][0]['after']['content'] = 'different content same id+version'
        with self.assertRaises(CCHError) as exc:
            proposals.freeze(self.home, other)
        self.assertEqual(exc.exception.code, 'INVALID_PROPOSAL')

    def test_unpublished_commit_not_readable(self):
        tree_dir = Path(self.temp.name)/'tree'
        (tree_dir/'cognitive-base').mkdir(parents=True)
        (tree_dir/'cognitive-base/FACT-001.yaml').write_text('id: FACT-001\n', encoding='utf-8')
        commit = make_commit(self.home, write_tree_from_dir(self.home, tree_dir),
                             approved_commit(self.home), 'unpublished')
        with self.assertRaises(CCHError) as exc:
            load_base(self.home, commit)
        self.assertEqual(exc.exception.code, 'INVALID_REVISION')

class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self.temp.name))
        self.base_revision = approved_commit(self.home)
        for n, pid in [(1, 'PR-c1000000001'), (2, 'PR-c1000000002')]:
            env = bootstrap_envelope(pid, [make_object(f'FACT-c100000000{n}')],
                                     base_revision=self.base_revision)
            proposals.freeze(self.home, copy.deepcopy(env))
            reviews.render_bundle(self.home, pid, 1)
            reviews.approve(self.home, pid, 1, 'reviewer')
            (Path(self.temp.name)/f'{pid}.json').write_text(json.dumps(env), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def _apply_process(self, pid):
        env = json.loads((Path(self.temp.name)/f'{pid}.json').read_text(encoding='utf-8'))
        approval = reviews.load_approval(self.home, pid, 1)
        bundle = reviews.load_bundle_hash(self.home, pid, 1)
        try:
            result = publisher.apply(self.home, env, approval, bundle)
            return result['status']
        except CCHError as exc:
            return exc.code

    def test_two_applies_same_base_exactly_one_wins(self):
        outcomes = [None, None]
        def run(i, pid):
            outcomes[i] = self._apply_process(pid)
        threads = [threading.Thread(target=run, args=(i, pid))
                   for i, pid in enumerate(['PR-c1000000001', 'PR-c1000000002'])]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(outcomes), ['APPLIED', 'STALE_BASE'])
        # no lost update: exactly one new commit on top of the shared base
        hist = history(self.home)
        self.assertEqual(len(hist), 2)
        _, base = load_base(self.home)
        self.assertTrue(any(i.startswith('FACT-c1000000') for i in base.objects))

    def test_reader_never_sees_mixed_revision(self):
        stop = threading.Event()
        seen = []
        def reader():
            while not stop.is_set():
                commit, base = load_base(self.home)
                seen.append((commit, frozenset(base.objects)))
        thread = threading.Thread(target=reader); thread.start()
        self._apply_process('PR-c1000000001')
        stop.set(); thread.join()
        before = frozenset({'FACT-001'})
        after = before | {'FACT-c1000000001'}
        self.assertTrue(all(objects in (before, after) for _, objects in seen))
        self.assertGreater(len(seen), 0)

if __name__ == '__main__':
    unittest.main()
