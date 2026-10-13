"""C0-B planner exchange tests: job binding, atomic import, no answers."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cch.core import CCHError
from cch.store import Home, store_init
from cch.authoring import models, planner_exchange
from cch.authoring import brief as brief_mod
from tests.test_authoring_contracts import draft_brief, plan


class PlannerFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "home"
        self.home = Home(self.root)
        store_init(self.home)  # empty store; approved_commit() is None
        brief_path = self.root / "draft-brief.json"
        brief_path.write_text(json.dumps(draft_brief(), ensure_ascii=False), encoding="utf-8")
        result = brief_mod.init_run(self.home, brief_path)
        self.run_id = result["run_id"]
        self.run_dir = models.run_dir_for(self.home, self.run_id)

    def tearDown(self):
        self._tmp.cleanup()

    def _job(self):
        return planner_exchange.build_plan_job(self.home, self.run_id)

    def _result(self, sections=None):
        p = plan(sections)
        p.pop("brief_id")  # strict result schema: the job carries the brief
        p["schema"] = "authoring-plan-result/1"
        p["job_id"] = "placeholder"
        p["manifest_sha256"] = "placeholder"
        return p

    def test_job_binds_frozen_brief_hash(self):
        out = self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        self.assertEqual(job["schema"], "authoring-plan-job/1")
        manifest = models.read_manifest(self.run_dir)
        brief_raw = json.dumps(
            models.load_json(self.run_dir / "brief.json"),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        import hashlib
        brief_canon_sha = hashlib.sha256(brief_raw.encode("utf-8")).hexdigest()
        self.assertEqual(job["brief_sha256"], manifest["brief_sha256"])
        self.assertEqual(job["brief_sha256"], brief_canon_sha)
        self.assertEqual(out["plan_job_sha256"], models.sha256_of(job))
        self.assertFalse(job["instructions"]["use_external_knowledge"])
        self.assertTrue(job["instructions"]["output_retrieval_intents_only"])

    def test_import_happy_path(self):
        self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        result = self._result()
        result["job_id"] = job["job_id"]
        result["manifest_sha256"] = job["manifest_sha256"]
        result_path = self.root / "result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        receipt = planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        self.assertEqual(receipt["state"], "PLAN_FROZEN")
        imported = models.load_json(self.run_dir / "plan.json")
        self.assertEqual(imported["schema"], "authoring-plan/1")
        self.assertEqual(imported["sections"][0]["section_id"], "S01")

    def test_import_bad_job_id_writes_nothing(self):
        self._job()
        result = self._result()
        result["job_id"] = "AUTHPLAN-wrong"
        result["manifest_sha256"] = "x" * 64
        result_path = self.root / "bad.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        self.assertFalse((self.run_dir / "plan.json").exists())
        manifest = models.read_manifest(self.run_dir)
        self.assertEqual(manifest["state"], "PLAN_PENDING")

    def test_import_bad_manifest_hash_writes_nothing(self):
        self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        result = self._result()
        result["job_id"] = job["job_id"]
        result["manifest_sha256"] = "0" * 64
        result_path = self.root / "bad2.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        self.assertFalse((self.run_dir / "plan.json").exists())

    def test_import_atomic_when_second_section_invalid(self):
        self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        s1 = plan()["sections"][0]
        s2 = json.loads(json.dumps(s1))
        s2["section_id"] = "S02"
        s3 = json.loads(json.dumps(s1))
        s3["section_id"] = "S01"  # duplicate -> invalid
        result = self._result([s1, s2, s3])
        result["job_id"] = job["job_id"]
        result["manifest_sha256"] = job["manifest_sha256"]
        result_path = self.root / "dup.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        self.assertFalse((self.run_dir / "plan.json").exists())
        self.assertFalse((self.run_dir / "plan-result.json").exists())

    def test_import_rejects_extra_section_fields(self):
        self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        sec = plan()["sections"][0]
        sec["draft_text"] = "领导汇报可写成……"
        result = self._result([sec])
        result["job_id"] = job["job_id"]
        result["manifest_sha256"] = job["manifest_sha256"]
        result_path = self.root / "extra.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        self.assertFalse((self.run_dir / "plan.json").exists())
        manifest = models.read_manifest(self.run_dir)
        self.assertEqual(manifest["state"], "PLAN_PENDING")

    def test_import_rejects_extra_result_fields(self):
        self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        result = self._result()
        result["job_id"] = job["job_id"]
        result["manifest_sha256"] = job["manifest_sha256"]
        result["answer"] = "5G-A 已经实现了……"
        result_path = self.root / "extra2.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        self.assertFalse((self.run_dir / "plan.json").exists())

    def test_import_rejects_extra_query_channel(self):
        self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        sec = plan()["sections"][0]
        sec["retrieval_queries"]["facts"] = ["某事实"]
        result = self._result([sec])
        result["job_id"] = job["job_id"]
        result["manifest_sha256"] = job["manifest_sha256"]
        result_path = self.root / "extra3.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)

    def test_import_rejects_tampered_plan_job(self):
        """Exchange integrity RC: an issued plan job is immutable; import
        must verify its hash against the manifest before anything else."""
        self._job()
        job_path = self.run_dir / "plan-job.json"
        original = job_path.read_bytes()
        job = models.load_json(job_path)
        job["instructions"]["use_external_knowledge"] = True
        models.write_json(job_path, job)
        result = self._result()
        result["job_id"] = job["job_id"]  # even result matches tampered job
        result["manifest_sha256"] = job["manifest_sha256"]
        result_path = self.root / "tampered-job.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        manifest = models.read_manifest(self.run_dir)
        self.assertEqual(manifest["state"], "PLAN_PENDING")
        self.assertFalse((self.run_dir / "plan.json").exists())
        job_path.write_bytes(original)
        self.assertFalse((self.run_dir / "plan.json").exists())

    def test_oversized_section_count_rejected(self):
        self._job()
        job = models.load_json(self.run_dir / "plan-job.json")
        base_sec = plan()["sections"][0]
        sections = []
        for i in range(6):  # brief max_sections = 5
            s = json.loads(json.dumps(base_sec))
            s["section_id"] = "S%02d" % (i + 1)
            sections.append(s)
        result = self._result(sections)
        result["job_id"] = job["job_id"]
        result["manifest_sha256"] = job["manifest_sha256"]
        result_path = self.root / "big.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CCHError):
            planner_exchange.import_plan_result(self.home, self.run_id, result_path)
        self.assertFalse((self.run_dir / "plan.json").exists())


if __name__ == "__main__":
    unittest.main()
