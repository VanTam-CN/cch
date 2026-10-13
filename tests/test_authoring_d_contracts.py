"""C0-D contract tests: evidence-map/2 + storyboard/1 frozen schemas.

Office-domain synthetic fixtures only; validates structure that the
Evidence Mapper and Storyboard generators must satisfy in C0-D.
"""
from __future__ import annotations
import unittest

import sys
ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cch.core import CCHError
from cch.authoring import models


def evidence_map(**over):
    em = {
        "schema": "evidence-map/2",
        "run_id": "AUTH-20260915-001",
        "evidence_package_sha256": "a" * 64,
        "claims": [{
            "claim_id": "CLM-S01-001",
            "section_id": "S01",
            "claim_type": "FACT_CURRENT",
            "text": "设备归档须部门负责人审批后提交档案室。",
            "evidence": [
                {"evidence_id": "COG-FACT-arch00000001-0",
                 "relation": "supports"},
                {"evidence_id": "EV-arch00000001",
                 "relation": "supports"},
            ],
            "confidence": "high",
            "currentness": "current",
        }],
        "visual_bindings": [{
            "section_id": "S01",
            "asset_id": "AST-20260910-0001",
            "role": "primary_visual",
            "supports_claim_ids": ["CLM-S01-001"],
            "reason": "该图完整展示归档流程。",
        }],
        "unresolved": [],
    }
    em.update(over)
    return em


def storyboard(**over):
    sb = {
        "schema": "storyboard/1",
        "run_id": "AUTH-20260915-001",
        "sections": [{
            "section_id": "S01",
            "purpose": "说明归档流程入口",
            "audience_takeaway": "归档第一步是部门负责人审批。",
            "headline": "设备归档流程",
            "claim_ids": ["CLM-S01-001"],
            "visuals": [{"asset_id": "AST-20260910-0001",
                         "role": "primary_visual"}],
            "narrative_order": ["problem", "evidence", "implication"],
            "transition_to_next": "下一部分说明审批要求。",
        }],
    }
    sb.update(over)
    return sb


class EvidenceMapContractTests(unittest.TestCase):
    def test_valid_map_passes(self):
        models.validate_evidence_map(evidence_map())

    def test_duplicate_claim_id_rejected(self):
        em = evidence_map()
        em["claims"].append(dict(em["claims"][0]))
        with self.assertRaises(CCHError):
            models.validate_evidence_map(em)

    def test_unknown_claim_type_rejected(self):
        em = evidence_map()
        em["claims"][0]["claim_type"] = "FACT_TOMORROW"
        with self.assertRaises(CCHError):
            models.validate_evidence_map(em)

    def test_unknown_evidence_relation_rejected(self):
        em = evidence_map()
        em["claims"][0]["evidence"][0]["relation"] = "proves"
        with self.assertRaises(CCHError):
            models.validate_evidence_map(em)

    def test_visual_binding_unknown_claim_rejected(self):
        em = evidence_map()
        em["visual_bindings"][0]["supports_claim_ids"] = ["CLM-S09-999"]
        with self.assertRaises(CCHError):
            models.validate_evidence_map(em)

    def test_bad_package_hash_rejected(self):
        with self.assertRaises(CCHError):
            models.validate_evidence_map(
                evidence_map(evidence_package_sha256="xyz"))


class StoryboardContractTests(unittest.TestCase):
    def test_valid_storyboard_passes(self):
        models.validate_storyboard(storyboard())

    def test_duplicate_section_rejected(self):
        sb = storyboard()
        sb["sections"].append(dict(sb["sections"][0]))
        with self.assertRaises(CCHError):
            models.validate_storyboard(sb)

    def test_unknown_narrative_step_rejected(self):
        sb = storyboard()
        sb["sections"][0]["narrative_order"] = ["problem", "vibes"]
        with self.assertRaises(CCHError):
            models.validate_storyboard(sb)

    def test_visual_role_restricted(self):
        sb = storyboard()
        sb["sections"][0]["visuals"][0]["role"] = "hero_image"
        with self.assertRaises(CCHError):
            models.validate_storyboard(sb)


class VerificationAndReceiptSchemaTests(unittest.TestCase):
    def test_verification_report_validates(self):
        vr = {"schema": "authoring-verification/1",
              "run_id": "AUTH-20260915-001",
              "status": "READY_FOR_REVIEW",
              "gates": {"fact_evidence_coverage": True},
              "metrics": {"fact_claims": 1},
              "warnings": [], "blockers": []}
        models.validate_verification_report(vr)
        vr["status"] = "success"
        with self.assertRaises(CCHError):
            models.validate_verification_report(vr)

    def test_work_receipt_validates(self):
        wr = self._full_receipt()
        models.validate_work_receipt(wr)
        wr["changed"] = "nothing"
        with self.assertRaises(CCHError):
            models.validate_work_receipt(wr)

    def test_work_receipt_base_revision_is_git_commit(self):
        wr = self._full_receipt()
        models.validate_work_receipt(wr)
        sha = self._full_receipt()
        sha["base_revision"] = "b" * 64
        with self.assertRaises(CCHError):
            models.validate_work_receipt(sha)

    @staticmethod
    def _full_receipt():
        return {"schema": "work-receipt/1",
                "run_id": "AUTH-20260915-001",
                "task_status": "READY_FOR_REVIEW",
                "base_revision": "b" * 40,
                "retrieval_version": "v0.4.0",
                "artifacts": {k: "a" * 64 for k in (
                    "brief_sha256", "plan_sha256",
                    "evidence_package_sha256", "evidence_map_sha256",
                    "storyboard_sha256")},
                "verification": {},
                "changed": [], "unresolved": [], "proposals": [],
                "handoff": {"next_agent_should_know": ["note"]},
                "generated_at": "2026-09-18T00:00:00+00:00"}
