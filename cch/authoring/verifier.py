"""C0-E verification: deterministic hard gates over the frozen chain.

Read-only gate evaluation on top of the frozen Brief / Plan / Evidence
Package / Evidence Map / Storyboard. The verifier produces an
authoring-verification/1 report and a work-receipt/1 handoff document.
It never writes DONE: that transition requires explicit human approval
and is implemented in review, not here.

Hard gates (mechanical only; semantic support quality stays with the
human reviewer). M2-C.0-F hardening: relation polarity is enforced —
- every claim cites at least one package evidence item (already
  enforced at import, re-checked here over the frozen map);
- FACT_CURRENT claims cite at least one cognition item from their own
  section with relation=supports (sources/artifacts and non-supports
  relations cannot back a current fact);
- FACT_HISTORICAL claims cite at least one section-local
  cognition/source evidence item with relation=supports; other
  relations are context, not grounding. A cognition-only supported
  historical claim keeps its currentness warning;
- INTERPRETATION claims carry a reasoning mapping whose basis cites a
  relation=supports evidence ref (structure check only; enforced at
  import, re-checked here);
- RECOMMENDATION claims cite at least one evidence item;
- a claim citing relation=contradicts must have a same-section
  unresolved kind=conflict (import enforces it atomically; re-checked
  here so a tampered-but-rehashed map can never pass gates).
- every visual binding supports at least one same-section claim with a
  non-empty reason (already enforced at import, re-checked here);
- primary visuals must not reference superseded assets (asset_status
  from the frozen package is authoritative).
"""
from __future__ import annotations

from ..core import require
from ..store import canon_digest, now_iso
from ..store import Home
from . import models
from . import brief as brief_mod


def _require_frozen_chain(run_dir):
    """Re-verify every frozen artifact hash before any gate runs."""
    manifest = models.read_manifest(run_dir)
    frozen_brief = brief_mod.load_frozen_brief(run_dir)
    plan = models.load_json(run_dir / "plan.json")
    pkg = models.load_json(run_dir / "evidence-package.json")
    em = models.load_json(run_dir / "evidence-map.json")
    sb = models.load_json(run_dir / "storyboard.json")
    require(canon_digest(frozen_brief) == manifest.get("brief_sha256"),
            "frozen brief changed; refusing verification")
    require(canon_digest(plan) == manifest.get("plan_sha256"),
            "frozen plan changed; refusing verification")
    require(canon_digest(pkg) == manifest.get("evidence_package_sha256"),
            "frozen evidence package changed; refusing verification")
    require(canon_digest(em) == manifest.get("evidence_map_sha256"),
            "frozen evidence map changed; refusing verification")
    require(canon_digest(sb) == manifest.get("storyboard_sha256"),
            "frozen storyboard changed; refusing verification")
    # F-RC: hash identity is not enough. The frozen map is re-validated
    # as evidence-map/2 before any gate runs, so a hand-edited but
    # rehashed map fails structural validation here, and legacy /1
    # never re-enters the /2 verification flow.
    models.validate_evidence_map(em)
    return manifest, frozen_brief, plan, pkg, em, sb


def _section_bindings(pkg):
    by_sid = {}
    for sec in pkg["sections"]:
        by_sid[sec["section_id"]] = {
            "cognition": {c["evidence_id"] for c in sec["cognition"]},
            "sources": {s["evidence_id"] for s in sec["sources"]},
            "assets": {a["asset_id"]: a for a in sec["artifacts"]},
            "missing": sec.get("missing", []),
            "conflicts": sec.get("conflicts", []),
        }
    return by_sid


def _is_superseded(sections, binding):
    entry = sections.get(binding["section_id"], {}).get(
        "assets", {}).get(binding["asset_id"])
    return bool(entry) and entry.get("asset_status") in (
        "superseded", "changed")


def evaluate_gates(pkg, em):
    """Run the mechanical hard gates over the frozen map/package.

    Returns (gates, warnings, blockers); gates is a name->bool mapping.
    """
    gates = {
        "claims_have_evidence": True,
        "fact_current_backed_by_cognition": True,
        "fact_historical_section_local": True,
        "interpretation_has_evidence": True,
        "interpretation_has_supporting_basis": True,
        "recommendation_has_evidence": True,
        "visuals_bound_to_claims": True,
        "no_superseded_primary_visual": True,
        "no_unhandled_conflicts": True,
        "no_unresolved_blockers": True,
    }
    warnings, blockers = [], []
    sections = _section_bindings(pkg)
    for claim in em["claims"]:
        sid = claim["section_id"]
        refs = claim.get("evidence", [])
        ref_ids = {r["evidence_id"] for r in refs}
        supports_ids = {r["evidence_id"] for r in refs
                        if r["relation"] == "supports"}
        sec = sections.get(sid, {})
        cog_ids = sec.get("cognition", set())
        src_ids = sec.get("sources", set())
        if not refs:
            gates["claims_have_evidence"] = False
            blockers.append("claim " + claim["claim_id"]
                            + " has no evidence")
        ctype = claim["claim_type"]
        if ctype == "FACT_CURRENT" and not (supports_ids & cog_ids):
            gates["fact_current_backed_by_cognition"] = False
            blockers.append("FACT_CURRENT claim " + claim["claim_id"]
                            + " has no relation=supports cognition "
                            + "evidence in section " + sid)
        if ctype == "FACT_HISTORICAL":
            if not (supports_ids & (cog_ids | src_ids)):
                gates["fact_historical_section_local"] = False
                blockers.append("FACT_HISTORICAL claim "
                                + claim["claim_id"]
                                + " cites no section-local relation="
                                + "supports cognition/source evidence")
            elif not (supports_ids & src_ids):
                warnings.append("FACT_HISTORICAL claim "
                                + claim["claim_id"]
                                + " has no source evidence; currentness "
                                + "not assessable")
        if ctype == "INTERPRETATION":
            if not refs:
                gates["interpretation_has_evidence"] = False
                blockers.append("INTERPRETATION claim "
                                + claim["claim_id"] + " has no evidence")
            reasoning = claim.get("reasoning") or {}
            from_ids = set(reasoning.get("from_evidence_ids") or [])
            if not (from_ids & supports_ids):
                gates["interpretation_has_supporting_basis"] = False
                blockers.append("INTERPRETATION claim "
                                + claim["claim_id"]
                                + " has no relation=supports evidence in "
                                + "its reasoning basis")
        if ctype == "RECOMMENDATION" and not refs:
            gates["recommendation_has_evidence"] = False
            blockers.append("RECOMMENDATION claim " + claim["claim_id"]
                            + " has no evidence/basis")
    claim_ids_by_sid = {}
    for claim in em["claims"]:
        claim_ids_by_sid.setdefault(claim["section_id"],
                                    set()).add(claim["claim_id"])
    for binding in em.get("visual_bindings", []):
        sid = binding["section_id"]
        supported = set(binding.get("supports_claim_ids", []))
        if not supported or not (supported
                                 & claim_ids_by_sid.get(sid, set())):
            gates["visuals_bound_to_claims"] = False
            blockers.append("visual binding for asset "
                            + binding["asset_id"]
                            + " supports no same-section claim")
        if _is_superseded(sections, binding):
            if binding.get("role") == "primary_visual":
                gates["no_superseded_primary_visual"] = False
                blockers.append("primary visual " + binding["asset_id"]
                                + " is superseded/changed")
            else:
                warnings.append("visual " + binding["asset_id"]
                                + " is superseded/changed")
    for sid, sec in sections.items():
        for item in sec.get("missing", []):
            warnings.append("section " + sid + " missing "
                            + item.get("kind", "?") + ": "
                            + item.get("reason", ""))
        if sec.get("conflicts"):
            gates["no_unhandled_conflicts"] = False
            blockers.append("section " + sid + " has unhandled conflicts")
    # F: contradicts-without-conflict is normally rejected at import;
    # re-check here so a hand-edited but rehashed map cannot pass gates.
    unresolved_conflict_sids = {i["section_id"]
                                for i in em.get("unresolved", [])
                                if i["kind"] == "conflict"}
    for claim in em["claims"]:
        if any(r["relation"] == "contradicts" for r in claim["evidence"]) \
                and claim["section_id"] not in unresolved_conflict_sids:
            gates["claims_have_evidence"] = False
            blockers.append("claim " + claim["claim_id"]
                            + " cites relation=contradicts without a "
                            + "same-section unresolved conflict")
    blocking_kinds = {"missing_evidence", "conflict",
                      "unsupported_request"}
    for item in em.get("unresolved", []):
        entry = ("unresolved " + item["section_id"] + " "
                 + item["kind"] + ": " + item.get("reason", ""))
        if item["kind"] in blocking_kinds:
            gates["no_unresolved_blockers"] = False
            blockers.append(entry)
        else:
            warnings.append(entry)
    return gates, warnings, blockers


def _decide_status(warnings, blockers):
    if blockers:
        return "BLOCKED"
    return "PARTIALLY_VERIFIED" if warnings else "READY_FOR_REVIEW"


def _missing_plan_sections(plan, sb):
    plan_sids = {sec["section_id"] for sec in plan["sections"]}
    sb_sids = {sec["section_id"] for sec in sb["sections"]}
    return sorted(plan_sids - sb_sids)


def verify(home, run_id):
    """Run verification for a STORYBOARD_FROZEN run.

    Writes verification-report.json + work-receipt.json and freezes the
    run into VERIFIED. Zero Cognitive Base / artifact-library writes.
    The verifier can never emit DONE; that state is reachable only via
    explicit human approval outside this module.
    """
    home = home if isinstance(home, Home) else Home(home)
    run_dir = models.run_dir_for(home, run_id)
    brief_mod.require_state(run_dir, "STORYBOARD_FROZEN")
    manifest, frozen_brief, plan, pkg, em, sb = \
        _require_frozen_chain(run_dir)
    gates, warnings, blockers = evaluate_gates(pkg, em)
    sections = _section_bindings(pkg)
    missing_sections = _missing_plan_sections(plan, sb)
    gates["all_plan_sections_accounted_for"] = not missing_sections
    if missing_sections:
        blockers.append("storyboard is missing plan sections: "
                        + ", ".join(missing_sections))
    status = _decide_status(warnings, blockers)
    # F: coverage counts only polarity-true grounding. A FACT_CURRENT
    # claim is covered only when a section-local cognition evidence item
    # is cited with relation=supports; contextualizes/contradicts never
    # count, so gates and metrics can no longer disagree.
    fact_claims = [c for c in em["claims"]
                   if c["claim_type"] == "FACT_CURRENT"]
    covered = [c for c in fact_claims
               if {r["evidence_id"] for r in c["evidence"]
                   if r["relation"] == "supports"}
               & sections.get(c["section_id"], {}).get("cognition", set())]
    interp_claims = [c for c in em["claims"]
                     if c["claim_type"] == "INTERPRETATION"]
    reasoning_covered = [
        c for c in interp_claims
        if bool(set((c.get("reasoning") or {}).get("from_evidence_ids")
                    or [])
                & {r["evidence_id"] for r in c["evidence"]
                   if r["relation"] == "supports"})]
    all_refs = {r["evidence_id"] for c in em["claims"]
                for r in c["evidence"]}
    valid_refs = set()
    for sec in sections.values():
        valid_refs |= sec["cognition"] | sec["sources"]
    all_bindings = em.get("visual_bindings", [])
    traced_bindings = [b for b in all_bindings
                       if b["asset_id"] in sections.get(
                           b["section_id"], {}).get("assets", {})]
    vr = {
        "schema": models.VERIFICATION_SCHEMA,
        "run_id": run_id,
        "status": status,
        "gates": gates,
        "metrics": {
            "fact_evidence_coverage": (
                round(len(covered) / len(fact_claims), 4)
                if fact_claims else 1.0),
            "evidence_reference_traceability": (
                round(len(all_refs & valid_refs) / len(all_refs), 4)
                if all_refs else 1.0),
            "asset_traceability": (
                round(len(traced_bindings) / len(all_bindings), 4)
                if all_bindings else 1.0),
            "claims": len(em["claims"]),
            "interpretation_reasoning_coverage": (
                round(len(reasoning_covered) / len(interp_claims), 4)
                if interp_claims else 1.0),
            "visual_bindings": len(all_bindings),
            "unresolved": len(em.get("unresolved", [])),
        },
        "warnings": warnings,
        "blockers": blockers,
    }
    models.validate_verification_report(vr)
    vr_sha = models.write_json(run_dir / "verification-report.json", vr)
    receipt = {
        "schema": models.WORK_RECEIPT_SCHEMA,
        "run_id": run_id,
        "task_status": status,
        "base_revision": frozen_brief["base_revision"],
        "retrieval_version": models.RETRIEVAL_VERSION,
        "artifacts": {
            "brief_sha256": manifest["brief_sha256"],
            "plan_sha256": manifest["plan_sha256"],
            "evidence_package_sha256":
                manifest["evidence_package_sha256"],
            "evidence_map_sha256": manifest["evidence_map_sha256"],
            "storyboard_sha256": manifest["storyboard_sha256"],
        },
        "verification": dict(vr["metrics"]),
        "changed": [],
        "unresolved": list(em.get("unresolved", [])),
        "proposals": [],
        "handoff": {
            "next_agent_should_know": [
                "status " + status + (": " + "; ".join(blockers)
                                      if blockers else ""),
                *(["unresolved: " + "; ".join(
                    i.get("section_id", "?") + " " + i.get("kind", "?")
                    + " " + i.get("reason", "")
                    for i in em.get("unresolved", []))] if em.get(
                        "unresolved") else []),
                "DONE requires explicit human approval; "
                "the verifier cannot set it",
            ],
        },
        "generated_at": now_iso(),
    }
    models.validate_work_receipt(receipt)
    wr_sha = models.write_json(run_dir / "work-receipt.json", receipt)
    models.update_manifest(run_dir, verification_sha256=vr_sha,
                           work_receipt_sha256=wr_sha,
                           state="VERIFIED")
    return {"run_id": run_id, "status": status,
            "gates": gates,
            "blockers": len(blockers), "warnings": len(warnings),
            "verification_sha256": vr_sha, "work_receipt_sha256": wr_sha,
            "state": "VERIFIED"}
