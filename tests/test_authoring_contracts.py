"""C0-A contract tests: brief/plan schemas, hashing, run storage.

Fixtures are office-domain synthetic data only (archive/approval/demo).
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
from cch.authoring import models


def draft_brief(**overrides):
    brief = {
        "schema": "authoring-brief/1",
        "brief_id": "BRIEF-20260915-001",
        "title": "Q4 设备归档工作说明",
        "request": "用3到5页说明设备归档流程怎么走、审批要求是什么",
        "audience": "部门同事",
        "purpose": "explain",
        "deliverable_type": "technical_brief",
        "scope": {"domain": "demo", "project": "m2"},
        "constraints": {
            "max_sections": 5,
            "cross_project_reuse": False,
            "allow_superseded_assets": False,
            "external_knowledge": False,
        },
        "requested_topics": [],
        "must_include": [],
        "must_avoid": [],
        "created_at": "2026-09-15T00:00:00+00:00",
        "base_revision": "0" * 64,
    }
    brief.update(overrides)
    return brief


class BriefContractTests(unittest.TestCase):
    def test_valid_brief_passes(self):
        models.validate_brief(draft_brief())

    def test_bad_schema_rejected(self):
        with self.assertRaises(CCHError):
            models.validate_brief(draft_brief(schema="authoring-brief/2"))

    def test_bad_purpose_rejected(self):
        with self.assertRaises(CCHError):
            models.validate_brief(draft_brief(purpose="vibes"))

    def test_bad_max_sections_rejected(self):
        brief = draft_brief()
        brief["constraints"]["max_sections"] = 0
        with self.assertRaises(CCHError):
            models.validate_brief(brief)

    def test_bool_flag_required(self):
        brief = draft_brief()
        brief["constraints"]["external_knowledge"] = "no"
        with self.assertRaises(CCHError):
            models.validate_brief(brief)


def plan(sections=None):
    return {
        "schema": "authoring-plan/1",
        "brief_id": "BRIEF-20260915-001",
        "sections": sections or [{
            "section_id": "S01",
            "purpose": "说明归档流程入口",
            "audience_question": "归档从哪里开始？",
            "required_claim_types": ["FACT_CURRENT"],
            "retrieval_queries": {"cognition": ["归档流程 启动"],
                                   "artifact": ["归档流程图"]},
        }],
    }


class PlanContractTests(unittest.TestCase):
    def test_valid_plan_passes(self):
        models.validate_plan(plan())

    def test_duplicate_section_rejected(self):
        s = plan()["sections"]
        s.append(dict(s[0]))
        with self.assertRaises(CCHError):
            models.validate_plan(plan(s))

    def test_unknown_claim_type_rejected(self):
        s = plan()["sections"][0]
        s["required_claim_types"] = ["FACT_TOMORROW"]
        with self.assertRaises(CCHError):
            models.validate_plan(plan([s]))

    def test_empty_queries_rejected(self):
        s = plan()["sections"][0]
        s["retrieval_queries"] = {"cognition": [], "artifact": []}
        with self.assertRaises(CCHError):
            models.validate_plan(plan([s]))


class StorageTests(unittest.TestCase):
    def test_write_json_is_canonical_and_hashable(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = {"b": 1, "a": 2}
            path = Path(tmp) / "x.json"
            sha = models.write_json(path, value)
            raw = path.read_text()
            self.assertEqual(raw, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            self.assertEqual(sha, models.sha256_of(value))

    def test_new_run_dir_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            run1, dir1 = models.new_run_dir(tmp, date="20260915")
            run2, dir2 = models.new_run_dir(tmp, date="20260915")
            n1 = int(run1.rsplit("-", 1)[1])
            n2 = int(run2.rsplit("-", 1)[1])
            self.assertEqual(n2, n1 + 1)  # monotonic, never reused
            self.assertTrue(run1.startswith("AUTH-20260915-"))
            self.assertTrue(dir1.is_dir() and dir2.is_dir())


if __name__ == "__main__":
    unittest.main()
