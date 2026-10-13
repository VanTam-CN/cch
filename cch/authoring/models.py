"""Frozen authoring contracts and run storage.

Schemas (all validated mechanically before any file lands in a run):

  authoring-brief/1        the request: what material, for whom, why
  authoring-plan/1         section split + retrieval intents (no answers)
  authoring-plan-job/1     planner exchange job bound to the frozen brief
  authoring-plan-result/1  planner return, validated against the job
  evidence-package/1       deterministic collection result, frozen at write
  evidence-map/2           claim/evidence binding; /1 stays readable for
                           frozen pilot artifacts but is never written
  storyboard/1             section narrative order, produced in C0-D
  authoring-verification/1 hard-gate verification report (C0-E)
  work-receipt/1           done-contract receipt for the authoring run

Every object carries a sha256 in the run manifest; downstream stages bind
to hashes, not paths.
"""
from __future__ import annotations
import json
from pathlib import Path

from ..core import CCHError, require
from ..store import canon, canon_digest

BRIEF_SCHEMA = "authoring-brief/1"
PLAN_SCHEMA = "authoring-plan/1"
PLAN_JOB_SCHEMA = "authoring-plan-job/1"
PLAN_RESULT_SCHEMA = "authoring-plan-result/1"
MAP_JOB_SCHEMA = "authoring-map-job/2"
MAP_RESULT_SCHEMA = "authoring-map-result/2"
EVIDENCE_PACKAGE_SCHEMA = "evidence-package/1"
EVIDENCE_MAP_SCHEMA = "evidence-map/2"
EVIDENCE_MAP_V1_SCHEMA = "evidence-map/1"
STORYBOARD_SCHEMA = "storyboard/1"
VERIFICATION_SCHEMA = "authoring-verification/1"
WORK_RECEIPT_SCHEMA = "work-receipt/1"

PURPOSES = {"explain", "decision_support", "status_report",
            "technical_review", "proposal", "training"}
DELIVERABLES = {"leadership_brief", "technical_brief", "project_summary",
                "solution_overview", "review_material"}
CLAIM_TYPES = {"FACT_CURRENT", "FACT_HISTORICAL",
               "INTERPRETATION", "RECOMMENDATION"}
EVIDENCE_RELATIONS = {"supports", "illustrates", "contextualizes",
                      "contradicts"}
EVIDENCE_POSITIVE_RELATIONS = {"supports"}
EVIDENCE_NEUTRAL_RELATIONS = {"illustrates", "contextualizes"}
EVIDENCE_NEGATIVE_RELATIONS = {"contradicts"}
VISUAL_ROLES = {"primary_visual", "supporting_visual", "context_visual"}
UNRESOLVED_KINDS = {"missing_evidence", "conflict",
                    "currentness_unconfirmed", "unsupported_request"}
VERIFICATION_STATES = {"BLOCKED", "PARTIALLY_VERIFIED", "READY_FOR_REVIEW",
                       "DONE"}
STORYBOARD_NARRATIVE_STEPS = {"problem", "context", "evidence",
                              "implication", "recommendation", "next"}
RETRIEVAL_VERSION = "v0.4.0"

_RUN_SEQ = {}


def _require_str(value, label, max_len=2000):
    require(isinstance(value, str) and value.strip(),
            label + " must be a non-empty string")
    require(len(value) <= max_len, label + " exceeds length limit")


def _require_str_list(value, label, max_items=100):
    require(isinstance(value, list) and
            all(isinstance(x, str) and x.strip() for x in value),
            label + " must be a list of non-empty strings")
    require(len(value) <= max_items, label + " exceeds item limit")


def validate_brief(brief):
    """Validate authoring-brief/1. Raises CCHError(VALIDATION_ERROR)."""
    require(isinstance(brief, dict), "brief must be a mapping")
    require(brief.get("schema") == BRIEF_SCHEMA,
            "brief schema must be " + BRIEF_SCHEMA)
    _require_str(brief.get("brief_id"), "brief_id", 100)
    require(brief["brief_id"].startswith("BRIEF-"),
            "brief_id must start with BRIEF-")
    for field in ("title", "request", "audience"):
        _require_str(brief.get(field), field)
    require(brief.get("purpose") in PURPOSES,
            "purpose must be one of " + str(sorted(PURPOSES)))
    require(brief.get("deliverable_type") in DELIVERABLES,
            "deliverable_type must be one of " + str(sorted(DELIVERABLES)))
    scope = brief.get("scope")
    require(isinstance(scope, dict), "scope must be a mapping")
    for key in ("domain", "project"):
        _require_str(scope.get(key), "scope." + key, 200)
    constraints = brief.get("constraints")
    require(isinstance(constraints, dict), "constraints must be a mapping")
    max_sections = constraints.get("max_sections")
    require(isinstance(max_sections, int) and not isinstance(max_sections, bool)
            and 1 <= max_sections <= 50,
            "constraints.max_sections must be an int 1..50")
    for flag in ("cross_project_reuse", "allow_superseded_assets",
                 "external_knowledge"):
        require(isinstance(constraints.get(flag), bool),
                "constraints." + flag + " must be a bool")
    for field in ("requested_topics", "must_include", "must_avoid"):
        _require_str_list(brief.get(field, []), field)
    _require_str(brief.get("created_at"), "created_at", 40)
    _require_str(brief.get("base_revision"), "base_revision", 64)
    return brief


def validate_plan(plan):
    """Validate authoring-plan/1: sections + retrieval intents only."""
    require(isinstance(plan, dict), "plan must be a mapping")
    require(plan.get("schema") == PLAN_SCHEMA,
            "plan schema must be " + PLAN_SCHEMA)
    _require_str(plan.get("brief_id"), "brief_id", 100)
    sections = plan.get("sections")
    require(isinstance(sections, list) and sections,
            "plan.sections must be a non-empty list")
    require(len(sections) <= 50, "plan exceeds 50 sections")
    seen = set()
    for sec in sections:
        require(isinstance(sec, dict), "section must be a mapping")
        sid = sec.get("section_id")
        _require_str(sid, "section_id", 40)
        require(sid not in seen, "duplicate section_id " + str(sid))
        seen.add(sid)
        _require_str(sec.get("purpose"), "purpose", 2000)
        _require_str(sec.get("audience_question"), "audience_question", 500)
        req_types = sec.get("required_claim_types")
        require(isinstance(req_types, list) and req_types and
                all(t in CLAIM_TYPES for t in req_types),
                "required_claim_types must be a subset of " + str(sorted(CLAIM_TYPES)))
        require(len(req_types) == len(set(req_types)),
                "required_claim_types must not repeat")
        queries = sec.get("retrieval_queries")
        require(isinstance(queries, dict),
                "retrieval_queries must be a mapping")
        for channel in ("cognition", "artifact"):
            _require_str_list(queries.get(channel, []),
                              "retrieval_queries." + channel, 20)
        both = queries.get("cognition", []) or queries.get("artifact", [])
        require(bool(both),
                "section needs at least one cognition or artifact query")
    return plan


def validate_evidence_package(pkg):
    """Validate evidence-package/1 structural invariants (per section)."""
    require(isinstance(pkg, dict), "evidence package must be a mapping")
    require(pkg.get("schema") == EVIDENCE_PACKAGE_SCHEMA,
            "package schema must be " + EVIDENCE_PACKAGE_SCHEMA)
    _require_str(pkg.get("run_id"), "run_id", 100)
    _require_str(pkg.get("brief_id"), "brief_id", 100)
    _require_str(pkg.get("base_revision"), "base_revision", 64)
    require(pkg.get("retrieval_version") == RETRIEVAL_VERSION,
            "retrieval_version must be " + RETRIEVAL_VERSION)
    sections = pkg.get("sections")
    require(isinstance(sections, list) and sections,
            "sections must be a non-empty list")
    for sec in sections:
        require(isinstance(sec, dict), "section must be a mapping")
        _require_str(sec.get("section_id"), "section_id", 40)
        for field in ("cognition", "sources", "artifacts"):
            require(isinstance(sec.get(field), list),
                    "section." + field + " must be a list")
        require(isinstance(sec.get("missing", []), list),
                "section.missing must be a list")
        for item in sec["cognition"]:
            _validate_cognition_item(item)
        for item in sec["artifacts"]:
            _validate_artifact_item(item)
        for item in sec["sources"]:
            _require_str(item.get("evidence_id"), "evidence_id", 100)
            _require_str(item.get("source_sha256"), "source_sha256", 64)
    return pkg


def _validate_cognition_item(item):
    require(isinstance(item, dict), "cognition item must be a mapping")
    for field in ("evidence_id", "object_id", "type", "status", "content",
                  "authority"):
        _require_str(item.get(field), field, 4000)
    _require_str(item.get("scope_domain"), "scope_domain", 200)
    _require_str(item.get("scope_project"), "scope_project", 200)


def _validate_artifact_item(item):
    require(isinstance(item, dict), "artifact item must be a mapping")
    require(isinstance(item.get("asset_id"), str) and
            item["asset_id"].startswith("AST-"),
            "artifact asset_id must be an AST id")
    require(isinstance(item.get("query_rank"), int) and item["query_rank"] >= 1,
            "artifact query_rank must be a positive int")
    require(isinstance(item.get("query_index"), int) and item["query_index"] >= 1,
            "artifact query_index must be a positive int")
    _require_str(item.get("query"), "artifact query", 200)
    _require_sha256(item.get("asset_sha256"), "asset_sha256")
    _require_sha256(item.get("source_sha256"), "source_sha256")


def _require_git_commit(value, label):
    require(isinstance(value, str) and len(value) == 40 and
            all(c in "0123456789abcdef" for c in value.lower()),
            label + " must be a 40-char git commit id")


def _require_sha256(value, label):
    require(isinstance(value, str) and len(value) == 64 and
            all(c in "0123456789abcdef" for c in value.lower()),
            label + " must be a 64-char hex sha256")


def validate_evidence_map(em):
    """Validate evidence-map/2 structural invariants (frozen in C0-A)."""
    require(isinstance(em, dict), "evidence map must be a mapping")
    require(em.get("schema") == EVIDENCE_MAP_SCHEMA,
            "evidence map schema must be " + EVIDENCE_MAP_SCHEMA)
    _require_str(em.get("run_id"), "run_id", 100)
    _require_sha256(em.get("evidence_package_sha256"),
                    "evidence_package_sha256")
    claims = em.get("claims", [])
    require(isinstance(claims, list), "claims must be a list")
    seen_claims = set()
    for claim in claims:
        require(isinstance(claim, dict), "claim must be a mapping")
        cid = claim.get("claim_id")
        _require_str(cid, "claim_id", 100)
        require(cid not in seen_claims, "duplicate claim_id " + str(cid))
        seen_claims.add(cid)
        _require_str(claim.get("section_id"), "claim section_id", 40)
        require(claim.get("claim_type") in CLAIM_TYPES,
                "claim_type must be one of " + str(sorted(CLAIM_TYPES)))
        _require_str(claim.get("text"), "claim text")
        evidence = claim.get("evidence")
        require(isinstance(evidence, list), "claim.evidence must be a list")
        for ref in evidence:
            require(isinstance(ref, dict), "evidence ref must be a mapping")
            _require_str(ref.get("evidence_id"), "evidence ref id", 200)
            require(ref.get("relation") in EVIDENCE_RELATIONS,
                    "evidence relation must be one of "
                    + str(sorted(EVIDENCE_RELATIONS)))
        if claim.get("claim_type") == "INTERPRETATION":
            _validate_interpretation_reasoning(claim, evidence)
    for binding in em.get("visual_bindings", []):
        require(isinstance(binding, dict), "visual binding must be a mapping")
        _require_str(binding.get("section_id"), "binding section_id", 40)
        require(isinstance(binding.get("asset_id"), str)
                and binding["asset_id"].startswith("AST-"),
                "visual binding asset_id must be an AST id")
        require(binding.get("role") in VISUAL_ROLES,
                "visual role must be one of " + str(sorted(VISUAL_ROLES)))
        for cid in binding.get("supports_claim_ids", []):
            require(cid in seen_claims,
                    "visual binding references unknown claim " + str(cid))
    unresolved = em.get("unresolved", [])
    require(isinstance(unresolved, list), "unresolved must be a list")
    require(claims or unresolved,
            "evidence map must have claims or unresolved items; "
            "an empty map is not a valid mapper output")
    for item in unresolved:
        require(isinstance(item, dict), "unresolved item must be a mapping")
        _require_str(item.get("section_id"), "unresolved section_id", 40)
        require(item.get("kind") in UNRESOLVED_KINDS,
                "unresolved kind must be one of "
                + str(sorted(UNRESOLVED_KINDS)))
        _require_str(item.get("reason"), "unresolved reason", 4000)
    return em


def _validate_interpretation_reasoning(claim, evidence):
    """Mechanical structure check only: INTERPRETATION must carry an
    explicit reasoning chain whose basis includes at least one
    relation=supports evidence ref. Semantic entailment of the
    inference text stays with the human reviewer."""
    require(claim.get("claim_type") == "INTERPRETATION",
            "reasoning validation is for INTERPRETATION claims only")
    reasoning = claim.get("reasoning")
    require(isinstance(reasoning, dict),
            "INTERPRETATION claim " + str(claim.get("claim_id"))
            + " must carry a reasoning mapping")
    from_ids = reasoning.get("from_evidence_ids")
    require(isinstance(from_ids, list) and from_ids,
            "reasoning.from_evidence_ids must be a non-empty list")
    evidence_ids = {ref.get("evidence_id") for ref in evidence}
    for eid in from_ids:
        require(isinstance(eid, str) and eid,
                "reasoning.from_evidence_ids entries must be strings")
        require(eid in evidence_ids,
                "reasoning.from_evidence_ids reference "
                + str(eid) + " is not cited by this claim")
    supporting = {ref.get("evidence_id") for ref in evidence
                  if ref.get("relation") == "supports"}
    require(bool(set(from_ids) & supporting),
            "INTERPRETATION claim " + str(claim.get("claim_id"))
            + " needs at least one relation=supports evidence in its "
            + "reasoning basis; contextualizes/illustrates are context "
            + "only")
    inference = reasoning.get("inference")
    require(isinstance(inference, str) and inference.strip(),
            "reasoning.inference must be a non-empty string")


def read_evidence_map_any_version(path):
    """Read-only accessor for frozen evidence maps: /2 and legacy /1.

    Frozen pilot artifacts stay untouched; verification only ever runs
    on /2 maps freshly written by the mapper exchange.
    """
    data = load_json(path)
    require(isinstance(data, dict), "evidence map must be a mapping")
    schema = data.get("schema")
    require(schema in (EVIDENCE_MAP_SCHEMA, EVIDENCE_MAP_V1_SCHEMA),
            "unsupported evidence map schema: " + str(schema))
    return data


def validate_storyboard(sb):
    """Validate storyboard/1 structural invariants (frozen in C0-A)."""
    require(isinstance(sb, dict), "storyboard must be a mapping")
    require(sb.get("schema") == STORYBOARD_SCHEMA,
            "storyboard schema must be " + STORYBOARD_SCHEMA)
    _require_str(sb.get("run_id"), "run_id", 100)
    sections = sb.get("sections")
    require(isinstance(sections, list) and sections,
            "storyboard sections must be a non-empty list")
    seen = set()
    for sec in sections:
        require(isinstance(sec, dict), "storyboard section must be a mapping")
        sid = sec.get("section_id")
        _require_str(sid, "storyboard section_id", 40)
        require(sid not in seen, "duplicate storyboard section " + str(sid))
        seen.add(sid)
        _require_str(sec.get("purpose"), "storyboard purpose", 2000)
        _require_str(sec.get("audience_takeaway"), "audience_takeaway", 2000)
        _require_str(sec.get("headline"), "headline", 500)
        for cid in sec.get("claim_ids", []):
            _require_str(cid, "claim_ids entry", 100)
        for vis in sec.get("visuals", []):
            require(isinstance(vis, dict), "visual entry must be a mapping")
            require(isinstance(vis.get("asset_id"), str)
                    and vis["asset_id"].startswith("AST-"),
                    "storyboard visual asset_id must be an AST id")
            require(vis.get("role") in VISUAL_ROLES,
                    "storyboard visual role must be one of "
                    + str(sorted(VISUAL_ROLES)))
        order = sec.get("narrative_order")
        require(isinstance(order, list) and order,
                "narrative_order must be a non-empty list")
        for step in order:
            require(step in STORYBOARD_NARRATIVE_STEPS,
                    "narrative step must be one of "
                    + str(sorted(STORYBOARD_NARRATIVE_STEPS)))
    return sb


def validate_verification_report(vr):
    """Validate authoring-verification/1 structure (frozen in C0-A)."""
    require(isinstance(vr, dict), "verification report must be a mapping")
    require(vr.get("schema") == VERIFICATION_SCHEMA,
            "verification schema must be " + VERIFICATION_SCHEMA)
    _require_str(vr.get("run_id"), "run_id", 100)
    require(vr.get("status") in VERIFICATION_STATES,
            "verification status must be one of "
            + str(sorted(VERIFICATION_STATES)))
    gates = vr.get("gates")
    require(isinstance(gates, dict) and gates,
            "gates must be a non-empty mapping")
    for name, passed in gates.items():
        require(isinstance(passed, bool), "gate " + str(name) + " must be bool")
    metrics = vr.get("metrics", {})
    require(isinstance(metrics, dict), "metrics must be a mapping")
    require(isinstance(vr.get("warnings", []), list),
            "warnings must be a list")
    require(isinstance(vr.get("blockers", []), list),
            "blockers must be a list")
    return vr


def validate_work_receipt(wr):
    """Validate work-receipt/1 structure (frozen in C0-A)."""
    require(isinstance(wr, dict), "work receipt must be a mapping")
    require(wr.get("schema") == WORK_RECEIPT_SCHEMA,
            "work receipt schema must be " + WORK_RECEIPT_SCHEMA)
    _require_str(wr.get("run_id"), "run_id", 100)
    require(wr.get("task_status") in VERIFICATION_STATES,
            "task_status must be one of " + str(sorted(VERIFICATION_STATES)))
    _require_git_commit(wr.get("base_revision"), "base_revision")
    require(wr.get("retrieval_version") == RETRIEVAL_VERSION,
            "retrieval_version must be " + RETRIEVAL_VERSION)
    artifacts = wr.get("artifacts")
    require(isinstance(artifacts, dict), "artifacts must be a mapping")
    for key in ("brief_sha256", "plan_sha256",
                "evidence_package_sha256", "evidence_map_sha256",
                "storyboard_sha256"):
        _require_sha256(artifacts.get(key), "artifacts." + key)
    require(isinstance(wr.get("verification"), dict),
            "verification must be a mapping")
    handoff = wr.get("handoff")
    require(isinstance(handoff, dict), "handoff must be a mapping")
    _require_str_list(handoff.get("next_agent_should_know"),
                      "handoff.next_agent_should_know")
    require(isinstance(wr.get("generated_at"), str)
            and wr.get("generated_at").strip(),
            "generated_at must be a non-empty string")
    require(isinstance(wr.get("changed", []), list),
            "changed must be a list")
    require(isinstance(wr.get("unresolved", []), list),
            "unresolved must be a list")
    require(isinstance(wr.get("proposals", []), list),
            "proposals must be a list")
    return wr


def sha256_of(value):
    return canon_digest(value)


def load_json(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise CCHError("IO_ERROR", "cannot read " + str(path) + ": " + str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise CCHError("VALIDATION_ERROR", str(path.name) + ": invalid JSON: " + str(exc)) from exc
    return data


def write_json(path, value):
    """Atomic canonical write; returns the canonical sha256."""
    path = Path(path)
    from ..cli import atomic_write
    atomic_write(path, canon(value) + "\n")
    return sha256_of(value)


def new_run_dir(home, date=None):
    """Allocate <home>/authoring/runs/AUTH-YYYYMMDD-NNN (never reuses ids)."""
    import datetime as _dt
    from ..store import Home
    root = (home.root if isinstance(home, Home) else Path(home)) / "authoring" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    day = date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    seq = _RUN_SEQ.get(day, 0)
    while True:
        seq += 1
        run_id = "AUTH-" + day + "-" + format(seq, "03d")
        candidate = root / run_id
        if candidate.exists():
            continue
        candidate.mkdir(parents=True)
        _RUN_SEQ[day] = seq
        return run_id, candidate


def run_dir_for(home, run_id):
    from ..store import Home
    root = (home.root if isinstance(home, Home) else Path(home)) / "authoring" / "runs"
    candidate = root / run_id
    if not candidate.is_dir():
        raise CCHError("NOT_FOUND", "authoring run " + str(run_id) + " not found")
    return candidate


def read_manifest(run_dir):
    path = Path(run_dir) / "manifest.json"
    if not path.is_file():
        raise CCHError("NOT_FOUND", "run manifest missing")
    return load_json(path)


def update_manifest(run_dir, **fields):
    manifest = {}
    path = Path(run_dir) / "manifest.json"
    if path.is_file():
        manifest = load_json(path)
    manifest.update(fields)
    write_json(path, manifest)
    return manifest
