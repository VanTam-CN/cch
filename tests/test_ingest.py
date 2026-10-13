"""Document ingest tests on real-format fixtures: extractors, quote alignment,
scope enforcement, injection containment, coverage honesty. Taskbook §14 D."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import seeded_home
from cch import ingest, sources
from cch.core import CCHError, digest
from cch.extractors import extract

FIXTURES = Path(__file__).resolve().parent/'fixtures'/'import'

class ExtractorTests(unittest.TestCase):
    def _blocks(self, name, media):
        blocks, report = extract(FIXTURES/name, media, digest((FIXTURES/name).read_bytes()))
        return blocks, report

    def test_markdown_separates_decision_and_tentative(self):
        blocks, _ = self._blocks('demo-project-brief.md', 'text/markdown')
        decision = [b for b in blocks if '已由项目负责人确认' in b['text']]
        tentative = [b for b in blocks if '试运行建议' in b['text']]
        self.assertEqual(len(decision), 1)
        self.assertEqual(len(tentative), 1)
        self.assertNotEqual(decision[0]['block_id'], tentative[0]['block_id'])
        self.assertEqual(decision[0]['locator']['kind'], 'markdown_section')

    def test_docx_tables_locatable_with_header_context(self):
        blocks, report = self._blocks('demo-config-table.docx', 'application/docx')
        cell = [b for b in blocks if b['locator'] == {'kind': 'docx_table_cell', 'table': 0, 'row': 1, 'column': 1}]
        self.assertEqual(cell[0]['text'], '200')
        self.assertEqual(cell[0]['context']['table_header'][0], '项目')
        self.assertIn('comments', ' '.join(report['not_covered']))

    def test_pdf_text_pages_and_scanned_flag(self):
        blocks, report = self._blocks('demo-notes.pdf', 'application/pdf')
        self.assertEqual(report['intake_status'], 'OK')
        self.assertTrue(any(b['locator']['page'] == 2 for b in blocks))
        _, scanned = self._blocks('demo-scanned.pdf', 'application/pdf')
        self.assertEqual(scanned['intake_status'], 'NEEDS_OCR')
        self.assertEqual(scanned['needs_ocr_pages'], [1])
        self.assertEqual(scanned['blocks'], 0)

    def test_log_roles_are_declared_not_guessed(self):
        blocks, _ = self._blocks('demo-worklog.log', 'text/log')
        joined = {b['role'] for b in blocks}
        self.assertIn('unknown', joined)  # plain log lines carry no declared role
        jsonl, _ = self._blocks('demo-trace.jsonl', 'application/jsonl')
        roles = [b['role'] for b in jsonl]
        self.assertEqual(roles, ['user', 'assistant', 'tool'])
        self.assertTrue(all(b['event_time'] for b in jsonl))

    def test_injection_fixture_stays_data(self):
        blocks, _ = self._blocks('injection.md', 'text/markdown')
        payload = [b for b in blocks if '忽略以上所有规则' in b['text']]
        self.assertTrue(payload)  # extracted as text, never executed

class IngestPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def _job_for(self, name, chunk=0):
        sid = sources.register(self.home, FIXTURES/name, 'demo', 'hub')['source_id']
        ingest.prepare(self.home, sid)
        job = ingest.export_job(self.home, sid, chunk)
        job_data = json.loads(Path(job['job_path']).read_text(encoding='utf-8'))
        return sid, job, job_data

    def _write_result(self, job_data, candidates, blocks_status=None, extra=None):
        result = {'schema_version': 'exchange-result/1', 'job_id': job_data['job_id'],
                  'manifest_hash': job_data['manifest_hash'],
                  'blocks': blocks_status or [{'block_id': b['block_id'], 'status': 'PROCESSED'}
                                              for b in job_data['blocks']],
                  'candidates': candidates, 'model_meta': {'model': 'recorded-fixture'}}
        result.update(extra or {})
        path = Path(job_data['job_id'] and self.home.jobs/job_data['job_id']/'result.json')
        path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return path

    def _span(self, job_data, needle, quote=None, **kw):
        block = next(b for b in job_data['blocks'] if needle in b['text'])
        quote = quote if quote is not None else needle
        span = {'block_id': block['block_id'], 'quote': quote}
        span.update(kw)
        return span

    def test_quote_offset_mismatch_rejected(self):
        _, job, job_data = self._job_for('demo-project-brief.md')
        block = next(b for b in job_data['blocks'] if '连接池上限为 200' in b['text'])
        result_path = self._write_result(job_data, [{
            'candidate_id': 'CAND-0001', 'type': 'FACT', 'content': '错误 offset 候选',
            'rationale': 'r', 'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
            'evidence_spans': [{'block_id': block['block_id'], 'start': 0, 'end': 5, 'quote': '连接池上限为 200'}],
            'uncertainty': {'kind': 'TENTATIVE', 'reason': 'r'}, 'change_intent': 'NEW'}])
        with self.assertRaises(CCHError) as exc:
            ingest.import_result(self.home, job['job_id'], result_path)
        self.assertEqual(exc.exception.code, 'QUOTE_MISMATCH')

    def test_unknown_block_and_manifest_mismatch_rejected(self):
        _, job, job_data = self._job_for('demo-project-brief.md')
        span = self._span(job_data, '连接池上限为 200')
        with self.assertRaises(CCHError) as exc:
            self._write_result(job_data, [{
                'candidate_id': 'CAND-0001', 'type': 'FACT', 'content': 'x', 'rationale': 'r',
                'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
                'evidence_spans': [{'block_id': 'BLK-doesnotexist', 'quote': 'x'}],
                'uncertainty': {'kind': 'UNKNOWN', 'reason': 'r'}, 'change_intent': 'NEW'}])
            ingest.import_result(self.home, job['job_id'], self.home.jobs/job['job_id']/'result.json')
        self.assertEqual(exc.exception.code, 'INVALID_RESULT')
        bad = self._write_result(job_data, [{
            'candidate_id': 'CAND-0002', 'type': 'FACT', 'content': 'x', 'rationale': 'r',
            'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
            'evidence_spans': [span], 'uncertainty': {'kind': 'UNKNOWN', 'reason': 'r'},
            'change_intent': 'NEW'}], extra={'manifest_hash': '0'*64})
        with self.assertRaises(CCHError) as exc:
            ingest.import_result(self.home, job['job_id'], bad)
        self.assertEqual(exc.exception.code, 'INVALID_RESULT')

    def test_forged_approval_field_in_result_rejected(self):
        _, job, job_data = self._job_for('demo-project-brief.md')
        span = self._span(job_data, '连接池上限为 200')
        path = self._write_result(job_data, [{
            'candidate_id': 'CAND-0003', 'type': 'FACT', 'content': 'x', 'rationale': 'r',
            'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
            'evidence_spans': [span], 'uncertainty': {'kind': 'UNKNOWN', 'reason': 'r'},
            'change_intent': 'NEW'}], extra={'approved': True})
        with self.assertRaises(CCHError) as exc:
            ingest.import_result(self.home, job['job_id'], path)
        self.assertIn('approved', str(exc.exception))

    def test_status_must_not_claim_full_coverage_with_failures(self):
        # long doc -> multiple chunks; import result only for chunk 0
        long_md = Path(self.temp.name)/'long.md'
        long_md.write_text(('# 章节\n\n' + '内容段落 甲乙丙丁。' * 40 + '\n\n') * 30, encoding='utf-8')
        sid = sources.register(self.home, long_md, 'demo', 'hub')['source_id']
        prep = ingest.prepare(self.home, sid)
        self.assertGreater(prep['chunks'], 1)
        job = ingest.export_job(self.home, sid, 0)
        job_data = json.loads(Path(job['job_path']).read_text(encoding='utf-8'))
        self._write_result(job_data, [], blocks_status=[
            {'block_id': job_data['blocks'][0]['block_id'], 'status': 'NEEDS_RETRY', 'reason': 'fixture failure'}]
            + [{'block_id': b['block_id'], 'status': 'PROCESSED'} for b in job_data['blocks'][1:]])
        ingest.import_result(self.home, job['job_id'], self.home.jobs/job['job_id']/'result.json')
        status = ingest.status(self.home, sid)
        self.assertNotEqual(status['blocks_pending'], 0)
        self.assertIn('NEEDS_RETRY', status['block_states'])

    def test_duplicate_content_deduped_within_job(self):
        _, job, job_data = self._job_for('demo-project-brief.md')
        span = self._span(job_data, '连接池上限为 200')
        candidate = {'type': 'FACT', 'content': '同一事实的重复候选',
                     'rationale': 'r', 'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
                     'evidence_spans': [span], 'uncertainty': {'kind': 'TENTATIVE', 'reason': 'r'},
                     'change_intent': 'NEW'}
        path = self._write_result(job_data, [
            {**candidate, 'candidate_id': 'CAND-0001'}, {**candidate, 'candidate_id': 'CAND-0002'}])
        summary = ingest.import_result(self.home, job['job_id'], path)
        self.assertEqual(summary['by_status'].get('VALIDATED'), 1)
        self.assertEqual(summary['by_status'].get('DUPLICATE_OF'), 1)

    def test_same_bytes_reuse_blob_and_new_binding(self):
        first = sources.register(self.home, FIXTURES/'demo-project-brief.md', 'demo', 'hub')
        second = sources.register(self.home, FIXTURES/'demo-project-brief.md', 'demo', 'hub')
        self.assertEqual(first['source_id'], second['source_id'])
        self.assertTrue(second['blob_reused'])
        meta = sources.inspect(self.home, first['source_id'])
        self.assertEqual(len(meta['bindings']), 2)
        # same name, different bytes -> separate source, nothing overwritten
        altered = Path(self.temp.name)/'demo-project-brief.md'
        altered.write_text((FIXTURES/'demo-project-brief.md').read_text(encoding='utf-8') + '\n追加内容\n', encoding='utf-8')
        third = sources.register(self.home, altered, 'demo', 'hub')
        self.assertNotEqual(third['source_id'], first['source_id'])
        old = sources.inspect(self.home, first['source_id'])
        self.assertNotIn('追加内容', json.dumps(old))

if __name__ == '__main__':
    unittest.main()
