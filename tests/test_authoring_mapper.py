"""C0-D mapper/storyboard behavior tests: exchange binding, strict
schema, package-scoped evidence citations, atomic import, one-way state
machine, deterministic storyboard derivation.

Office-domain synthetic fixtures only (equipment archive); the frozen
package is written exactly once by collector.collect in the base flow.
"""
from __future__ import annotations
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cch.core import CCHError
from cch.authoring import collector, mapper_exchange, models, \
    planner_exchange, verifier
from cch.authoring import brief as brief_mod
from cch.store import approved_commit
from tests.test_authoring_contracts import draft_brief, plan
from tests.util import bootstrap_envelope, make_object, publish, seeded_home


EVIDENCE_ID = "EV-map00000001"
OBJECT_ID = "FACT-map00000001"


def map_result(job, section_id="S01", cog_eid=None, src_eid=None, asset_id=None):
    evidence = []
    if cog_eid:
        evidence.append({"evidence_id": cog_eid, "relation": "supports"})
    if src_eid:
        evidence.append({"evidence_id": src_eid, "relation": "supports"})
    doc = {
        "schema": "authoring-map-result/2",
        "job_id": job["job_id"],
        "manifest_sha256": job["manifest_sha256"],
        "claims": [{
            "claim_id": "CLM-S01-001",
            "section_id": section_id,
            "claim_type": "FACT_CURRENT",
            "text": "设备归档须部门负责人审批后提交档案室。",
            "evidence": evidence,
            "confidence": "high",
            "currentness": "current",
        }],
        "visual_bindings": ([{
            "section_id": section_id,
            "asset_id": asset_id,
            "role": "primary_visual",
            "supports_claim_ids": ["CLM-S01-001"],
            "reason": "该图完整展示归档流程。",
        }] if asset_id else []),
        "unresolved": [],
    }
    return doc


class MapperFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self._tmp.name) / "home")
        self._publish_subject_object()
        self.run_id = self._start_run()
        collector.collect(self.home, self.run_id)

    def tearDown(self):
        self._tmp.cleanup()

    def _publish_subject_object(self):
        obj = make_object(OBJECT_ID,
                          content="设备归档 需部门负责人审批后提交档案室")
        obj["scope"] = {"domain": "demo", "project": "map", "topic": ["归档"]}
        obj["provenance"] = {"evidence_refs": [EVIDENCE_ID]}
        env = bootstrap_envelope("PR-map00000001", [obj],
                                 scope={"domain": "demo", "project": "map", "topic": []},
                                 base_revision=approved_commit(self.home),
                                 evidence_assets=[{
                                     "id": EVIDENCE_ID,
                                     "summary": "归档制度说明摘录",
                                     "source_text": "设备归档须部门负责人审批后提交档案室。",
                                     "source": {"source_id": "src-map-1",
                                                "source_sha256": "a" * 64,
                                                "source_name": "设备管理制度.md",
                                                "block_id": "BLK-3",
                                                "spans": [[0, 8]]}}],
                                 submitter={"agent_id": "t"})
        publish(self.home, env)

    def _start_run(self):
        brief = draft_brief(scope={"domain": "demo", "project": "map"},
                            requested_topics=["归档"],
                            brief_id="BRIEF-MAP-01")
        brief_path = Path(self._tmp.name) / "brief.json"
        brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        result = brief_mod.init_run(self.home, brief_path)
        run_id = result["run_id"]
        planner_exchange.build_plan_job(self.home, run_id)
        run_dir = models.run_dir_for(self.home, run_id)
        job = models.load_json(run_dir / "plan-job.json")
        doc = plan([{
            "section_id": "S01",
            "purpose": "说明归档流程入口",
            "audience_question": "归档从哪里开始？",
            "required_claim_types": ["FACT_CURRENT"],
            "retrieval_queries": {"cognition": ["归档流程 启动"],
                                   "artifact": ["归档流程图"]},
        }])
        doc.pop("brief_id")
        doc["schema"] = "authoring-plan-result/1"
        doc["job_id"] = job["job_id"]
        doc["manifest_sha256"] = job["manifest_sha256"]
        result_path = Path(self._tmp.name) / "plan-result.json"
        result_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        planner_exchange.import_plan_result(self.home, run_id, result_path)
        return run_id

    def _start_mapping(self):
        return mapper_exchange.build_map_job(self.home, self.run_id)

    def _package_ids(self):
        run_dir = models.run_dir_for(self.home, self.run_id)
        pkg = models.load_json(run_dir / "evidence-package.json")
        sec = pkg["sections"][0]
        artifacts = sec["artifacts"] or [None]
        return (sec["cognition"][0]["evidence_id"],
                sec["sources"][0]["evidence_id"],
                artifacts[0]["asset_id"] if artifacts[0] else None)

    def _write_map_result(self, doc):
        path = Path(self._tmp.name) / "map-result.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def test_full_map_and_storyboard_chain(self):
        job = self._start_mapping()
        cog_eid, src_eid, asset_id = self._package_ids()
        doc = map_result(job, cog_eid=cog_eid, src_eid=src_eid,
                         asset_id=asset_id)
        out = mapper_exchange.import_map_result(
            self.home, self.run_id, self._write_map_result(doc))
        self.assertEqual(out["state"], "MAP_FROZEN")
        self.assertEqual(out["claims"], 1)
        run_dir = models.run_dir_for(self.home, self.run_id)
        em = models.load_json(run_dir / "evidence-map.json")
        self.assertEqual(em["evidence_package_sha256"],
                         job["evidence_package_sha256"])
        sb_out = mapper_exchange.build_storyboard(self.home, self.run_id)
        self.assertEqual(sb_out["state"], "STORYBOARD_FROZEN")
        sb = models.load_json(run_dir / "storyboard.json")
        sec = sb["sections"][0]
        self.assertEqual(sec["section_id"], "S01")
        self.assertEqual(sec["claim_ids"], ["CLM-S01-001"])
        self.assertEqual(sec["headline"], "归档从哪里开始？")
        self.assertEqual(sec["transition_to_next"], "")
        if asset_id:
            self.assertEqual(sec["visuals"][0]["asset_id"], asset_id)
        else:
            self.assertEqual(sec["visuals"], [])
        self.assertEqual(sec["narrative_order"],
                         ["problem", "evidence", "implication"])
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "STORYBOARD_FROZEN")
        self.assertEqual(manifest["storyboard_sha256"], sb_out["storyboard_sha256"])

    def test_map_job_requires_frozen_package(self):
        run_dir = models.run_dir_for(self.home, self.run_id)
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "EVIDENCE_FROZEN")
        job = self._start_mapping()
        self.assertIn("evidence_package_sha256", job)
        with self.assertRaises(CCHError) as ctx:
            self._start_mapping()
        self.assertEqual(ctx.exception.code, "INVALID_STATE")

    def test_citation_of_unknown_evidence_rejected_atomically(self):
        job = self._start_mapping()
        doc = map_result(job, cog_eid="COG-nope-999")
        run_dir = models.run_dir_for(self.home, self.run_id)
        with self.assertRaises(CCHError):
            mapper_exchange.import_map_result(
                self.home, self.run_id, self._write_map_result(doc))
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "MAPPING")
        self.assertFalse((run_dir / "evidence-map.json").exists())

    def test_binding_to_unknown_asset_rejected(self):
        job = self._start_mapping()
        cog_eid, src_eid, _ = self._package_ids()
        doc = map_result(job, cog_eid=cog_eid, src_eid=src_eid,
                         asset_id="AST-00000000-0000")
        with self.assertRaises(CCHError):
            mapper_exchange.import_map_result(
                self.home, self.run_id, self._write_map_result(doc))

    def test_unknown_fields_rejected(self):
        job = self._start_mapping()
        cog_eid, src_eid, asset_id = self._package_ids()
        doc = map_result(job, cog_eid=cog_eid, src_eid=src_eid,
                         asset_id=asset_id)
        doc["answer"] = "模型自己写的答案"
        with self.assertRaises(CCHError):
            mapper_exchange.import_map_result(
                self.home, self.run_id, self._write_map_result(doc))
        doc2 = copy.deepcopy(map_result(job, cog_eid=cog_eid,
                                        src_eid=src_eid, asset_id=asset_id))
        doc2["claims"][0]["draft_text"] = "越权正文"
        with self.assertRaises(CCHError):
            mapper_exchange.import_map_result(
                self.home, self.run_id, self._write_map_result(doc2))

    def test_wrong_job_or_hash_rejected(self):
        job = self._start_mapping()
        cog_eid, src_eid, asset_id = self._package_ids()
        doc = map_result(job, cog_eid=cog_eid, src_eid=src_eid,
                         asset_id=asset_id)
        doc["job_id"] = "AUTHMAP-others"
        with self.assertRaises(CCHError):
            mapper_exchange.import_map_result(
                self.home, self.run_id, self._write_map_result(doc))
        doc2 = copy.deepcopy(map_result(job, cog_eid=cog_eid,
                                        src_eid=src_eid, asset_id=asset_id))
        doc2["manifest_sha256"] = "f" * 64
        with self.assertRaises(CCHError):
            mapper_exchange.import_map_result(
                self.home, self.run_id, self._write_map_result(doc2))

    def test_storyboard_requires_map_frozen(self):
        with self.assertRaises(CCHError) as ctx:
            mapper_exchange.build_storyboard(self.home, self.run_id)
        self.assertEqual(ctx.exception.code, "INVALID_STATE")


class SectionIsolationTests(unittest.TestCase):
    """D-RC: two-section run proving map-job self-containment,
    section-local binding, minimum evidence requirements, and the
    storyboard frozen-chain re-verification."""

    OBJ1 = "FACT-map00000003"
    OBJ2 = "FACT-map00000004"
    EV1 = "EV-map00000003"
    EV2 = "EV-map00000004"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = seeded_home(Path(self._tmp.name) / "home")
        self._publish_two_objects()
        self._insert_artifact()
        self.run_id = self._start_run()
        collector.collect(self.home, self.run_id)

    def tearDown(self):
        self._tmp.cleanup()

    def _publish_two_objects(self):
        o1 = make_object(self.OBJ1, content="设备归档流程 从部门发起")
        o1["scope"] = {"domain": "demo", "project": "map", "topic": ["归档"]}
        o1["retrieval"]["keywords"] = ["归档", "流程"]
        o1["provenance"] = {"evidence_refs": [self.EV1]}
        o2 = make_object(self.OBJ2, content="设备采购预算 需财务审批")
        o2["scope"] = {"domain": "demo", "project": "map", "topic": ["预算"]}
        o2["retrieval"]["keywords"] = ["预算", "审批"]
        o2["provenance"] = {"evidence_refs": [self.EV2]}
        env = bootstrap_envelope(
            "PR-map00000002", [o1, o2],
            scope={"domain": "demo", "project": "map", "topic": []},
            base_revision=approved_commit(self.home),
            evidence_assets=[
                {"id": self.EV1, "summary": "归档流程说明",
                 "source_text": "设备归档流程从部门发起。",
                 "source": {"source_id": "src-map-3", "source_sha256": "a" * 64,
                            "source_name": "归档制度.md", "block_id": "BLK-1",
                            "spans": [[0, 8]]}},
                {"id": self.EV2, "summary": "预算审批说明",
                 "source_text": "设备采购预算需财务审批。",
                 "source": {"source_id": "src-map-4", "source_sha256": "e" * 64,
                            "source_name": "预算制度.md", "block_id": "BLK-2",
                            "spans": [[0, 8]]}}],
            submitter={"agent_id": "t"})
        publish(self.home, env)

    def _insert_artifact(self):
        from cch.artifact import index as aindex
        conn = aindex.connect(self.home.root)
        aid = aindex.mint_asset_id(conn)
        now = aindex.datetime.now(aindex.timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO assets (asset_id, kind, mime, size_bytes, sha256, source_file, "
            "page_or_slide, embedded_ref, document_sha256, document_title, domain, project, "
            "topics, asset_status, index_state, title, summary, extracted_text, entities, "
            "concepts, aliases, visual_type, enrichment_method, enrichment_job_id, "
            "observed_at, created_at, modified_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, "image", "image/png", 10, "b" * 64, "设备归档流程.png", None, None,
             "c" * 64, "设备管理制度附件", "demo", "map", '["归档"]', "unknown",
             "indexed", "设备归档流程图", "说明设备归档流程。", "[]", '["归档"]',
             '["设备归档", "归档流程"]', "[]", "flowchart", "test-fixture",
             "JOB-X", "", now, now))
        conn.commit()
        return aid

    def _start_run(self):
        brief = draft_brief(scope={"domain": "demo", "project": "map"},
                            requested_topics=["归档", "预算"],
                            brief_id="BRIEF-MAP-2-01")
        brief_path = Path(self._tmp.name) / "brief.json"
        brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        result = brief_mod.init_run(self.home, brief_path)
        run_id = result["run_id"]
        planner_exchange.build_plan_job(self.home, run_id)
        run_dir = models.run_dir_for(self.home, run_id)
        job = models.load_json(run_dir / "plan-job.json")
        doc = plan([
            {
                "section_id": "S01",
                "purpose": "说明归档流程入口",
                "audience_question": "归档从哪里开始？",
                "required_claim_types": ["FACT_CURRENT"],
                "retrieval_queries": {"cognition": ["归档流程 发起"],
                                       "artifact": ["归档流程图"]},
            },
            {
                "section_id": "S02",
                "purpose": "说明预算审批要求",
                "audience_question": "预算怎么审批？",
                "required_claim_types": ["FACT_CURRENT"],
                "retrieval_queries": {"cognition": ["采购预算 财务要求"],
                                       "artifact": ["设备采购预算表"]},
            },
        ])
        doc.pop("brief_id")
        doc["schema"] = "authoring-plan-result/1"
        doc["job_id"] = job["job_id"]
        doc["manifest_sha256"] = job["manifest_sha256"]
        result_path = Path(self._tmp.name) / "plan-result.json"
        result_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        planner_exchange.import_plan_result(self.home, run_id, result_path)
        return run_id

    def _package_ids(self):
        run_dir = models.run_dir_for(self.home, self.run_id)
        pkg = models.load_json(run_dir / "evidence-package.json")
        s1, s2 = pkg["sections"]
        return {
            "s1_cog": s1["cognition"][0]["evidence_id"],
            "s1_src": s1["sources"][0]["evidence_id"],
            "s1_asset": s1["artifacts"][0]["asset_id"] if s1["artifacts"] else None,
            "s2_cog": s2["cognition"][0]["evidence_id"],
            "s2_src": s2["sources"][0]["evidence_id"],
            "s2_asset": s2["artifacts"][0]["asset_id"] if s2["artifacts"] else None,
        }

    def _valid_doc(self, job):
        ids = self._package_ids()
        doc = {
            "schema": "authoring-map-result/2",
            "job_id": job["job_id"],
            "manifest_sha256": job["manifest_sha256"],
            "claims": [
                {"claim_id": "CLM-S01-001", "section_id": "S01",
                 "claim_type": "FACT_CURRENT", "text": "归档流程从部门发起。",
                 "evidence": [{"evidence_id": ids["s1_cog"],
                               "relation": "supports"}],
                 "confidence": "high", "currentness": "current"},
                {"claim_id": "CLM-S02-001", "section_id": "S02",
                 "claim_type": "FACT_CURRENT", "text": "设备采购预算需财务审批。",
                 "evidence": [{"evidence_id": ids["s2_cog"],
                               "relation": "supports"}],
                 "confidence": "high", "currentness": "current"},
            ],
            "visual_bindings": [{
                "section_id": "S01", "asset_id": ids["s1_asset"],
                "role": "primary_visual",
                "supports_claim_ids": ["CLM-S01-001"],
                "reason": "该图展示归档流程。",
            }],
            "unresolved": [],
        }
        return doc, ids

    def _write_map_result(self, doc):
        path = Path(self._tmp.name) / "map-result-2.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def _import(self, doc):
        return mapper_exchange.import_map_result(
            self.home, self.run_id, self._write_map_result(doc))

    def test_map_job_self_contained_and_hash_bound(self):
        mapper_exchange.build_map_job(self.home, self.run_id)
        job = models.load_json(
            models.run_dir_for(self.home, self.run_id) / "map-job.json")
        self.assertEqual(job["brief"]["brief_id"], "BRIEF-MAP-2-01")
        self.assertEqual(len(job["plan"]["sections"]), 2)
        self.assertEqual(job["evidence_package"]["run_id"], self.run_id)
        manifest = models.read_manifest(
            models.run_dir_for(self.home, self.run_id))
        self.assertEqual(job["brief_sha256"], manifest["brief_sha256"])
        self.assertEqual(job["plan_sha256"], manifest["plan_sha256"])
        self.assertEqual(job["evidence_package_sha256"],
                         manifest["evidence_package_sha256"])
        self.assertEqual(models.canon_digest(job["brief"]),
                         manifest["brief_sha256"])
        self.assertEqual(models.canon_digest(job["plan"]),
                         manifest["plan_sha256"])
        self.assertEqual(models.canon_digest(job["evidence_package"]),
                         manifest["evidence_package_sha256"])

    def test_tampered_plan_rejected_before_job(self):
        run_dir = models.run_dir_for(self.home, self.run_id)
        plan_path = run_dir / "plan.json"
        original = plan_path.read_bytes()
        plan_doc = models.load_json(plan_path)
        plan_doc["sections"][0]["purpose"] = "被篡改的目的"
        models.write_json(plan_path, plan_doc)
        with self.assertRaises(CCHError):
            mapper_exchange.build_map_job(self.home, self.run_id)
        plan_path.write_bytes(original)
        job = mapper_exchange.build_map_job(self.home, self.run_id)
        self.assertTrue(job["job_id"].startswith("AUTHMAP-"))

    def test_section_local_evidence_and_source_steal_rejected(self):
        job = mapper_exchange.build_map_job(self.home, self.run_id)
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["evidence"][0]["evidence_id"] = ids["s2_cog"]
        with self.assertRaises(CCHError):
            self._import(doc)
        run_dir = models.run_dir_for(self.home, self.run_id)
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "MAPPING")
        self.assertFalse((run_dir / "evidence-map.json").exists())
        doc2, ids = self._valid_doc(job)
        doc2["claims"][0]["evidence"][0]["evidence_id"] = ids["s2_src"]
        with self.assertRaises(CCHError):
            self._import(doc2)

    def test_section_local_asset_and_claim_steal_rejected(self):
        job = mapper_exchange.build_map_job(self.home, self.run_id)
        doc, ids = self._valid_doc(job)
        doc["visual_bindings"][0]["section_id"] = "S02"
        with self.assertRaises(CCHError):
            self._import(doc)
        doc2, _ = self._valid_doc(job)
        doc2["visual_bindings"][0]["supports_claim_ids"] = ["CLM-S02-001"]
        with self.assertRaises(CCHError):
            self._import(doc2)

    def test_minimum_evidence_requirements(self):
        job = mapper_exchange.build_map_job(self.home, self.run_id)
        doc, _ = self._valid_doc(job)
        doc["claims"][0]["evidence"] = []
        with self.assertRaises(CCHError):
            self._import(doc)
        doc2, _ = self._valid_doc(job)
        doc2["visual_bindings"][0]["supports_claim_ids"] = []
        with self.assertRaises(CCHError):
            self._import(doc2)
        doc3, _ = self._valid_doc(job)
        doc3["visual_bindings"][0]["reason"] = "   "
        with self.assertRaises(CCHError):
            self._import(doc3)

    def test_storyboard_revalidates_frozen_chain_and_deterministic_fields(self):
        job = mapper_exchange.build_map_job(self.home, self.run_id)
        doc, _ = self._valid_doc(job)
        out = self._import(doc)
        self.assertEqual(out["state"], "MAP_FROZEN")
        run_dir = models.run_dir_for(self.home, self.run_id)
        map_path = run_dir / "evidence-map.json"
        pkg_path = run_dir / "evidence-package.json"
        orig_map = map_path.read_bytes()
        orig_pkg = pkg_path.read_bytes()
        em = models.load_json(map_path)
        em["claims"][0]["text"] = "篡改后的结论"
        models.write_json(map_path, em)
        with self.assertRaises(CCHError):
            mapper_exchange.build_storyboard(self.home, self.run_id)
        map_path.write_bytes(orig_map)
        pkg = models.load_json(pkg_path)
        pkg["sections"][0]["cognition"][0]["content"] = "篡改内容"
        models.write_json(pkg_path, pkg)
        with self.assertRaises(CCHError):
            mapper_exchange.build_storyboard(self.home, self.run_id)
        pkg_path.write_bytes(orig_pkg)
        sb_out = mapper_exchange.build_storyboard(self.home, self.run_id)
        self.assertEqual(sb_out["state"], "STORYBOARD_FROZEN")
        sb = models.load_json(run_dir / "storyboard.json")
        s1, s2 = sb["sections"]
        self.assertEqual(s1["headline"], "归档从哪里开始？")
        self.assertEqual(s1["transition_to_next"], "衔接下一部分：说明预算审批要求")
        self.assertEqual(s2["headline"], "预算怎么审批？")
        self.assertEqual(s2["transition_to_next"], "")

    def _valid_doc_for(self, job):
        return self._valid_doc(job)[0]

    def _issued_job(self):
        mapper_exchange.build_map_job(self.home, self.run_id)
        return models.load_json(
            models.run_dir_for(self.home, self.run_id) / "map-job.json")

    def _assert_import_blocked(self, job):
        run_dir = models.run_dir_for(self.home, self.run_id)
        doc = self._valid_doc_for(job)
        path = Path(self._tmp.name) / "map-result-tamper.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            mapper_exchange.import_map_result(self.home, self.run_id, path)
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "MAPPING")
        self.assertFalse((run_dir / "evidence-map.json").exists())

    def test_import_rejects_tampered_job_instructions(self):
        """Exchange integrity RC: issued map job is hash-bound in the
        manifest; any post-issuance edit must block import even when the
        result was regenerated to match the edited job."""
        job = self._issued_job()
        job_path = models.run_dir_for(self.home, self.run_id) / "map-job.json"
        original = job_path.read_bytes()
        tampered = models.load_json(job_path)
        tampered["instructions"]["claims_cite_package_evidence_only"] = False
        models.write_json(job_path, tampered)
        self._assert_import_blocked(tampered)
        job_path.write_bytes(original)

    def test_import_rejects_tampered_embedded_plan(self):
        job = self._issued_job()
        job_path = models.run_dir_for(self.home, self.run_id) / "map-job.json"
        original = job_path.read_bytes()
        tampered = models.load_json(job_path)
        tampered["plan"]["sections"][0]["purpose"] = "被替换的目的"
        models.write_json(job_path, tampered)
        self._assert_import_blocked(tampered)
        job_path.write_bytes(original)

    def test_import_rejects_wrong_run_id_job(self):
        job = self._issued_job()
        job_path = models.run_dir_for(self.home, self.run_id) / "map-job.json"
        original = job_path.read_bytes()
        tampered = models.load_json(job_path)
        tampered["run_id"] = "AUTH-20990101-999"
        models.write_json(job_path, tampered)
        self._assert_import_blocked(tampered)
        job_path.write_bytes(original)

    def _map_and_storyboard(self):
        job = self._issued_job()
        doc = self._valid_doc_for(job)
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)

    def test_verify_happy_path_ready_for_review(self):
        self._map_and_storyboard()
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["state"], "VERIFIED")
        self.assertEqual(out["status"], "READY_FOR_REVIEW")
        self.assertTrue(all(out["gates"].values()))
        run_dir = models.run_dir_for(self.home, self.run_id)
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "VERIFIED")
        self.assertEqual(manifest["verification_sha256"],
                         out["verification_sha256"])
        self.assertEqual(manifest["work_receipt_sha256"],
                         out["work_receipt_sha256"])
        vr = models.load_json(run_dir / "verification-report.json")
        self.assertEqual(vr["status"], "READY_FOR_REVIEW")
        self.assertEqual(vr["metrics"]["fact_evidence_coverage"], 1.0)
        self.assertEqual(vr["metrics"]["asset_traceability"], 1.0)
        self.assertNotEqual(vr["status"], "DONE")
        wr = models.load_json(run_dir / "work-receipt.json")
        self.assertEqual(wr["task_status"], "READY_FOR_REVIEW")
        self.assertEqual(wr["base_revision"], manifest["base_revision"])
        self.assertEqual(len(wr["base_revision"]), 40)
        for key in ("brief_sha256", "plan_sha256",
                    "evidence_package_sha256", "evidence_map_sha256",
                    "storyboard_sha256"):
            self.assertEqual(wr["artifacts"][key], manifest[key])

    def test_verify_blocked_when_fact_current_lacks_cognition(self):
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["evidence"] = [
            {"evidence_id": ids["s1_src"], "relation": "supports"}]
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "BLOCKED")
        self.assertFalse(out["gates"]["fact_current_backed_by_cognition"])
        run_dir = models.run_dir_for(self.home, self.run_id)
        vr = models.load_json(run_dir / "verification-report.json")
        self.assertTrue(vr["blockers"])
        wr = models.load_json(run_dir / "work-receipt.json")
        self.assertEqual(wr["task_status"], "BLOCKED")

    def test_verify_rejects_tampered_storyboard(self):
        self._map_and_storyboard()
        run_dir = models.run_dir_for(self.home, self.run_id)
        sb_path = run_dir / "storyboard.json"
        sb = models.load_json(sb_path)
        sb["sections"][0]["audience_takeaway"] = "被篡改的结论"
        models.write_json(sb_path, sb)
        with self.assertRaises(CCHError):
            verifier.verify(self.home, self.run_id)
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "STORYBOARD_FROZEN")
        self.assertFalse((run_dir / "verification-report.json").exists())

    def test_verify_twice_rejected(self):
        self._map_and_storyboard()
        verifier.verify(self.home, self.run_id)
        with self.assertRaises(CCHError) as ctx:
            verifier.verify(self.home, self.run_id)
        self.assertEqual(ctx.exception.code, "INVALID_STATE")

    def test_verify_requires_storyboard_frozen(self):
        with self.assertRaises(CCHError) as ctx:
            verifier.verify(self.home, self.run_id)
        self.assertEqual(ctx.exception.code, "INVALID_STATE")

    def test_verify_cli(self):
        self._map_and_storyboard()
        proc = subprocess.run(
            [sys.executable, "-m", "cch", "--home", str(self.home.root),
             "author", "verify", self.run_id],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "READY_FOR_REVIEW")

    def test_verify_cli_rejects_early_state(self):
        proc = subprocess.run(
            [sys.executable, "-m", "cch", "--home", str(self.home.root),
             "author", "verify", self.run_id],
            capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)

    # --- E-RC: unresolved participates in status; no silent sections ---

    def test_unresolved_only_section_reaches_verify_and_blocks(self):
        """Plan S02 has no evidence: mapper records unresolved instead of
        a claim; the storyboard keeps S02 visible; verify returns
        BLOCKED instead of crashing or silently going READY."""
        job = self._issued_job()
        ids = self._package_ids()
        doc = {
            "schema": "authoring-map-result/2",
            "job_id": job["job_id"],
            "manifest_sha256": job["manifest_sha256"],
            "claims": [
                {"claim_id": "CLM-S01-001", "section_id": "S01",
                 "claim_type": "FACT_CURRENT",
                 "text": "归档流程从部门发起。",
                 "evidence": [{"evidence_id": ids["s1_cog"],
                               "relation": "supports"}],
                 "confidence": "high", "currentness": "current"},
            ],
            "visual_bindings": [],
            "unresolved": [
                {"section_id": "S02", "kind": "missing_evidence",
                 "reason": "没有找到预算审批的当前证据"},
            ],
        }
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        run_dir = models.run_dir_for(self.home, self.run_id)
        sb = models.load_json(run_dir / "storyboard.json")
        self.assertEqual([s["section_id"] for s in sb["sections"]],
                         ["S01", "S02"])
        s2 = sb["sections"][1]
        self.assertEqual(s2["claim_ids"], [])
        self.assertEqual(s2["audience_takeaway"],
                         "证据不足，当前无法形成经验证结论")
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "BLOCKED")
        self.assertFalse(out["gates"]["no_unresolved_blockers"])
        vr = models.load_json(run_dir / "verification-report.json")
        self.assertTrue(any("missing_evidence" in b
                            for b in vr["blockers"]))
        wr = models.load_json(run_dir / "work-receipt.json")
        self.assertEqual(wr["task_status"], "BLOCKED")
        self.assertTrue(any("missing_evidence" in line for line
                            in wr["handoff"]["next_agent_should_know"]))

    def test_currentness_unconfirmed_resolves_to_partially_verified(self):
        job = self._issued_job()
        ids = self._package_ids()
        doc = self._valid_doc_for(job)
        doc["claims"] = doc["claims"][:1]
        doc["unresolved"] = [
            {"section_id": "S02", "kind": "currentness_unconfirmed",
             "reason": "预算页时效未确认"},
        ]
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "PARTIALLY_VERIFIED")
        self.assertTrue(out["gates"]["no_unresolved_blockers"])

    def test_full_unresolved_run_blocks_without_claims(self):
        """The negative P5 case: zero legal claims anywhere; the
        pipeline reaches verify and returns a governed BLOCKED."""
        job = self._issued_job()
        doc = {
            "schema": "authoring-map-result/2",
            "job_id": job["job_id"],
            "manifest_sha256": job["manifest_sha256"],
            "claims": [],
            "visual_bindings": [],
            "unresolved": [
                {"section_id": "S01", "kind": "missing_evidence",
                 "reason": "无归档证据"},
                {"section_id": "S02", "kind": "missing_evidence",
                 "reason": "无预算证据"},
            ],
        }
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "BLOCKED")
        run_dir = models.run_dir_for(self.home, self.run_id)
        sb = models.load_json(run_dir / "storyboard.json")
        self.assertEqual(len(sb["sections"]), 2)

    def test_storyboard_missing_plan_section_fails_gate(self):
        self._map_and_storyboard()
        run_dir = models.run_dir_for(self.home, self.run_id)
        sb_path = run_dir / "storyboard.json"
        sb = models.load_json(sb_path)
        dropped = sb["sections"][-1]["section_id"]
        sb["sections"] = sb["sections"][:-1]
        models.write_json(sb_path, sb)
        manifest = models.read_manifest(run_dir)
        manifest["storyboard_sha256"] = models.sha256_of(sb)
        models.write_json(run_dir / "manifest.json", manifest)
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "BLOCKED")
        self.assertFalse(out["gates"]["all_plan_sections_accounted_for"])
        vr = models.load_json(run_dir / "verification-report.json")
        self.assertTrue(any(dropped in b for b in vr["blockers"]))

    def test_import_rejects_unresolved_outside_plan(self):
        job = self._issued_job()
        doc = self._valid_doc_for(job)
        doc["unresolved"] = [
            {"section_id": "S99", "kind": "missing_evidence",
             "reason": "x"}]
        with self.assertRaises(CCHError):
            self._import(doc)

    def test_import_rejects_empty_result_and_bad_unresolved(self):
        job = self._issued_job()
        base = self._valid_doc_for(job)
        empty = copy.deepcopy(base)
        empty["claims"] = []
        empty["unresolved"] = []
        with self.assertRaises(CCHError):
            self._import(empty)
        bad_kind = copy.deepcopy(base)
        bad_kind["unresolved"] = [
            {"section_id": "S02", "kind": "vibes", "reason": "x"}]
        with self.assertRaises(CCHError):
            self._import(bad_kind)
        bad_reason = copy.deepcopy(base)
        bad_reason["unresolved"] = [
            {"section_id": "S02", "kind": "missing_evidence",
             "reason": "  "}]
        with self.assertRaises(CCHError):
            self._import(bad_reason)

    # --- F: semantic grounding hardening (relation polarity +
    # interpretation reasoning provenance), office-domain only ---

    def test_f_map_job_rejects_legacy_result_schema(self):
        """A /2 map job never accepts a /1 result: backward-compatible
        read of frozen evidence does not mean backward-compatible
        write/import."""
        job = self._issued_job()
        doc, _ = self._valid_doc(job)
        doc["schema"] = "authoring-map-result/1"
        with self.assertRaises(CCHError):
            self._import(doc)

    def test_f_artifact_id_cannot_back_claim_evidence(self):
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["evidence"] = [
            {"evidence_id": ids["s1_asset"], "relation": "supports"}]
        with self.assertRaises(CCHError):
            self._import(doc)

    def test_f_interpretation_without_reasoning_rejected(self):
        job = self._issued_job()
        doc, _ = self._valid_doc(job)
        doc["claims"][0]["claim_type"] = "INTERPRETATION"
        with self.assertRaises(CCHError):
            self._import(doc)

    def test_f_interpretation_reasoning_outside_claim_evidence_rejected(self):
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["claim_type"] = "INTERPRETATION"
        doc["claims"][0]["reasoning"] = {
            "from_evidence_ids": [ids["s2_cog"]],
            "inference": "引用另一节证据的推导",
        }
        with self.assertRaises(CCHError):
            self._import(doc)

    def test_f_pp1_structure_regression_rejected_at_import(self):
        """Office-domain replay of the P-P1 failure structure: a
        supported FACT_CURRENT plus an INTERPRETATION whose reasoning
        basis is only contextualizes must never enter the frozen
        chain."""
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["text"] = "办公系统由采集层、处理层、展示层三层构成。"
        doc["claims"][1]["claim_type"] = "INTERPRETATION"
        doc["claims"][1]["text"] = "数据必然按采集层、处理层、展示层顺序流动。"
        doc["claims"][1]["evidence"] = [
            {"evidence_id": ids["s2_cog"], "relation": "contextualizes"}]
        doc["claims"][1]["reasoning"] = {
            "from_evidence_ids": [ids["s2_cog"]],
            "inference": "由三层命名顺序推断数据逐层流动。",
        }
        with self.assertRaises(CCHError):
            self._import(doc)

    def test_f_interpretation_with_supports_basis_passes_and_covers(self):
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["claim_type"] = "INTERPRETATION"
        doc["claims"][0]["text"] = (
            "归档以部门负责人审批为第一环，可推断其为核心控制点。")
        doc["claims"][0]["reasoning"] = {
            "from_evidence_ids": [ids["s1_cog"]],
            "inference": "制度将部门负责人审批设为流程首环，据此推断其为核心控制点。",
        }
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "READY_FOR_REVIEW")
        run_dir = models.run_dir_for(self.home, self.run_id)
        vr = models.load_json(run_dir / "verification-report.json")
        self.assertEqual(
            vr["metrics"]["interpretation_reasoning_coverage"], 1.0)

    def test_f_rehashed_malformed_reasoning_rejected_by_verifier(self):
        """F-RC: structural /2 validation runs again inside the
        verifier. A hand-edited frozen map whose INTERPRETATION
        reasoning cites an evidence ID never present in the claim is
        rejected even after the manifest hash is regenerated."""
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["claim_type"] = "INTERPRETATION"
        doc["claims"][0]["text"] = (
            "归档以部门负责人审批为第一环，可推断其为核心控制点。")
        doc["claims"][0]["reasoning"] = {
            "from_evidence_ids": [ids["s1_cog"]],
            "inference": "制度将部门负责人审批设为流程首环，据此推断其为核心控制点。",
        }
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        run_dir = models.run_dir_for(self.home, self.run_id)
        map_path = run_dir / "evidence-map.json"
        em = models.load_json(map_path)
        em["claims"][0]["reasoning"]["from_evidence_ids"].append(
            "EV-NOT-CITED")
        models.write_json(map_path, em)
        manifest = models.read_manifest(run_dir)
        manifest["evidence_map_sha256"] = models.sha256_of(em)
        models.write_json(run_dir / "manifest.json", manifest)
        with self.assertRaises(CCHError):
            verifier.verify(self.home, self.run_id)
        re_manifest = models.read_manifest(run_dir)
        self.assertEqual(re_manifest["state"], "STORYBOARD_FROZEN")
        self.assertFalse(
            (run_dir / "verification-report.json").exists())

    def test_f_contradicts_without_conflict_rejected_at_import(self):
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["evidence"].append(
            {"evidence_id": ids["s1_src"], "relation": "contradicts"})
        with self.assertRaises(CCHError):
            self._import(doc)
        run_dir = models.run_dir_for(self.home, self.run_id)
        manifest = models.read_manifest(run_dir)
        self.assertEqual(manifest["state"], "MAPPING")
        self.assertFalse((run_dir / "evidence-map.json").exists())

    def test_f_contradicts_with_conflict_freezes_but_blocks(self):
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"][0]["evidence"].append(
            {"evidence_id": ids["s1_src"], "relation": "contradicts"})
        doc["unresolved"] = [
            {"section_id": "S01", "kind": "conflict",
             "reason": "同节来源与制度条款冲突"}]
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "BLOCKED")
        self.assertFalse(out["gates"]["no_unresolved_blockers"])

    def test_f_fact_current_contextualizes_blocks_and_zero_coverage(self):
        job = self._issued_job()
        doc, ids = self._valid_doc(job)
        doc["claims"] = doc["claims"][:1]
        doc["claims"][0]["evidence"] = [
            {"evidence_id": ids["s1_cog"], "relation": "contextualizes"}]
        doc["unresolved"] = [
            {"section_id": "S02", "kind": "missing_evidence",
             "reason": "无预算证据"}]
        self._import(doc)
        mapper_exchange.build_storyboard(self.home, self.run_id)
        out = verifier.verify(self.home, self.run_id)
        self.assertEqual(out["status"], "BLOCKED")
        self.assertFalse(out["gates"]["fact_current_backed_by_cognition"])
        run_dir = models.run_dir_for(self.home, self.run_id)
        vr = models.load_json(run_dir / "verification-report.json")
        self.assertEqual(vr["metrics"]["fact_evidence_coverage"], 0.0)

    def test_f_map_job_instructions_declare_polarity_contract(self):
        job = self._issued_job()
        ins = job["instructions"]
        self.assertTrue(ins["artifact_is_not_fact_authority"])
        self.assertIn("unresolved",
                      ins["artifact_metadata_visual_discovery_only"])
        self.assertEqual(ins["relation_polarity"]["supports"],
                         "positive grounding")
        self.assertIn("conflict", ins["relation_polarity"]["contradicts"])
        self.assertIn("supports", ins["interpretation_grounding"])


class WorkReceiptContractTests(unittest.TestCase):
    """E-RC P1: the receipt validator enforces the full handoff
    contract, not just the loose minimum."""

    def _receipt(self):
        return {
            "schema": models.WORK_RECEIPT_SCHEMA,
            "run_id": "AUTH-20260918-001",
            "task_status": "READY_FOR_REVIEW",
            "base_revision": "b" * 40,
            "retrieval_version": models.RETRIEVAL_VERSION,
            "artifacts": {k: "a" * 64 for k in (
                "brief_sha256", "plan_sha256",
                "evidence_package_sha256", "evidence_map_sha256",
                "storyboard_sha256")},
            "verification": {"fact_evidence_coverage": 1.0},
            "changed": [],
            "unresolved": [],
            "proposals": [],
            "handoff": {"next_agent_should_know": ["note"]},
            "generated_at": "2026-09-18T00:00:00+00:00",
        }

    def test_full_receipt_passes(self):
        models.validate_work_receipt(self._receipt())

    def test_missing_artifact_hash_rejected(self):
        wr = self._receipt()
        del wr["artifacts"]["storyboard_sha256"]
        with self.assertRaises(CCHError):
            models.validate_work_receipt(wr)

    def test_bad_artifact_hash_rejected(self):
        wr = self._receipt()
        wr["artifacts"]["plan_sha256"] = "z" * 64
        with self.assertRaises(CCHError):
            models.validate_work_receipt(wr)

    def test_missing_verification_or_handoff_rejected(self):
        wr = self._receipt()
        del wr["verification"]
        with self.assertRaises(CCHError):
            models.validate_work_receipt(wr)
        wr2 = self._receipt()
        wr2["handoff"] = {}
        with self.assertRaises(CCHError):
            models.validate_work_receipt(wr2)


class VerifierGateTests(unittest.TestCase):
    """Pure evaluate_gates cases: superseded primary visual, unhandled
    conflicts, missing-section warnings, per-claim-type evidence rules."""

    @staticmethod
    def _pkg(conflict_section=True):
        def sec(sid, **over):
            s = {"section_id": sid, "cognition": [], "sources": [],
                 "artifacts": [], "conflicts": [], "missing": []}
            s.update(over)
            return s
        return {"schema": "evidence-package/1", "sections": [
            sec("S01",
                cognition=[{"evidence_id": "COG-X-0"}],
                sources=[{"evidence_id": "EV-X"}],
                artifacts=[{"asset_id": "AST-A", "asset_status": "unknown"},
                           {"asset_id": "AST-B", "asset_status": "superseded"}],
                missing=[{"kind": "retrieval", "reason": "no candidates"}]),
            sec("S02",
                conflicts=[{"kind": "conflict"}] if conflict_section else []),
        ]}

    @staticmethod
    def _claim(cid, sid, ctype, eids):
        return {"claim_id": cid, "section_id": sid, "claim_type": ctype,
                "text": "x", "confidence": "high", "currentness": "current",
                "evidence": [{"evidence_id": e, "relation": "supports"}
                             for e in eids]}

    def _gates(self, claims, bindings=()):
        em = {"claims": claims, "visual_bindings": list(bindings)}
        return verifier.evaluate_gates(self._pkg(), em)

    def _gates_no_conflict(self, claims, bindings=()):
        em = {"claims": claims, "visual_bindings": list(bindings)}
        return verifier.evaluate_gates(self._pkg(conflict_section=False), em)

    def test_fact_current_with_cognition_passes(self):
        gates, warnings, blockers = self._gates_no_conflict(
            [self._claim("C1", "S01", "FACT_CURRENT", ["COG-X-0"])])
        self.assertTrue(all(gates.values()))
        self.assertEqual(blockers, [])
        self.assertEqual(warnings,
                         ["section S01 missing retrieval: no candidates"])

    def test_fact_current_without_cognition_blocked(self):
        gates, _, blockers = self._gates(
            [self._claim("C1", "S01", "FACT_CURRENT", ["EV-X"])])
        self.assertFalse(gates["fact_current_backed_by_cognition"])
        self.assertTrue(blockers)

    def test_fact_historical_source_only_warns_not_blocks(self):
        gates, warnings, _ = self._gates_no_conflict(
            [self._claim("C1", "S01", "FACT_HISTORICAL",
                         ["COG-X-0"])])
        self.assertTrue(gates["fact_historical_section_local"])
        self.assertTrue(any("FACT_HISTORICAL" in w for w in warnings))

    def test_empty_evidence_claim_blocked(self):
        gates, _, blockers = self._gates(
            [self._claim("C1", "S01", "INTERPRETATION", [])])
        self.assertFalse(gates["claims_have_evidence"])
        self.assertTrue(any("INTERPRETATION" in b for b in blockers))

    def test_recommendation_needs_basis(self):
        gates, _, _ = self._gates(
            [self._claim("C1", "S01", "RECOMMENDATION", ["COG-X-0"])])
        self.assertTrue(gates["recommendation_has_evidence"])
        gates2, _, blockers2 = self._gates(
            [self._claim("C1", "S01", "RECOMMENDATION", ["EV-X"])])
        self.assertTrue(gates2["recommendation_has_evidence"])

    def test_binding_needs_same_section_claim(self):
        binding = {"section_id": "S01", "asset_id": "AST-A",
                   "role": "primary_visual",
                   "supports_claim_ids": ["CLM-OTHER"],
                   "reason": "r"}
        gates, _, _ = self._gates(
            [self._claim("C1", "S01", "FACT_CURRENT", ["COG-X-0"])],
            [binding])
        self.assertFalse(gates["visuals_bound_to_claims"])

    def test_superseded_primary_visual_blocked(self):
        claims = [self._claim("C1", "S01", "FACT_CURRENT", ["COG-X-0"])]
        good = {"section_id": "S01", "asset_id": "AST-A",
                "role": "primary_visual", "supports_claim_ids": ["C1"],
                "reason": "r"}
        gates, _, _ = self._gates(claims, [good])
        self.assertTrue(gates["no_superseded_primary_visual"])
        bad = dict(good, asset_id="AST-B")
        gates2, _, blockers2 = self._gates(claims, [bad])
        self.assertFalse(gates2["no_superseded_primary_visual"])
        self.assertTrue(any("AST-B" in b for b in blockers2))
        gates3, warnings3, _ = self._gates(
            claims, [dict(bad, role="supporting_visual")])
        self.assertTrue(gates3["no_superseded_primary_visual"])
        self.assertTrue(any("AST-B" in w for w in warnings3))

    def test_unhandled_conflict_blocked(self):
        gates, _, blockers = self._gates(
            [self._claim("C1", "S01", "FACT_CURRENT", ["COG-X-0"])])
        self.assertFalse(gates["no_unhandled_conflicts"])
        self.assertTrue(any("S02" in b for b in blockers))

    # --- F: polarity-aware gate cases (pure evaluate_gates) ---

    def _interp_claim(self, cid, sid, eids, from_ids, inference="x"):
        return {
            "claim_id": cid, "section_id": sid,
            "claim_type": "INTERPRETATION", "text": "x",
            "confidence": "high", "currentness": "current",
            "evidence": [{"evidence_id": e, "relation": r}
                         for e, r in eids],
            "reasoning": {"from_evidence_ids": from_ids,
                          "inference": inference},
        }

    def test_f_fact_current_supports_cognition_passes_pure(self):
        gates, _, blockers = self._gates_no_conflict(
            [self._claim("C1", "S01", "FACT_CURRENT", ["COG-X-0"])])
        self.assertTrue(gates["fact_current_backed_by_cognition"])
        self.assertEqual(blockers, [])

    def test_f_fact_historical_contextualizes_never_passes(self):
        gates, _, blockers = self._gates_no_conflict(
            [{"claim_id": "C1", "section_id": "S01",
              "claim_type": "FACT_HISTORICAL", "text": "x",
              "confidence": "high", "currentness": "historical",
              "evidence": [{"evidence_id": "COG-X-0",
                            "relation": "contextualizes"}]}])
        self.assertFalse(gates["fact_historical_section_local"])
        self.assertTrue(any("FACT_HISTORICAL" in b for b in blockers))

    def test_f_fact_historical_illustrates_never_passes(self):
        gates, _, _ = self._gates_no_conflict(
            [{"claim_id": "C1", "section_id": "S01",
              "claim_type": "FACT_HISTORICAL", "text": "x",
              "confidence": "high", "currentness": "historical",
              "evidence": [{"evidence_id": "EV-X",
                            "relation": "illustrates"}]}])
        self.assertFalse(gates["fact_historical_section_local"])

    def test_f_interpretation_supports_basis_passes_pure(self):
        gates, _, _ = self._gates_no_conflict(
            [self._interp_claim("C1", "S01",
                                [("COG-X-0", "supports"),
                                 ("EV-X", "contextualizes")],
                                ["COG-X-0"])])
        self.assertTrue(gates["interpretation_has_supporting_basis"])

    def test_f_interpretation_contextualizes_only_basis_blocked(self):
        gates, _, blockers = self._gates_no_conflict(
            [self._interp_claim("C1", "S01",
                                [("EV-X", "contextualizes")],
                                ["EV-X"])])
        self.assertFalse(gates["interpretation_has_supporting_basis"])
        self.assertTrue(any("INTERPRETATION" in b for b in blockers))

    def test_f_interpretation_mixed_basis_requires_supports(self):
        gates, _, _ = self._gates_no_conflict(
            [self._interp_claim("C1", "S01",
                                [("COG-X-0", "supports"),
                                 ("EV-X", "contextualizes")],
                                ["EV-X"])])
        self.assertFalse(gates["interpretation_has_supporting_basis"])


class EvidenceMapV1CompatTests(unittest.TestCase):
    """F: /1 is a frozen-evidence read format only; it validates
    nothing as /2 and never re-enters the /2 verifier."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "legacy-evidence-map.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _legacy_map(self):
        return {
            "schema": "evidence-map/1",
            "run_id": "AUTH-20260920-001",
            "evidence_package_sha256": "a" * 64,
            "claims": [{
                "claim_id": "CLM-S01-001", "section_id": "S01",
                "claim_type": "INTERPRETATION",
                "text": "无 reasoning 的旧格式解读",
                "evidence": [{"evidence_id": "EV-old-1",
                              "relation": "contextualizes"}],
                "confidence": "high", "currentness": "current",
            }],
            "visual_bindings": [],
            "unresolved": [],
        }

    def test_legacy_map_reads_but_never_validates_as_v2(self):
        legacy = self._legacy_map()
        self.path.write_text(json.dumps(legacy, ensure_ascii=False),
                             encoding="utf-8")
        data = models.read_evidence_map_any_version(self.path)
        self.assertEqual(data["schema"], "evidence-map/1")
        with self.assertRaises(CCHError):
            models.validate_evidence_map(legacy)


if __name__ == "__main__":
    unittest.main()
