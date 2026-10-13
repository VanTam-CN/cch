"""C0-C evidence collector tests: governance filtering, provenance-bound
sources, frozen Top-K artifacts, atomicity, zero Cognitive Base writes.

Synthetic office-domain content only (equipment archive / procurement
approval); no benchmark vocabulary.
"""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cch.core import CCHError
from cch.authoring import collector, models, planner_exchange
from cch.authoring import brief as brief_mod
from cch.store import approved_commit
from tests.test_authoring_contracts import draft_brief, plan
from tests.util import bootstrap_envelope, make_object, publish, seeded_home


EVIDENCE_ID = "EV-arch00000001"


class CollectorFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self._tmp.name) / "home")
        self._publish_subject_object()
        self.run_id = self._start_run()

    def tearDown(self):
        self._tmp.cleanup()

    def _publish_subject_object(self):
        obj = make_object("FACT-arch00000001",
                          content="设备归档 需部门负责人审批后提交档案室")
        obj["scope"] = {"domain": "demo", "project": "m2", "topic": ["归档"]}
        obj["provenance"] = {"evidence_refs": [EVIDENCE_ID]}
        env = bootstrap_envelope("PR-arch00000001", [obj],
                                scope={"domain": "demo", "project": "m2", "topic": []},
                                 base_revision=approved_commit(self.home),
                                 evidence_assets=[{
                                     "id": EVIDENCE_ID,
                                     "summary": "归档制度说明摘录",
                                     "source_text": "设备归档须部门负责人审批后提交档案室。",
                                     "source": {"source_id": "src-1",
                                                "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                                "source_name": "设备管理制度.md",
                                                "block_id": "BLK-3",
                                                "spans": [[0, 8]]}}],
                                 submitter={"agent_id": "t"})
        publish(self.home, env)

    def _start_run(self):
        brief = draft_brief(scope={"domain": "demo", "project": "m2"},
                            requested_topics=["归档"])
        brief_path = Path(self._tmp.name) / "brief.json"
        brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        result = brief_mod.init_run(self.home, brief_path)
        run_id = result["run_id"]
        planner_exchange.build_plan_job(self.home, run_id)
        job = models.load_json(models.run_dir_for(self.home, run_id) / "plan-job.json")
        result_doc = plan()
        result_doc.pop("brief_id")  # strict result schema: job carries it
        result_doc["schema"] = "authoring-plan-result/1"
        result_doc["job_id"] = job["job_id"]
        result_doc["manifest_sha256"] = job["manifest_sha256"]
        result_path = Path(self._tmp.name) / "plan-result.json"
        result_path.write_text(json.dumps(result_doc, ensure_ascii=False), encoding="utf-8")
        planner_exchange.import_plan_result(self.home, run_id, result_path)
        return run_id

    def _artifact_asset(self, home):
        from cch.artifact import index as aindex
        conn = aindex.connect(home.root)
        aid = aindex.mint_asset_id(conn)
        now = aindex.datetime.now(aindex.timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO assets (asset_id, kind, mime, size_bytes, sha256, source_file, "
            "page_or_slide, embedded_ref, document_sha256, document_title, domain, project, "
            "topics, asset_status, index_state, title, summary, extracted_text, entities, "
            "concepts, aliases, visual_type, enrichment_method, enrichment_job_id, observed_at, created_at, modified_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, "image", "image/png", 10, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "设备归档流程.png", None, None,
             "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "设备管理制度附件", "demo", "m2",
             '["归档", "流程"]', "unknown", "indexed", "设备归档流程图",
             "说明设备归档从部门发起至档案室入库的流程。", "[]", '["归档"]',
             '["设备归档", "审批流程", "档案室"]', "[]", "flowchart",
             "test-fixture", "JOB-X", "", now, now))
        conn.commit()
        return aid

    def test_collect_full_chain(self):
        aid = self._artifact_asset(self.home)
        out = collector.collect(self.home, self.run_id)
        self.assertEqual(out["state"], "EVIDENCE_FROZEN")
        run_dir = models.run_dir_for(self.home, self.run_id)
        pkg = models.load_json(run_dir / "evidence-package.json")
        sec = pkg["sections"][0]
        self.assertEqual(sec["section_id"], "S01")
        self.assertEqual(len(sec["cognition"]), 1)
        cog = sec["cognition"][0]
        self.assertEqual(cog["object_id"], "FACT-arch00000001")
        self.assertEqual(cog["status"], "ACTIVE")
        self.assertEqual(cog["source_refs"], [EVIDENCE_ID])
        self.assertEqual(len(sec["sources"]), 1)
        src = sec["sources"][0]
        self.assertEqual(src["evidence_id"], EVIDENCE_ID)
        self.assertEqual(src["source_sha256"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(src["currentness"], "not_assessed")
        self.assertEqual(src["currentness_basis"], "referenced_by_current_cognition")
        self.assertTrue(sec["artifacts"])
        self.assertIn(aid, [x["asset_id"] for x in sec["artifacts"]])
        art = [x for x in sec["artifacts"] if x["asset_id"] == aid][0]
        self.assertEqual(art["asset_status"], "unknown")
        self.assertEqual(art["asset_sha256"], "b" * 64)
        self.assertEqual(art["source_sha256"], "c" * 64)
        self.assertEqual(art["query_rank"], 1)
        self.assertEqual(art["query"], "归档流程图")
        self.assertEqual(art["query_index"], 1)
        self.assertTrue(art["why"])
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "EVIDENCE_FROZEN")
        self.assertEqual(manifest["evidence_package_sha256"], out["evidence_package_sha256"])

    def test_collect_freezes_package_no_recompute(self):
        self._artifact_asset(self.home)
        first = collector.collect(self.home, self.run_id)
        run_dir = models.run_dir_for(self.home, self.run_id)
        pkg_path = run_dir / "evidence-package.json"
        bytes_before = pkg_path.read_bytes()
        with self.assertRaises(CCHError) as ctx:
            collector.collect(self.home, self.run_id)
        self.assertEqual(ctx.exception.code, "INVALID_STATE")
        self.assertEqual(pkg_path.read_bytes(), bytes_before)
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["evidence_package_sha256"],
                         first["evidence_package_sha256"])

    def test_unmatched_artifact_query_recorded_in_observations(self):
        run_dir = models.run_dir_for(self.home, self.run_id)
        job = models.load_json(run_dir / "plan-job.json")
        result_doc = plan([{
            "section_id": "S01",
            "purpose": "无命中查询观察",
            "audience_question": "不相关查询会怎样？",
            "required_claim_types": ["FACT_CURRENT"],
            "retrieval_queries": {"cognition": ["归档流程 启动"],
                                   "artifact": ["完全无关关键词xyz"]},
        }])
        result_doc.pop("brief_id")
        result_doc["schema"] = "authoring-plan-result/1"
        result_doc["job_id"] = job["job_id"]
        result_doc["manifest_sha256"] = job["manifest_sha256"]
        result_path = Path(self._tmp.name) / "plan-result-unmatched.json"
        result_path.write_text(json.dumps(result_doc, ensure_ascii=False), encoding="utf-8")
        planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        out = collector.collect(self.home, self.run_id)
        pkg = models.load_json(run_dir / "evidence-package.json")
        sec = pkg["sections"][0]
        self.assertEqual(sec["artifacts"], [])
        obs = pkg.get("retrieval_observations", [])
        self.assertTrue(any(
            o.get("observation") == "search returned no candidates"
            and o.get("query") == "完全无关关键词xyz"
            for o in obs), obs)
        self.assertEqual(out["totals"]["missing"], 0)

    def test_superseded_asset_excluded(self):
        aid = self._artifact_asset(self.home)
        from cch.artifact import index as aindex
        conn = aindex.connect(self.home.root)
        conn.execute("UPDATE assets SET asset_status=? WHERE asset_id=?", ("superseded", aid))
        conn.commit()
        collector.collect(self.home, self.run_id)
        run_dir = models.run_dir_for(self.home, self.run_id)
        pkg = models.load_json(run_dir / "evidence-package.json")
        sec = pkg["sections"][0]
        self.assertNotIn(aid, [x["asset_id"] for x in sec["artifacts"]])

    def test_cross_scope_object_not_collected(self):
        obj = make_object("FACT-other000001", content="其他域流程 不应出现在归档包里")
        obj["scope"] = {"domain": "elsewhere", "project": "other", "topic": []}
        env = bootstrap_envelope("PR-other000001", [obj],
                                scope={"domain": "elsewhere", "project": "other", "topic": []},
                                base_revision=approved_commit(self.home),
                                submitter={"agent_id": "t"})
        publish(self.home, env)
        # base moved -> the setUp run is stale by design; start a fresh run
        # frozen against the new revision, then collect from it.
        self.run_id = self._start_run()
        collector.collect(self.home, self.run_id)
        run_dir = models.run_dir_for(self.home, self.run_id)
        pkg = models.load_json(run_dir / "evidence-package.json")
        cogs = [c for s in pkg["sections"] for c in s["cognition"]]
        self.assertNotIn("FACT-other000001", [c["object_id"] for c in cogs])

    def test_cognition_without_sources_recorded_missing(self):
        obj = make_object("FACT-nosrc000001", content="无来源事实 归档例外情形")
        obj["scope"] = {"domain": "demo", "project": "m2", "topic": ["归档"]}
        obj["provenance"] = {"evidence_refs": []}
        env = bootstrap_envelope("PR-nosrc000001", [obj],
                                scope={"domain": "demo", "project": "m2", "topic": []},
                                base_revision=approved_commit(self.home),
                                submitter={"agent_id": "t"})
        publish(self.home, env)
        self.run_id = self._start_run()
        collector.collect(self.home, self.run_id)
        run_dir = models.run_dir_for(self.home, self.run_id)
        pkg = models.load_json(run_dir / "evidence-package.json")
        sec = pkg["sections"][0]
        missing_refs = [m["ref"] for m in sec["missing"]]
        self.assertTrue(any("FACT-nosrc000001" in r for r in missing_refs), missing_refs)
        # the sourced object still resolves its provenance; the no-source
        # object contributes no source entries of its own
        self.assertFalse(any("nosrc" in s["derived_from"] for s in sec["sources"]))

    def test_base_move_blocks_collect(self):
        obj = make_object("FACT-move000001", content="新事实 使 base 前进")
        env = bootstrap_envelope("PR-move000001", [obj],
                                 base_revision=approved_commit(self.home),
                                 submitter={"agent_id": "t"})
        publish(self.home, env)
        with self.assertRaises(CCHError):
            collector.collect(self.home, self.run_id)


if __name__ == "__main__":
    unittest.main()
