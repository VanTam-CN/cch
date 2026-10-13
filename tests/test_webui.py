"""Web UI backend end-to-end: bootstrap publish, daily ingest loop, query.

Runs the real HTTP server in-process on an ephemeral port and talks to it with
urllib. Proves the UI exposes the same contracts as the CLI (same error codes,
same CAS/approval semantics) without shelling out.
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys_path = str(Path(__file__).resolve().parents[1])
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from webui import server as ui  # noqa: E402


def call(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8'))


class WebUIFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        ui.UI.set_home(cls.temp.name)
        cls.httpd = ui.ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.temp.cleanup()

    def post(self, path, body):
        status, data = call(self.port, 'POST', path, body)
        self.assertEqual(status, 200, f'{path} -> {status}: {data}')
        return data

    def get(self, path):
        status, data = call(self.port, 'GET', path)
        self.assertEqual(status, 200, f'{path} -> {status}: {data}')
        return data

    def _bootstrap(self):
        self.post('/api/store/init', {})
        now = '2026-09-09T00:00:00Z'
        envelope = {
            'schema_version': 'proposal/1', 'proposal_id': 'PR-boot-001',
            'proposal_version': 1, 'base_revision': '0' * 40,
            'scope': {'domain': 'demo', 'project': 'first', 'topic': []},
            'submitter': {'agent_id': 'local-ui'}, 'origin': {'kind': 'manual'},
            'reason': 'bootstrap',
            'operations': [{'op': 'create', 'object_id': 'FACT-smoke-0001',
                            'expected_object_hash': None,
                            'after': {'id': 'FACT-smoke-0001', 'type': 'FACT',
                                      'status': 'ACTIVE',
                                      'scope': {'domain': 'demo', 'project': 'first', 'topic': []},
                                      'confidence': 'CONFIRMED',
                                      'authority': 'USER_CONFIRMED', 'priority': 'REFERENCE',
                                      'content': '演示项目使用 PostgreSQL 15。',
                                      'rationale': 'seed',
                                      'relations': {'supersedes': [], 'depends_on': [],
                                                    'conflicts_with': [], 'derived_from': []},
                                      'provenance': {'evidence_refs': []},
                                      'lifecycle': {'valid_from': now, 'valid_until': None,
                                                    'ttl_hours': None},
                                      'retrieval': {'keywords': [], 'aliases': []},
                                      'tags': ['bootstrap']}}],
            'evidence_assets': [], 'candidate_assets': [], 'risk_flags': [],
            'requested_at': now}
        self.post('/api/proposal/envelope', {'envelope': envelope})
        self.post('/api/review/render', {'proposal_id': 'PR-boot-001'})
        approval = self.post('/api/review/approve',
                             {'proposal_id': 'PR-boot-001', 'reviewer': 'tester'})
        self.assertEqual(approval['action'], 'approve')
        result = self.post('/api/apply', {'proposal_id': 'PR-boot-001'})
        self.assertEqual(result['status'], 'APPLIED')

    def test_01_page_served(self):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/', timeout=10) as res:
            html = res.read().decode('utf-8')
        self.assertIn('CCH 认知库控制台', html)

    def test_02_bootstrap_and_query(self):
        self._bootstrap()
        state = self.get('/api/state')
        self.assertEqual(state['store']['state'], 'OK')
        self.assertEqual([p['status'] for p in state['proposals']], ['APPLIED'])
        result = self.post('/api/agent/context',
                           {'query': '演示项目用什么数据库', 'domain': 'demo', 'project': 'first'})
        self.assertIn('run_id', result['receipt'])
        self.assertEqual(result['receipt']['knowledge_revision'],
                         state['store']['approved_revision'])
        self.assertIn('PostgreSQL', result['context_text'])

    def test_03_daily_ingest_loop(self):
        fixture = Path(__file__).parent / 'fixtures' / 'import' / 'demo-project-brief.md'
        add = self.post('/api/source/add',
                        {'name': fixture.name, 'domain': 'demo', 'project': 'first',
                         'content_hex': fixture.read_bytes().hex()})
        sid = add['source_id']
        prepare = self.post('/api/ingest/prepare', {'source_id': sid})
        self.assertGreater(prepare['chunks'], 0)
        job = self.post('/api/ingest/export-job', {'source_id': sid, 'chunk': 0})
        summary = self.post('/api/ingest/auto-result', {'job_id': job['job_id']})
        self.assertEqual(summary['blocks_reported'], summary['blocks_total'])
        self.assertGreater(summary['candidates'], 0)
        job_view = self.get('/api/job/' + job['job_id'])
        usable = [c for c in job_view['candidates']['candidates']
                  if c['status'] == 'VALIDATED' and c['change_intent'] == 'NEW']
        self.assertTrue(usable, 'local-direct candidates should validate')
        proposal = self.post('/api/proposal/from-job',
                             {'job_id': job['job_id'],
                              'candidate_ids': [c['candidate_id'] for c in usable]})
        pid = proposal['proposal_id']
        detail = self.get('/api/proposal/' + pid)
        self.assertEqual(detail['status']['status'], 'SUBMITTED')
        self.post('/api/review/render', {'proposal_id': pid})
        detail = self.get('/api/proposal/' + pid)
        self.assertEqual(detail['status']['status'], 'READY')
        self.assertIn('bundle_text', detail)
        self.post('/api/review/approve', {'proposal_id': pid, 'reviewer': 'tester'})
        result = self.post('/api/apply', {'proposal_id': pid})
        self.assertEqual(result['status'], 'APPLIED')
        state = self.get('/api/state')
        self.assertEqual(len(state['runs']), 1)


if __name__ == '__main__':
    unittest.main()
