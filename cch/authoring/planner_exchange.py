"""Planner exchange: freeze brief into a plan job; import result atomically.

The job embeds the frozen brief and its manifest hash; the result must
match job_id + hash and carry sections with retrieval intents only.
Import is validate-all-then-apply: any invalid section aborts the whole
import with zero partial writes.
"""
from __future__ import annotations
import json
import uuid
from pathlib import Path

from ..core import CCHError, require
from ..store import Home, canon_digest, now_iso
from . import brief as brief_mod
from . import models

_RESULT_FIELDS = {"schema", "job_id", "manifest_sha256", "sections"}
_SECTION_FIELDS = {"section_id", "purpose", "audience_question",
                   "required_claim_types", "retrieval_queries"}
_QUERY_CHANNELS = {"cognition", "artifact"}


def _reject_unknown_fields(obj, allowed, label):
    """Strict-schema guard: any unknown field is a validation error, not a
    warning. This is what keeps planner model priors (answers, drafts,
    fact claims) out of the frozen plan."""
    extra = sorted(set(obj) - set(allowed))
    require(not extra,
            label + " has forbidden/unknown fields: " + ", ".join(extra))

def build_plan_job(home, run_id):
    home = home if isinstance(home, Home) else Home(home)
    run_dir = models.run_dir_for(home, run_id)
    manifest = brief_mod.require_state(run_dir, "BRIEF_FROZEN", "PLAN_PENDING")
    frozen_brief = brief_mod.load_frozen_brief(run_dir)
    job_id = "AUTHPLAN-" + uuid.uuid4().hex[:12]
    job = {
        "schema": models.PLAN_JOB_SCHEMA,
        "job_id": job_id,
        "run_id": run_id,
        "created_at": now_iso(),
        "brief_sha256": manifest["brief_sha256"],
        "manifest_sha256": canon_digest(frozen_brief),
        "instructions": {
            "use_external_knowledge": False,
            "do_not_write_factual_answers": True,
            "output_retrieval_intents_only": True,
        },
        "brief": frozen_brief,
    }
    path = run_dir / "plan-job.json"
    sha = models.write_json(path, job)
    models.update_manifest(run_dir, plan_job_sha256=sha, state="PLAN_PENDING")
    return {"job_id": job_id, "job_path": str(path), "plan_job_sha256": sha}


def _validate_result_shape(result, job):
    require(isinstance(result, dict), "plan result must be a mapping")
    _reject_unknown_fields(result, _RESULT_FIELDS, "plan result")
    require(result.get("schema") == models.PLAN_RESULT_SCHEMA,
            "result schema must be " + models.PLAN_RESULT_SCHEMA)
    require(result.get("job_id") == job["job_id"],
            "result job_id mismatch")
    brief = job["brief"]
    require(canon_digest(brief) == job["manifest_sha256"],
            "job brief manifest hash mismatch")
    require(result.get("manifest_sha256") == job["manifest_sha256"],
            "result manifest_sha256 mismatch with job")
    sections = result.get("sections")
    require(isinstance(sections, list) and sections,
            "result sections must be a non-empty list")
    for sec in sections:
        require(isinstance(sec, dict), "result section must be a mapping")
        _reject_unknown_fields(sec, _SECTION_FIELDS,
                               "section " + str(sec.get("section_id")))
        queries = sec.get("retrieval_queries")
        require(isinstance(queries, dict),
                "retrieval_queries must be a mapping")
        _reject_unknown_fields(queries, _QUERY_CHANNELS,
                               "retrieval_queries of section "
                               + str(sec.get("section_id")))
    plan_skeleton = {
        "schema": models.PLAN_SCHEMA,
        "brief_id": brief["brief_id"],
        "sections": result.get("sections"),
    }
    return plan_skeleton


def import_plan_result(home, run_id, result_path):
    """Validate the planner result fully, then apply in one pass.

    On any validation failure nothing is written: the previous run state
    (including a prior plan.json, if any) stays intact.
    """
    run_dir = models.run_dir_for(home, run_id)
    home = home if isinstance(home, Home) else Home(home)
    brief_mod.require_state(run_dir, "PLAN_PENDING", "PLAN_FROZEN")
    manifest = models.read_manifest(run_dir)
    job = models.load_json(run_dir / "plan-job.json")
    if job.get("schema") != models.PLAN_JOB_SCHEMA:
        raise CCHError("VALIDATION_ERROR", "plan job schema mismatch")
    require(canon_digest(job) == manifest.get("plan_job_sha256"),
            "plan job changed after issuance; refusing import")
    require(job.get("run_id") == run_id, "plan job run_id mismatch")
    result = models.load_json(result_path)
    skeleton = _validate_result_shape(result, job)
    frozen_brief = brief_mod.load_frozen_brief(run_dir)
    max_sections = frozen_brief["constraints"]["max_sections"]
    skeleton["sections"] = result.get("sections")
    models.validate_plan(skeleton)
    sections = skeleton["sections"]
    require(len(sections) <= max_sections,
            "plan has more sections than brief max_sections")
    for sec in sections:
        ids = set(sec["retrieval_queries"].get("cognition", []))
        ids.update(sec["retrieval_queries"].get("artifact", []))
        for q in ids:
            require(len(q) <= 200,
                    "retrieval query exceeds 200 chars: " + q[:40])
    plan = skeleton
    plan_sha = models.write_json(run_dir / "plan.json", plan)
    result_sha = models.write_json(run_dir / "plan-result.json", result)
    models.update_manifest(run_dir, plan_sha256=plan_sha,
                           plan_result_sha256=result_sha, state="PLAN_FROZEN")
    return {"run_id": run_id, "sections": len(sections),
            "plan_sha256": plan_sha, "plan_result_sha256": result_sha,
            "state": "PLAN_FROZEN"}
