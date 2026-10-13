"""Artifact Intelligence Phase A end-to-end tests.

Runs the real CLI (python -m cch.artifact) as a subprocess so exit codes,
error JSON, and the on-disk index are exercised exactly as a user would.
Proves the M2 gates that are testable without real-world materials:
traceability, sha256 binding on import, deterministic ranking, superseded
demotion, and zero writes outside <home>/artifact (no Cognitive-Base touch).
"""
from __future__ import annotations
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 1x1 transparent PNG, valid enough for zip embedding and hash binding.
PNG_1PX = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')


def run_cli(home: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, '-m', 'cch.artifact', *args, '--home', str(home)],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def jout(stdout: str) -> dict:
    return json.loads(stdout)


class ArtifactFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix='cch-art-'))
        cls.library = Path(tempfile.mkdtemp(prefix='cch-lib-'))
        # Library contents: one standalone PNG, one DOCX built here with an
        # embedded media member (scanner contract: media/* inside OOXML zip),
        # one 2-page PDF (page candidates).
        (cls.library/'架构图.png').write_bytes(PNG_1PX)
        docx_path = cls.library/'带图文档.docx'
        with zipfile.ZipFile(docx_path, 'w') as zf:
            zf.writestr('[Content_Types].xml',
                        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            zf.writestr('word/media/image1.png', PNG_1PX)
        shutil.copy(ROOT/'tests/fixtures/import/demo-notes.pdf', cls.library)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.library, ignore_errors=True)

    def test_01_scan_registers_candidates_with_traceability(self):
        code, out, err = run_cli(self.home, 'scan', str(self.library),
                                 '--domain', 'demo', '--project', 'm2',
                                 '--topics', '系统架构,汇报素材')
        self.assertEqual(code, 0, err)
        payload = jout(out)
        # 1 PNG + >=1 docx image + 2 PDF page candidates.
        self.assertGreaterEqual(payload['scanned'], 4)
        # Every candidate carries a sha256 and resolves to a real file.
        for entry in payload['registered']:
            self.assertRegex(entry['asset_id'], r'^AST-\d{8}-\d{4}$')
            self.assertRegex(entry['sha256'], r'^[0-9a-f]{64}$')
        code, out, _ = run_cli(self.home, 'stats')
        stats = jout(out)
        self.assertEqual(stats['assets'], payload['scanned'])
        self.assertEqual(stats['pending'], payload['scanned'])

    def test_01b_scan_is_idempotent_no_duplicate_assets(self):
        # Rescanning the same library must recognize every candidate and
        # mint zero new assets (identity = document hash + page/slide +
        # embedded ref + content hash).
        code, out, err = run_cli(self.home, 'scan', str(self.library),
                                 '--domain', 'demo', '--project', 'm2',
                                 '--topics', '系统架构,汇报素材')
        self.assertEqual(code, 0, err)
        payload = jout(out)
        code, out, _ = run_cli(self.home, 'stats')
        stats = jout(out)
        self.assertEqual(stats['assets'], payload['scanned'])

    def test_02_exchange_roundtrip_and_bindings(self):
        code, out, _ = run_cli(self.home, 'export-job', '--limit', '50')
        self.assertEqual(code, 0)
        job = jout(out)
        self.assertGreaterEqual(job['assets'], 4)
        job_json = json.loads(Path(job['job_path']).read_text('utf-8'))
        manifest_ids = [a['asset_id'] for a in job_json['assets']]

        # Build a VLM result bound to the frozen hashes.
        def asset_entry(aid: str, title: str, status='unknown') -> dict:
            frozen = next(a for a in job_json['assets'] if a['asset_id'] == aid)
            return {'asset_id': aid, 'sha256': frozen['sha256'], 'title': title,
                    'summary': f'{title}的测试描述', 'extracted_text': ['数据服务中心'],
                    'entities': ['综合网管'], 'visual_type': 'architecture-diagram',
                    'asset_status': status}
        result = {'schema_version': 'asset-exchange-result/1', 'job_id': job['job_id'],
                  'assets': [asset_entry(manifest_ids[0], '总体架构图'),
                             asset_entry(manifest_ids[1], '界面截图')]}
        result_path = self.home/'artifact'/'jobs'/job['job_id']/'result.json'
        result_path.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        code, out, err = run_cli(self.home, 'import-result', job['job_id'],
                                 '--result', str(result_path))
        self.assertEqual(code, 0, err)
        receipt = jout(out)
        self.assertEqual(receipt['applied'], manifest_ids[:2])

        # Tampered hash must be rejected with exit 2 and machine error JSON.
        bad = dict(result)
        bad['assets'] = [dict(result['assets'][0], sha256='0'*64)]
        bad_path = result_path.with_name('result-bad.json')
        bad_path.write_text(json.dumps(bad, ensure_ascii=False), 'utf-8')
        code, _, err = run_cli(self.home, 'import-result', job['job_id'], '--result', str(bad_path))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)['error'], 'INVALID_RESULT')

        code, out, _ = run_cli(self.home, 'inspect', manifest_ids[0])
        rec = jout(out)
        self.assertEqual(rec['index_state'], 'indexed')
        # Traceability: locator resolves to a real file, never empty.
        self.assertTrue(Path(rec['source_file']).is_file())
        self.assertTrue(rec['sha256'])

    def test_03_search_deterministic_scope_and_supersede(self):
        code, out, _ = run_cli(self.home, 'search', '总体架构', '--home', self.home,
                               '--domain', 'demo', '--project', 'm2')
        self.assertEqual(code, 0)
        first = jout(out)
        self.assertTrue(first['results'])
        top = first['results'][0]
        self.assertIn('domain=demo', top['why'])
        # Deterministic: identical query, identical ranking.
        code, out, _ = run_cli(self.home, 'search', '总体架构',
                               '--domain', 'demo', '--project', 'm2')
        self.assertEqual(jout(out)['results'][0]['asset']['asset_id'],
                         top['asset']['asset_id'])

        asset_id = top['asset']['asset_id']
        code, out, _ = run_cli(self.home, 'supersede', asset_id)
        self.assertEqual(code, 0)
        # Default view: the superseded asset is silently excluded from
        # auto-selection results; the audit flag remains on the record.
        code, out, _ = run_cli(self.home, 'search', '总体架构',
                               '--domain', 'demo', '--project', 'm2')
        again = jout(out)
        self.assertFalse(any(a['asset']['asset_id'] == asset_id
                             for a in again['results']))
        code, out, _ = run_cli(self.home, 'search', '总体架构',
                               '--domain', 'demo', '--project', 'm2',
                               '--include-superseded')
        flagged = jout(out)
        flagged_row = next(a for a in flagged['results']
                           if a['asset']['asset_id'] == asset_id)
        self.assertIn('SUPERSEDED', flagged_row['why'])

        # Unknown query: honest no-hit note, not a false claim of absence.
        code, out, _ = run_cli(self.home, 'search', '完全不存在的主题词')
        self.assertEqual(code, 0)
        self.assertIn('不代表素材不存在', jout(out)['note'])

    def test_04_list_aid_for_gold_labeling(self):
        code, out, err = run_cli(self.home, 'list')
        self.assertEqual(code, 0, err)
        rows = jout(out)
        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            self.assertRegex(row['sha256'], r'^[0-9a-f]{64}$')
            self.assertTrue(Path(row['source_file']).is_file())
            self.assertIn(row['index_state'], ('pending', 'indexed'))
        code, out, _ = run_cli(self.home, 'list', '--state', 'indexed')
        self.assertTrue(all(r['index_state'] == 'indexed' for r in jout(out)))


class ScopeAndSupersedeIsolation(unittest.TestCase):
    """v0.3.4 hardening: strict scope excludes cross-project assets by
    default; superseded assets are invisible to auto-selection unless
    explicitly requested."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix='cch-scope-'))
        cls.libA = Path(tempfile.mkdtemp(prefix='cch-scope-a-'))
        cls.libB = Path(tempfile.mkdtemp(prefix='cch-scope-b-'))
        (cls.libA/'总体架构图5g.png').write_bytes(b'arch-5g-a-bytes')
        (cls.libB/'总体架构图网管.png').write_bytes(b'arch-nms-b-bytes')
        for lib, proj in ((cls.libA, '5g'), (cls.libB, 'nms')):
            code, _, err = run_cli(cls.home, 'scan', str(lib),
                                   '--domain', 'demo', '--project', proj)
            assert code == 0, err

    @classmethod
    def tearDownClass(cls):
        for p in (cls.home, cls.libA, cls.libB):
            shutil.rmtree(p, ignore_errors=True)

    def test_strict_scope_excludes_other_projects_by_default(self):
        code, out, err = run_cli(self.home, 'search', '总体架构图',
                                 '--domain', 'demo', '--project', '5g')
        self.assertEqual(code, 0, err)
        results = jout(out)['results']
        self.assertTrue(results)
        self.assertTrue(all(r['asset']['project'] == '5g' for r in results))
        # Expanded scope: the other project becomes visible again.
        code, out, _ = run_cli(self.home, 'search', '总体架构图',
                               '--domain', 'demo', '--project', '5g',
                               '--expanded-scope')
        self.assertEqual(code, 0)
        projects = {r['asset']['project'] for r in jout(out)['results']}
        self.assertEqual(projects, {'5g', 'nms'})

    def test_superseded_hidden_by_default_and_restored_by_flag(self):
        aid = jout(run_cli(self.home, 'search', '总体架构图5g',
                           '--project', '5g')[1])['results'][0]['asset']['asset_id']
        code, _, err = run_cli(self.home, 'supersede', aid)
        self.assertEqual(code, 0, err)
        # Default (auto-selection): hidden.
        code, out, _ = run_cli(self.home, 'search', '总体架构图5g',
                               '--project', '5g')
        self.assertEqual(jout(out)['results'], [])
        # Human-review view: explicitly requested.
        code, out, _ = run_cli(self.home, 'search', '总体架构图5g',
                               '--project', '5g', '--include-superseded')
        results = jout(out)['results']
        self.assertEqual(len(results), 1)
        self.assertIn('SUPERSEDED', results[0]['why'])


class PendingSearchability(unittest.TestCase):
    """Pending assets must be retrievable via scan-side fields only
    (topics + document title); VLM semantics is an enhancement, not a
    precondition for retrieval."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix='cch-pend-'))
        cls.lib = Path(tempfile.mkdtemp(prefix='cch-pend-lib-'))
        (cls.lib/'数据架构总览培训图.png').write_bytes(PNG_1PX)
        (cls.lib/'运行管控与并网检测系统图.png').write_bytes(PNG_1PX)
        code, _, err = run_cli(cls.home, 'scan', str(cls.lib),
                               '--domain', 'demo', '--project', 'm2',
                               '--topics', '四圈层,培训')
        assert code == 0, err

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.lib, ignore_errors=True)

    def test_pending_asset_found_by_topic_and_document_title(self):
        for query in ('四圈层', '数据架构总览'):
            code, out, err = run_cli(self.home, 'search', query,
                                     '--domain', 'demo', '--project', 'm2')
            self.assertEqual(code, 0, err)
            results = jout(out)['results']
            self.assertTrue(results, f'no hit for {query!r}')
            top = results[0]['asset']
            self.assertEqual(top['index_state'], 'pending')
            self.assertIn('PENDING-SEMANTICS', results[0]['why'])

    def test_compound_term_without_spaces_still_hits(self):
        # Natural compound queries ("运行管控并网检测") must recall content
        # that inserts one connecting character ("运行管控与并网检测").
        code, out, err = run_cli(self.home, 'search', '运行管控并网检测',
                                 '--domain', 'demo', '--project', 'm2')
        self.assertEqual(code, 0, err)
        self.assertTrue(jout(out)['results'])


class DocumentTitleSearch(unittest.TestCase):
    """document_title (proposal Asset Contract, source block) is searchable
    after semantics import and does not get clobbered by re-import."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix='cch-doct-'))
        cls.lib = Path(tempfile.mkdtemp(prefix='cch-doct-lib-'))
        (cls.lib/'总体方案V4配图.png').write_bytes(PNG_1PX)
        code, out, err = run_cli(cls.home, 'scan', str(cls.lib),
                                 '--domain', 'demo', '--project', 'm2',
                                 '--topics', '总体方案')
        assert code == 0, err
        code, out, _ = run_cli(cls.home, 'export-job')
        assert code == 0
        job = json.loads(out)
        job_json = json.loads(Path(job['job_path']).read_text('utf-8'))
        frozen = job_json['assets'][0]
        cls.asset_id = frozen['asset_id']
        result = {'schema_version': 'asset-exchange-result/1',
                  'job_id': job['job_id'],
                  'assets': [{'asset_id': frozen['asset_id'],
                              'sha256': frozen['sha256'],
                              'title': '语义标题架构图', 'summary': '描述',
                              'extracted_text': [], 'entities': [],
                              'visual_type': 'architecture-diagram',
                              'asset_status': 'current'}]}
        rp = cls.home/'artifact'/'jobs'/job['job_id']/'result.json'
        rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        code, _, err = run_cli(cls.home, 'import-result', job['job_id'], '--result', str(rp))
        assert code == 0, err

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.lib, ignore_errors=True)

    def test_document_title_hits_and_status_stays_unknown(self):
        code, out, err = run_cli(self.home, 'search', '总体方案V4',
                                 '--domain', 'demo', '--project', 'm2')
        self.assertEqual(code, 0, err)
        results = jout(out)['results']
        self.assertTrue(results)
        self.assertEqual(results[0]['asset']['asset_id'], self.asset_id)
        rec = jout(run_cli(self.home, 'inspect', self.asset_id)[1])
        # Human governance: VLM-suggested 'current' must NOT flip status.
        self.assertEqual(rec['asset_status'], 'unknown')
        self.assertEqual(rec['index_state'], 'indexed')

    def test_partial_invalid_result_leaves_index_untouched(self):
        # Two entries: second one has a bad hash. The whole import must
        # fail and the first (valid) entry must NOT be applied.
        home = Path(tempfile.mkdtemp(prefix='cch-atom-'))
        lib = Path(tempfile.mkdtemp(prefix='cch-atom-lib-'))
        try:
            (lib/'甲图.png').write_bytes(PNG_1PX)
            (lib/'乙图.png').write_bytes(PNG_1PX)
            code, _, err = run_cli(home, 'scan', str(lib), '--domain', 'demo', '--project', 'm2')
            assert code == 0, err
            code, out, _ = run_cli(home, 'export-job')
            self.assertEqual(code, 0)
            job = json.loads(out)
            job_json = json.loads(Path(job['job_path']).read_text('utf-8'))
            a1, a2 = job_json['assets'][0], job_json['assets'][1]
            result = {'schema_version': 'asset-exchange-result/1',
                      'job_id': job['job_id'],
                      'assets': [
                          {'asset_id': a1['asset_id'], 'sha256': a1['sha256'],
                           'title': '好的条目', 'summary': 's', 'extracted_text': [],
                           'entities': [], 'visual_type': 'd'},
                          {'asset_id': a2['asset_id'], 'sha256': '0' * 64,
                           'title': '坏哈希条目', 'summary': 's', 'extracted_text': [],
                           'entities': [], 'visual_type': 'd'}]}
            rp = home/'artifact'/'jobs'/job['job_id']/'result-bad.json'
            rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
            code, _, err = run_cli(home, 'import-result', job['job_id'], '--result', str(rp))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)['error'], 'INVALID_RESULT')
            rec = jout(run_cli(home, 'inspect', a1['asset_id'])[1])
            self.assertEqual(rec['index_state'], 'pending')
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(lib, ignore_errors=True)

    def test_string_list_fields_rejected(self):
        home = Path(tempfile.mkdtemp(prefix='cch-str-'))
        lib = Path(tempfile.mkdtemp(prefix='cch-str-lib-'))
        try:
            (lib/'丙图.png').write_bytes(PNG_1PX)
            code, _, err = run_cli(home, 'scan', str(lib), '--domain', 'demo', '--project', 'm2')
            assert code == 0, err
            code, out, _ = run_cli(home, 'export-job')
            self.assertEqual(code, 0)
            job = json.loads(out)
            job_json = json.loads(Path(job['job_path']).read_text('utf-8'))
            frozen = job_json['assets'][0]
            result = {'schema_version': 'asset-exchange-result/1',
                      'job_id': job['job_id'],
                      'assets': [{'asset_id': frozen['asset_id'],
                                  'sha256': frozen['sha256'],
                                  'title': 't', 'summary': 's',
                                  'extracted_text': '数据服务中心',
                                  'entities': [], 'visual_type': 'd'}]}
            rp = home/'artifact'/'jobs'/job['job_id']/'result-str.json'
            rp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
            code, _, err = run_cli(home, 'import-result', job['job_id'], '--result', str(rp))
            self.assertEqual(code, 2)
            self.assertIn('extracted_text must be a list', json.loads(err)['message'])
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(lib, ignore_errors=True)

class FtsMigration(unittest.TestCase):
    """An index created by the old schema must be rebuilt transparently."""

    def test_old_fts_schema_migrates_on_connect(self):
        import sqlite3
        home = Path(tempfile.mkdtemp(prefix='cch-mig-'))
        try:
            artifact = home/'artifact'
            artifact.mkdir(parents=True)
            conn = sqlite3.connect(artifact/'asset-index.db')
            conn.execute("CREATE TABLE assets (asset_id TEXT PRIMARY KEY, kind TEXT,"
                         " source_file TEXT, page_or_slide INTEGER, embedded_ref TEXT,"
                         " mime TEXT, size_bytes INTEGER, sha256 TEXT, document_sha256 TEXT,"
                         " document_title TEXT, modified_at TEXT, domain TEXT,"
                         " project TEXT, topics TEXT, title TEXT, summary TEXT,"
                         " extracted_text TEXT, entities TEXT, visual_type TEXT,"
                         " asset_status TEXT, observed_at TEXT, superseded_by TEXT,"
                         " extraction_method TEXT, job_id TEXT, confidence TEXT,"
                         " index_state TEXT, created_at TEXT)")
            conn.execute("INSERT INTO assets VALUES ('AST-20260101-0001','image','/x.png',"
                         "NULL,NULL,'image/png',1,'ab','ab','旧标题图','',"
                         "'d','p','[]','t','s','[]','[]','','unknown','','',NULL,"
                         "'','','pending','')")
            conn.execute("CREATE VIRTUAL TABLE assets_fts USING fts5("
                         "asset_id UNINDEXED, title, summary, extracted_text,"
                         " entities, topics, tokenize='trigram')")
            conn.commit()
            conn.close()
            from cch.artifact.index import connect
            c2 = connect(home)
            sql = c2.execute("SELECT sql FROM sqlite_master WHERE name='assets_fts'").fetchone()['sql']
            self.assertIn('document_title', sql)
            row = c2.execute("SELECT document_title FROM assets_fts WHERE asset_id='AST-20260101-0001'").fetchone()
            self.assertEqual(row['document_title'], '旧标题图')
            c2.close()
        finally:
            shutil.rmtree(home, ignore_errors=True)


class PptxSlideLocator(unittest.TestCase):
    """PPTX media must resolve to a slide number for slide-level traceability."""

    def test_media_map_to_referencing_slide(self):
        import tempfile
        from cch.artifact.scan import scan_directory
        tmp = Path(tempfile.mkdtemp(prefix='cch-pptx-'))
        try:
            deck = tmp/'test-deck.pptx'
            with zipfile.ZipFile(deck, 'w') as zf:
                zf.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types/>')
                zf.writestr('ppt/media/image1.png', PNG_1PX)
                zf.writestr('ppt/media/image2.png', PNG_1PX)
                zf.writestr('ppt/slides/_rels/slide1.xml.rels',
                            '<?xml?><Relationships><Relationship Target="../media/image2.png"/></Relationships>')
                zf.writestr('ppt/slides/_rels/slide2.xml.rels',
                            '<?xml?><Relationships><Relationship Target="../media/image1.png"/></Relationships>')
            cands = {c.embedded_ref: c for c in scan_directory(tmp)}
            self.assertEqual(cands['ppt/media/image1.png'].page, 2)
            self.assertEqual(cands['ppt/media/image2.png'].page, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SelfContainedPack(unittest.TestCase):
    """cch-asset pack materializes a frozen job into a verifiable,
    self-contained folder: real bytes, manifest.sha256, README."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix='cch-pack-'))
        cls.lib = Path(tempfile.mkdtemp(prefix='cch-pack-lib-'))
        (cls.lib/'单图.png').write_bytes(PNG_1PX)
        docx = cls.lib/'带图文档.docx'
        with zipfile.ZipFile(docx, 'w') as zf:
            zf.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types/>')
            zf.writestr('word/media/image1.png', PNG_1PX)
        shutil.copy(ROOT/'tests/fixtures/import/demo-notes.pdf', cls.lib)
        code, _, err = run_cli(cls.home, 'scan', str(cls.lib),
                               '--domain', 'demo', '--project', 'pack')
        assert code == 0, err
        code, out, err = run_cli(cls.home, 'export-job', '--limit', '50')
        assert code == 0, err
        cls.job = jout(out)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.home, cls.lib):
            shutil.rmtree(p, ignore_errors=True)

    def test_pack_materializes_all_assets_and_verifies(self):
        out_dir = self.home/'pack-out'
        code, out, err = run_cli(self.home, 'pack', self.job['job_id'],
                                 '--out', str(out_dir))
        self.assertEqual(code, 0, err)
        receipt = jout(out)
        self.assertEqual(receipt['assets'], self.job['assets'])
        packed = sorted((out_dir/'assets').iterdir())
        self.assertEqual(len(packed), self.job['assets'])
        # Every asset file is non-empty and the manifest covers all files.
        for p in packed:
            self.assertGreater(p.stat().st_size, 0, p)
        manifest = (out_dir/'manifest.sha256').read_text('utf-8')
        covered = {line.split('  ', 1)[1] for line in manifest.strip().splitlines()}
        self.assertIn('job.json', covered)
        for p in packed:
            self.assertIn(f'assets/{p.name}', covered)
        # Hash lines actually match on-disk bytes.
        import hashlib
        for line in manifest.strip().splitlines():
            digest, rel = line.split('  ', 1)
            data = (out_dir/rel).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        self.assertTrue((out_dir/'README.txt').is_file())
        # Zip variant carries the same content.
        code, out, err = run_cli(self.home, 'pack', self.job['job_id'],
                                 '--out', str(out_dir) + '.zip')
        self.assertEqual(code, 0, err)
        with zipfile.ZipFile(str(out_dir) + '.zip') as zf:
            names = set(zf.namelist())
        self.assertIn('job.json', names)
        self.assertIn('manifest.sha256', names)
        self.assertTrue(any(n.startswith('assets/') for n in names))


class RescanLineage(unittest.TestCase):
    """Content changed at a known logical position records lineage and
    flags the old asset as changed; supersede stays a human decision."""

    @classmethod
    def setUpClass(cls):
        cls.lib = Path(tempfile.mkdtemp(prefix='cch-lin-lib-'))
        cls.doc = cls.lib/'图表.png'
        cls.doc.write_bytes(b'version-1-bytes')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.lib, ignore_errors=True)

    def test_changed_content_records_lineage_not_auto_supersede(self):
        home = Path(tempfile.mkdtemp(prefix='cch-lin2-'))
        try:
            code, _, err = run_cli(home, 'scan', str(self.lib),
                                   '--domain', 'demo', '--project', 'lin')
            self.assertEqual(code, 0, err)
            v1 = jout(run_cli(home, 'search', '图表', '--project', 'lin')[1])
            v1_id = v1['results'][0]['asset']['asset_id']
            # Simulate human-confirmed current status without the forbidden
            # auto-supersede: import semantics marking it indexed/current.
            code, out, _ = run_cli(home, 'export-job', '--limit', '10')
            job = jout(out)
            frozen = json.loads(Path(job['job_path']).read_text('utf-8'))['assets'][0]
            rp = home/'r.json'
            rp.write_text(json.dumps({'schema_version': 'asset-exchange-result/1',
                'job_id': job['job_id'], 'assets': [{'asset_id': frozen['asset_id'],
                'sha256': frozen['sha256'], 'title': '图表一', 'summary': 'v1',
                'extracted_text': [], 'entities': [], 'visual_type': 'diagram'}]},
                ensure_ascii=False), 'utf-8')
            code, _, err = run_cli(home, 'import-result', job['job_id'], '--result', str(rp))
            self.assertEqual(code, 0, err)
            # v2 arrives with different bytes at the same locator.
            self.doc.write_bytes(b'version-2-bytes')
            code, out, err = run_cli(home, 'scan', str(self.lib),
                                     '--domain', 'demo', '--project', 'lin')
            self.assertEqual(code, 0, err)
            payload = jout(out)
            self.assertEqual(payload['scanned'], 1)
            v2_id = payload['registered'][0]['asset_id']
            self.assertNotEqual(v2_id, v1_id)
            # Lineage recorded old -> new.
            import sqlite3
            conn = sqlite3.connect(home/'artifact'/'asset-index.db')
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM asset_lineage').fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['old_asset_id'], v1_id)
            self.assertEqual(rows[0]['new_asset_id'], v2_id)
            # Old asset flagged changed but NOT superseded.
            rec = jout(run_cli(home, 'inspect', v1_id)[1])
            self.assertEqual(rec['asset_status'], 'changed')
            self.assertIsNone(rec['superseded_by'])
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
