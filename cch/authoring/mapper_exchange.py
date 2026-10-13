"""Mapper exchange: build-map-job / import-map-result / build-storyboard.

Same discipline as the planner exchange, tightened by the D-RC: the job
is fully self-contained (frozen brief + plan + evidence package are
embedded in full, with digests re-checked against the run manifest), so
a mapper agent needs only map-job.json. Import validates section-locally
(a claim/binding may only use evidence, assets and claims of its own
section) and enforces minimum evidence: every claim cites at least one
package evidence item; every visual binding supports at least one claim
with a non-empty reason. build_storyboard re-verifies the frozen hash
chain (plan, package, evidence map) before deriving the storyboard
deterministically (no LLM); rendering is out of scope for C0-D.
"""
from __future__ import annotations
import uuid
from ..core import CCHError, require
from ..store import canon_digest, now_iso
from ..store import Home
from . import models
from . import brief as brief_mod

_MAP_RESULT_FIELDS = {"schema", "job_id", "manifest_sha256", "claims",
                      "visual_bindings", "unresolved"}
_MAP_CLAIM_FIELDS = {"claim_id", "section_id", "claim_type", "text",
                     "evidence", "confidence", "currentness", "reasoning"}
_EVIDENCE_REF_FIELDS = {"evidence_id", "relation"}
_BINDING_FIELDS = {"section_id", "asset_id", "role", "supports_claim_ids",
                   "reason"}
_UNRESOLVED_FIELDS = {"section_id", "kind", "reason"}


def _reject_unknown_fields(obj, allowed, label):
    extra = sorted(set(obj) - set(allowed))
    require(not extra,
            label + " has forbidden/unknown fields: " + ", ".join(extra))


def _frozen_package(run_dir, map_doc):
    pkg = models.load_json(run_dir / "evidence-package.json")
    require(models.canon_digest(pkg) == map_doc["evidence_package_sha256"],
            "map evidence_package_sha256 mismatch with frozen package")
    return pkg


def build_map_job(home, run_id):
    home = home if isinstance(home, Home) else Home(home)
    run_dir = models.run_dir_for(home, run_id)
    brief_mod.require_state(run_dir, "EVIDENCE_FROZEN")
    manifest = models.read_manifest(run_dir)
    frozen_brief = brief_mod.load_frozen_brief(run_dir)
    frozen_plan = models.load_json(run_dir / "plan.json")
    frozen_package = models.load_json(run_dir / "evidence-package.json")
    require(canon_digest(frozen_brief) == manifest.get("brief_sha256"),
            "frozen brief changed after freeze; manifest brief_sha256 mismatch")
    require(canon_digest(frozen_plan) == manifest.get("plan_sha256"),
            "frozen plan changed after freeze; manifest plan_sha256 mismatch")
    package_sha = manifest.get("evidence_package_sha256")
    require(package_sha, "run has no frozen evidence package hash")
    require(canon_digest(frozen_package) == package_sha,
            "frozen evidence package changed after freeze; manifest "
            "evidence_package_sha256 mismatch")
    job = {
        "schema": models.MAP_JOB_SCHEMA,
        "job_id": "AUTHMAP-" + uuid.uuid4().hex[:12],
        "run_id": run_id,
        "created_at": now_iso(),
        "evidence_package_sha256": package_sha,
        "manifest_sha256": package_sha,
        "brief_sha256": canon_digest(frozen_brief),
        "plan_sha256": manifest["plan_sha256"],
        "base_revision": frozen_brief["base_revision"],
        "retrieval_version": models.RETRIEVAL_VERSION,
        "brief": frozen_brief,
        "plan": frozen_plan,
        "evidence_package": frozen_package,
        "instructions": {
            "use_external_knowledge": False,
            "claims_cite_package_evidence_only": True,
            "artifact_is_not_fact_authority": True,
            "unsupported_interpretation_is_forbidden": True,
            "artifact_metadata_visual_discovery_only": (
                "Artifact/VLM metadata is for visual discovery and "
                "context only. If a proposition exists only in artifact "
                "metadata and no cognition/source evidence supports it, "
                "do not form a FACT_* or INTERPRETATION claim for it; "
                "route it to unresolved when an answer is required, or "
                "describe it in a visual binding reason."),
            "relation_polarity": {
                "supports": "positive grounding",
                "illustrates": "non-grounding illustration",
                "contextualizes": "non-grounding context",
                "contradicts": ("negative evidence; the claim must also "
                                "carry a section unresolved item with "
                                "kind=conflict, or import is rejected"),
            },
            "interpretation_grounding": (
                "INTERPRETATION claims must carry a reasoning mapping "
                "whose from_evidence_ids subset this claim's evidence "
                "and include at least one relation=supports ref; "
                "contextualizes/illustrates alone cannot ground an "
                "interpretation."),
        },
    }
    sha = models.write_json(run_dir / "map-job.json", job)
    models.update_manifest(run_dir, map_job_sha256=sha, state="MAPPING")
    return {"job_id": job["job_id"], "job_path": str(run_dir / "map-job.json"),
            "evidence_package_sha256": job["evidence_package_sha256"],
            "manifest_sha256": job["manifest_sha256"],
            "map_job_sha256": sha}


def _validate_map_result(result, job, pkg):
    require(isinstance(result, dict), "map result must be a mapping")
    _reject_unknown_fields(result, _MAP_RESULT_FIELDS, "map result")
    require(result.get("schema") == models.MAP_RESULT_SCHEMA,
            "map result schema must be " + models.MAP_RESULT_SCHEMA)
    require(result.get("job_id") == job["job_id"], "map job_id mismatch")
    require(result.get("manifest_sha256") == job["manifest_sha256"],
            "map manifest_sha256 mismatch with job")
    claims = result.get("claims")
    require(isinstance(claims, list), "map result claims must be a list")
    for claim in claims:
        require(isinstance(claim, dict), "map claim must be a mapping")
        _reject_unknown_fields(claim, _MAP_CLAIM_FIELDS, "map claim")
        refs = claim.get("evidence")
        require(isinstance(refs, list), "map claim.evidence must be a list")
        require(refs, "map claim must cite at least one evidence item; "
                "unsupported content belongs in unresolved")
        for ref in refs:
            require(isinstance(ref, dict), "evidence ref must be a mapping")
            _reject_unknown_fields(ref, _EVIDENCE_REF_FIELDS,
                                   "evidence ref")
    for binding in result.get("visual_bindings", []):
        require(isinstance(binding, dict), "visual binding must be a mapping")
        _reject_unknown_fields(binding, _BINDING_FIELDS, "visual binding")
        require(binding.get("supports_claim_ids"),
                "visual binding must support at least one claim")
        require(isinstance(binding.get("reason"), str) and
                binding["reason"].strip(),
                "visual binding reason must be a non-empty string")
    unresolved = result.get("unresolved", [])
    require(isinstance(unresolved, list),
            "map result unresolved must be a list")
    for item in unresolved:
        require(isinstance(item, dict), "unresolved item must be a mapping")
        _reject_unknown_fields(item, _UNRESOLVED_FIELDS, "unresolved item")
        require(item.get("kind") in models.UNRESOLVED_KINDS,
                "unresolved kind must be one of "
                + str(sorted(models.UNRESOLVED_KINDS)))
        require(isinstance(item.get("reason"), str)
                and item["reason"].strip(),
                "unresolved reason must be a non-empty string")
    require(claims or unresolved,
            "map result must contain claims or unresolved items; "
            "an empty result is not acceptable")
    plan_sids = {sec["section_id"] for sec in job["plan"]["sections"]}
    claim_sids = {c.get("section_id") for c in claims}
    unresolved_sids = {i.get("section_id") for i in unresolved}
    unknown = (claim_sids | unresolved_sids) - plan_sids
    require(not unknown,
            "result references sections outside the plan: "
            + ", ".join(sorted(str(s) for s in unknown)))

    skeleton = {
        "schema": models.EVIDENCE_MAP_SCHEMA,
        "run_id": job["run_id"],
        "evidence_package_sha256": job["evidence_package_sha256"],
        "claims": claims,
        "visual_bindings": result.get("visual_bindings", []),
        "unresolved": result.get("unresolved", []),
    }
    models.validate_evidence_map(skeleton)

    sections_by_id = _package_sections_by_id(pkg)
    for claim in skeleton["claims"]:
        section = sections_by_id.get(claim["section_id"])
        require(section is not None,
                "claim references unknown section " + claim["section_id"])
        section_evidence = _section_evidence_ids(section)
        for ref in claim["evidence"]:
            require(ref["evidence_id"] in section_evidence,
                    "claim cites evidence outside its own section: "
                    + ref["evidence_id"])
    claim_ids_by_section = {}
    for claim in skeleton["claims"]:
        claim_ids_by_section.setdefault(claim["section_id"],
                                        set()).add(claim["claim_id"])
    # F: contradicts is negative evidence. A claim citing it must carry
    # a same-section unresolved conflict in the same result, otherwise
    # the import is rejected; with the conflict present the map can
    # freeze and the verifier must return BLOCKED (never READY).
    contradicts_sids = {c["section_id"] for c in skeleton["claims"]
                        if any(r["relation"] == "contradicts"
                               for r in c["evidence"])}
    conflict_sids = {i["section_id"] for i in skeleton["unresolved"]
                     if i["kind"] == "conflict"}
    missing_conflicts = sorted(contradicts_sids - conflict_sids)
    require(not missing_conflicts,
            "claims citing relation=contradicts require an unresolved "
            "kind=conflict item in the same section: "
            + ", ".join(missing_conflicts))
    for binding in skeleton["visual_bindings"]:
        section = sections_by_id.get(binding["section_id"])
        require(section is not None,
                "visual binding references unknown section "
                + binding["section_id"])
        require(binding["asset_id"] in _section_asset_ids(section),
                "visual binding references asset outside its own section: "
                + binding["asset_id"])
        same_section_claims = claim_ids_by_section.get(
            binding["section_id"], set())
        for cid in binding["supports_claim_ids"]:
            require(cid in same_section_claims,
                    "visual binding supports claim from another section: "
                    + cid)
    return skeleton


def _package_sections_by_id(pkg):
    return {sec["section_id"]: sec for sec in pkg["sections"]}


def _section_evidence_ids(section):
    ids = {item["evidence_id"] for item in section["cognition"]}
    ids.update(item["evidence_id"] for item in section["sources"])
    return ids


def _section_asset_ids(section):
    return {item["asset_id"] for item in section["artifacts"]}


def import_map_result(home, run_id, result_path):
    """Validate the mapper result fully, then apply in one pass.

    On any validation failure nothing is written: the previous run state
    (including a prior evidence-map.json, if any) stays intact.
    """
    run_dir = models.run_dir_for(home, run_id)
    home = home if isinstance(home, Home) else Home(home)
    brief_mod.require_state(run_dir, "MAPPING")
    manifest = models.read_manifest(run_dir)
    job = models.load_json(run_dir / "map-job.json")
    require(job.get("schema") == models.MAP_JOB_SCHEMA,
            "map job schema mismatch")
    require(canon_digest(job) == manifest.get("map_job_sha256"),
            "map job changed after issuance; refusing import")
    require(job.get("run_id") == run_id, "map job run_id mismatch")
    result = models.load_json(result_path)
    pkg = models.load_json(run_dir / "evidence-package.json")
    require(models.canon_digest(pkg) == job["evidence_package_sha256"],
            "frozen package changed after map job was issued")
    skeleton = _validate_map_result(result, job, pkg)
    map_sha = models.write_json(run_dir / "evidence-map.json", skeleton)
    result_sha = models.write_json(run_dir / "map-result.json", result)
    models.update_manifest(run_dir, map_result_sha256=result_sha,
                           evidence_map_sha256=map_sha,
                           state="MAP_FROZEN")
    return {"evidence_map_sha256": map_sha, "state": "MAP_FROZEN",
            "claims": len(skeleton["claims"]),
            "visual_bindings": len(skeleton["visual_bindings"])}


def build_storyboard(home, run_id):
    """Derive storyboard/1 deterministically from the frozen evidence map."""
    run_dir = models.run_dir_for(home, run_id)
    home = home if isinstance(home, Home) else Home(home)
    brief_mod.require_state(run_dir, "MAP_FROZEN")
    manifest = models.read_manifest(run_dir)
    plan = models.load_json(run_dir / "plan.json")
    em = models.load_json(run_dir / "evidence-map.json")
    pkg = models.load_json(run_dir / "evidence-package.json")
    require(canon_digest(plan) == manifest.get("plan_sha256"),
            "frozen plan changed after map import; refusing storyboard")
    require(canon_digest(pkg) == manifest.get("evidence_package_sha256"),
            "frozen evidence package changed after map import; "
            "refusing storyboard")
    require(canon_digest(em) == manifest.get("evidence_map_sha256"),
            "frozen evidence map was tampered with; refusing storyboard")

    purpose_by_sid = {sec["section_id"]: sec.get("purpose", "")
                      for sec in plan["sections"]}
    claims_by_sid, visuals_by_sid = {}, {}
    for claim in em["claims"]:
        claims_by_sid.setdefault(claim["section_id"], []).append(claim)
    for binding in em.get("visual_bindings", []):
        visuals_by_sid.setdefault(binding["section_id"], []).append(binding)

    unresolved_by_sid = {}
    for item in em.get("unresolved", []):
        unresolved_by_sid.setdefault(item["section_id"], []).append(item)
    sections = []
    for sid in [sec["section_id"] for sec in plan["sections"]]:
        claims = claims_by_sid.get(sid, [])
        audience_question = ""
        for plan_sec in plan["sections"]:
            if plan_sec["section_id"] == sid:
                audience_question = plan_sec.get("audience_question") or ""
                break
        if not claims and unresolved_by_sid.get(sid):
            sections.append({
                "section_id": sid,
                "purpose": purpose_by_sid.get(sid, ""),
                "audience_takeaway": "证据不足，当前无法形成经验证结论",
                "headline": audience_question
                or purpose_by_sid.get(sid, "") or sid,
                "claim_ids": [],
                "visuals": [],
                "narrative_order": ["context"],
                "transition_to_next": "",
            })
            continue
        if not claims:
            continue
        evidence_n = sum(len(c["evidence"]) for c in claims)
        narrative = ["problem", "evidence" if evidence_n else "context",
                     "implication"]
        visuals = [{"asset_id": b["asset_id"], "role": b["role"]}
                   for b in visuals_by_sid.get(sid, [])]
        idx = len(sections)
        sections.append({
            "section_id": sid,
            "purpose": purpose_by_sid.get(sid, ""),
            "audience_takeaway": "；".join(c["text"] for c in claims)[:2000],
            "headline": audience_question or purpose_by_sid.get(sid, "") or sid,
            "claim_ids": [c["claim_id"] for c in claims],
            "visuals": visuals,
            "narrative_order": narrative,
            "transition_to_next": "",
        })
        if idx > 0:
            prev_sid = sections[idx - 1]["section_id"]
            prev_purpose = purpose_by_sid.get(prev_sid, "")
            next_purpose = purpose_by_sid.get(sid, "")
            sections[idx - 1]["transition_to_next"] = (
                "衔接下一部分：" + next_purpose) if next_purpose else (
                "承接上句：" + prev_purpose)
    require(sections, "no section has claims; storyboard would be empty")
    sb = {"schema": models.STORYBOARD_SCHEMA,
          "run_id": run_id,
          "sections": sections}
    models.validate_storyboard(sb)
    sb_sha = models.write_json(run_dir / "storyboard.json", sb)
    models.update_manifest(run_dir, storyboard_sha256=sb_sha,
                           state="STORYBOARD_FROZEN")
    return {"storyboard_sha256": sb_sha, "state": "STORYBOARD_FROZEN",
            "sections": len(sections)}
