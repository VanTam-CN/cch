"""CLI wiring for cch author (called from cch.cli run_home)."""
from __future__ import annotations
from pathlib import Path

from ..core import CCHError
from . import collector, mapper_exchange, models, planner_exchange, \
    verifier
from . import brief as brief_mod
from ..store import Home


def add_parser(sub):
    p = sub.add_parser("author")
    author_sub = p.add_subparsers(dest="sub", required=True)
    init_p = author_sub.add_parser("init")
    init_p.add_argument("--brief", required=True, type=Path)
    job_p = author_sub.add_parser("build-plan-job")
    job_p.add_argument("run_id")
    imp_p = author_sub.add_parser("import-plan")
    imp_p.add_argument("run_id")
    imp_p.add_argument("result", type=Path)
    col_p = author_sub.add_parser("collect")
    col_p.add_argument("run_id")
    col_p.add_argument("--limit", type=int, default=10)
    mj_p = author_sub.add_parser("build-map-job")
    mj_p.add_argument("run_id")
    mi_p = author_sub.add_parser("import-map")
    mi_p.add_argument("run_id")
    mi_p.add_argument("result", type=Path)
    sb_p = author_sub.add_parser("build-storyboard")
    sb_p.add_argument("run_id")
    ver_p = author_sub.add_parser("verify")
    ver_p.add_argument("run_id")
    show_p = author_sub.add_parser("show")
    show_p.add_argument("run_id")


def run(args, home):
    home = home if isinstance(home, Home) else Home(home)
    if args.sub == "init":
        return brief_mod.init_run(home, args.brief)
    if args.sub == "build-plan-job":
        return planner_exchange.build_plan_job(home, args.run_id)
    if args.sub == "import-plan":
        return planner_exchange.import_plan_result(home, args.run_id, args.result)
    if args.sub == "collect":
        return collector.collect(home, args.run_id, artifact_limit=args.limit)
    if args.sub == "build-map-job":
        return mapper_exchange.build_map_job(home, args.run_id)
    if args.sub == "import-map":
        return mapper_exchange.import_map_result(home, args.run_id, args.result)
    if args.sub == "build-storyboard":
        return mapper_exchange.build_storyboard(home, args.run_id)
    if args.sub == "verify":
        return verifier.verify(home, args.run_id)
    if args.sub == "show":
        run_dir = models.run_dir_for(home, args.run_id)
        manifest = models.read_manifest(run_dir)
        out = {"run_id": args.run_id, "state": manifest.get("state"),
               "manifest": manifest}
        pkg_path = run_dir / "evidence-package.json"
        if pkg_path.is_file():
            pkg = models.load_json(pkg_path)
            out["totals"] = {
                "sections": len(pkg["sections"]),
                "cognition": sum(len(s["cognition"]) for s in pkg["sections"]),
                "sources": sum(len(s["sources"]) for s in pkg["sections"]),
                "artifacts": sum(len(s["artifacts"]) for s in pkg["sections"]),
                "missing": sum(len(s["missing"]) for s in pkg["sections"]),
            }
        return out
    raise CCHError("VALIDATION_ERROR", "unknown author subcommand")
