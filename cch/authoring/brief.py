"""Authoring brief lifecycle: DRAFT -> FROZEN, revisions never overwrite."""
from __future__ import annotations
from pathlib import Path

from ..core import CCHError, require
from ..store import Home, approved_commit, now_iso
from . import models


def _run(run_dir):
    return Path(run_dir)


def init_run(home, brief_path, run_id=None):
    """Create a run, freeze the brief into it; returns run_id + hashes."""
    home = home if isinstance(home, Home) else Home(home)
    brief = models.load_json(brief_path)
    if brief.get("schema") != models.BRIEF_SCHEMA:
        raise CCHError("VALIDATION_ERROR",
                       "input brief schema must be " + models.BRIEF_SCHEMA
                       + " (submit DRAFT, run freezes it)")
    models.validate_brief(brief)
    require(brief.get("status", "DRAFT") == "DRAFT",
            "brief must be DRAFT (status is set by freeze)")
    if run_id:
        run_dir = models.run_dir_for(home, run_id)
    else:
        run_id, run_dir = models.new_run_dir(home)
    existing = run_dir / "brief.json"
    if existing.is_file():
        raise CCHError("RUN_EXISTS",
                       "run already has a brief; create a new revision instead")
    frozen = dict(brief)
    frozen["status"] = "FROZEN"
    frozen["frozen_at"] = now_iso()
    frozen["base_revision"] = approved_commit(home) or frozen.get("base_revision", "0" * 40)
    brief_sha = models.write_json(existing, frozen)
    models.update_manifest(run_dir, run_id=run_id,
                           brief_sha256=brief_sha,
                           base_revision=frozen["base_revision"],
                           retrieval_version=models.RETRIEVAL_VERSION,
                           state="BRIEF_FROZEN")
    return {"run_id": run_id, "run_dir": str(run_dir),
            "brief_sha256": brief_sha, "status": "FROZEN"}


def load_frozen_brief(run_dir):
    brief = models.load_json(_run(run_dir) / "brief.json")
    require(brief.get("status") == "FROZEN", "brief is not FROZEN")
    return brief


def require_state(run_dir, *states):
    manifest = models.read_manifest(run_dir)
    if manifest.get("state") not in states:
        raise CCHError("INVALID_STATE",
                       "run state is " + str(manifest.get("state"))
                       + ", expected one of " + str(list(states)))
    return manifest
