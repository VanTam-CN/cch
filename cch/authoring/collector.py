"""Evidence collector: cognition + source + artifact Top-K per section.

Deterministic, read-only over the Cognitive Base and the artifact index.
Cognition items come only from governance-approved ACTIVE objects of the
approved revision; sources come only from recorded provenance/evidence
refs and artifact source metadata; gaps become missing[] entries, never
model-supplied filler. Artifact retrieval is the frozen v0.4.0 search.
"""
from __future__ import annotations
import json
from pathlib import Path

from ..core import CCHError, governance, instant, require, retrieve
from ..snapshots import load_base
from ..store import Home, approved_commit, now_iso
from . import brief as brief_mod
from . import models


def _collect_cognition(base, states, oid, evidence_id):
    o = base.objects[oid]
    return {
        "evidence_id": evidence_id,
        "object_id": oid,
        "type": o["type"],
        "status": states[oid]["effective_status"],
        "content": o["content"],
        "authority": o["authority"],
        "effective_at": o["lifecycle"].get("valid_from"),
        "scope_domain": o["scope"]["domain"],
        "scope_project": o["scope"]["project"],
        "source_refs": list(o["provenance"]["evidence_refs"]),
    }


def _collect_sources(base, prov_map, cognition_items, section_id):
    """Resolve source evidence for cognition items.

    Only recorded evidence refs become sources. A cognition item with no
    refs, or refs without provenance records, produces a missing entry;
    models never invent excerpts.
    """
    sources, missing = [], []
    for item in cognition_items:
        refs = item["source_refs"]
        if not refs:
            missing.append({"section_id": section_id,
                            "kind": "source_evidence",
                            "ref": item["evidence_id"],
                            "reason": "cognition has no recorded source refs"})
            continue
        for ref in refs:
            prov = prov_map.get(ref)
            if prov is None:
                missing.append({"section_id": section_id,
                                "kind": "source_evidence",
                                "ref": ref,
                                "reason": "no provenance record in approved tree"})
                continue
            sources.append({
                "evidence_id": ref,
                "derived_from": item["evidence_id"],
                "source_file": prov.get("source_name") or prov.get("source_id", ""),
                "source_sha256": prov.get("source_sha256", ""),
                "page_or_section": prov.get("block_id"),
                "excerpt": (base.evidence_text.get(ref) or "")[:500],
                "currentness": "not_assessed",
                "currentness_basis": "referenced_by_current_cognition",
            })
    return sources, missing


def _artifact_entry(hit, rank):
    asset = hit["asset"]
    return {
        "asset_id": asset["asset_id"],
        "query_rank": rank,
        "title": asset.get("title", ""),
        "summary": asset.get("summary", ""),
        "source_file": asset.get("source_file", ""),
        "page_or_slide": asset.get("page_or_slide"),
        "asset_status": asset.get("asset_status", "unknown"),
        "visual_type": asset.get("visual_type", ""),
        "why": hit.get("why", []),
        "asset_sha256": asset.get("sha256", ""),
        "source_sha256": asset.get("document_sha256", ""),
    }


def collect(home, run_id, artifact_limit=10):
    """Run deterministic collection for every plan section.

    Writes evidence-package.json and freezes the run into EVIDENCE_FROZEN.
    Cognitive Base and artifact index are read-only; the only writes land
    inside the authoring run directory.
    """
    run_dir = models.run_dir_for(home, run_id)
    # Freeze semantics: PLAN_FROZEN -> EVIDENCE_FROZEN, one way. A run
    # that is already EVIDENCE_FROZEN must never be recomputed or
    # overwritten; downstream map/storyboard stages bind to the package
    # hash, so a silent re-collect would break the provenance chain.
    brief_mod.require_state(run_dir, "PLAN_FROZEN")
    plan = models.load_json(run_dir / "plan.json")
    frozen_brief = brief_mod.load_frozen_brief(run_dir)
    if frozen_brief["constraints"].get("allow_superseded_assets"):
        raise CCHError("VALIDATION_ERROR",
                       "v0.5.0-dev collector requires allow_superseded_assets=false")
    revision, base = load_base(home)
    require(revision == frozen_brief["base_revision"],
            "approved base moved since brief freeze; new revision required")
    prov_dir = base.root / "provenance"
    prov_map = {}
    if prov_dir.is_dir():
        for p in sorted(prov_dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeError):
                raise CCHError("SNAPSHOT_INVALID",
                               "unreadable provenance record: " + p.name)
            require(isinstance(rec, dict) and rec.get("evidence_id"),
                    "invalid provenance record " + p.name)
            prov_map[rec["evidence_id"]] = rec
    as_of = instant(frozen_brief["frozen_at"][:19] + "+00:00")
    scope = frozen_brief["scope"]
    states = governance(base, scope["domain"], scope["project"],
                        frozen_brief.get("requested_topics", []), as_of)
    from ..artifact.index import search
    sections_out, retrieval_observations = [], []
    for sec in plan["sections"]:
        sid = sec["section_id"]
        cognition, missing = [], []
        seen_cog = set()
        for qi, query in enumerate(sec["retrieval_queries"].get("cognition", [])):
            hits = retrieve(query, base, states, {"evidence_words": []})
            for hit in hits[:10]:
                oid = hit["id"]
                if oid in seen_cog:
                    continue
                seen_cog.add(oid)
                st = states.get(oid, {})
                if not (st.get("eligible") and st.get("effective_status") == "ACTIVE"):
                    continue
                cognition.append(_collect_cognition(base, states, oid,
                                                    "COG-" + oid + "-" + str(qi)))
        sources, cog_missing = _collect_sources(base, prov_map, cognition, sid)
        missing.extend(cog_missing)
        artifacts = []
        seen_assets = set()
        artifact_queries = sec["retrieval_queries"].get("artifact", [])
        for qi, query in enumerate(artifact_queries, start=1):
            try:
                result = search(home.root, query,
                                domain=scope["domain"], project=scope["project"],
                                limit=artifact_limit, strict_scope=True,
                                include_superseded=False)
            except SystemExit as exc:
                retrieval_observations.append({
                    "section_id": sid, "query": query,
                    "query_index": qi,
                    "observation": "search rejected query: " + str(exc)})
                continue
            if not result.get("results"):
                retrieval_observations.append({
                    "section_id": sid, "query": query,
                    "query_index": qi,
                    "observation": "search returned no candidates"})
            for rank, hit in enumerate(result.get("results", []), start=1):
                aid = hit["asset"]["asset_id"]
                if aid in seen_assets:
                    continue
                seen_assets.add(aid)
                entry = _artifact_entry(hit, rank)
                entry["query"] = query
                entry["query_index"] = qi
                artifacts.append(entry)
        if not cognition and not artifacts:
            missing.append({"section_id": sid, "kind": "retrieval",
                            "ref": sid,
                            "reason": "no cognition and no artifact candidates found"})
        sections_out.append({"section_id": sid, "cognition": cognition,
                             "sources": sources, "artifacts": artifacts,
                             "conflicts": [], "missing": missing})
    pkg = {
        "schema": models.EVIDENCE_PACKAGE_SCHEMA,
        "run_id": run_id,
        "brief_id": frozen_brief["brief_id"],
        "base_revision": revision,
        "retrieval_version": models.RETRIEVAL_VERSION,
        "created_at": now_iso(),
        "sections": sections_out,
    }
    if retrieval_observations:
        pkg["retrieval_observations"] = retrieval_observations
    models.validate_evidence_package(pkg)
    pkg_sha = models.write_json(run_dir / "evidence-package.json", pkg)
    models.update_manifest(run_dir, evidence_package_sha256=pkg_sha,
                           state="EVIDENCE_FROZEN")
    totals = {
        "cognition": sum(len(s["cognition"]) for s in sections_out),
        "sources": sum(len(s["sources"]) for s in sections_out),
        "artifacts": sum(len(s["artifacts"]) for s in sections_out),
        "missing": sum(len(s["missing"]) for s in sections_out),
    }
    return {"run_id": run_id, "evidence_package_sha256": pkg_sha,
            "state": "EVIDENCE_FROZEN", "totals": totals}
