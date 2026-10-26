"""Operational CLI: serve, preflight, feature store, learning cycle and case inspection."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from datetime import date, timedelta
from pathlib import Path

import httpx

from .contracts import CaseRequest
from .settings import ROOT, Settings

API = "http://127.0.0.1:8010"


def api_token() -> str:
    folder = ROOT / ".runtime"
    folder.mkdir(exist_ok=True, mode=0o700)
    path = folder / "api-token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text().strip()


def model_check(settings: Settings) -> dict:
    """One real request per role with the schema that role uses, on a stored finished case.

    Catches what a /models probe cannot: grammar features (patterns, length bounds, oneOf) the
    serving engine rejects, reasoning text leaking out of content, and wrong model names or keys.
    """
    import time

    from .contracts import DECISIONS
    from .providers import Nvidia
    from .store import Store
    from .workflow import PLANNER_PROMPT, Workflow, planner_schema

    store = Store(settings.database)
    with store.connect() as db:
        rows = db.execute("SELECT state FROM runs ORDER BY rowid DESC LIMIT 400").fetchall()
    states = [json.loads(r["state"]) for r in rows]
    child = next(s for s in states if (s.get("request") or {}).get("assignment") and s["status"] == "completed" and s["observations"])
    parent = next(s for s in states if s.get("conclusion") and s.get("team") and s["status"] == "completed")
    checks = {}
    for name in ("investigator", "senior_investigator", "rule_learner", "tier_agent"):
        role = settings.role(name)
        provider = Nvidia(role)
        started = time.time()
        try:
            if name == "investigator":
                flow = Workflow(store, provider, None)
                state = {**child, "harness": {}, "plan": child["plan"] or ["q"]}
                raw, usage = provider.generate(PLANNER_PROMPT, flow.fit_packet(flow.packet(state)), planner_schema(state))
                if "_invalid_response" in raw:
                    raise ValueError(raw["_invalid_response"])
                outcome = f"valid {DECISIONS.validate_python(raw).decision} decision"
            else:
                from .adjudication import Adjudication, brief, schema

                accounts = list(parent["accounts"])[:4]
                raw, usage = provider.generate("Return one JSON instance adjudicating the case.",
                                               {"brief": brief(parent, {}, None), "accounts": accounts}, schema(accounts))
                if "_invalid_response" in raw:
                    raise ValueError(raw["_invalid_response"])
                outcome = f"valid adjudication ({Adjudication.model_validate(raw).adjustment})"
            checks[name] = {"ok": True, "model": role.model, "endpoint": "nvidia" if "integrate.api.nvidia.com" in role.model_base_url else "self_hosted",
                            "seconds": round(time.time() - started, 1), "result": outcome, "completion_tokens": usage.get("completion_tokens")}
        except Exception as error:
            checks[name] = {"ok": False, "model": role.model, "error": f"{type(error).__name__}: {str(error)[:300]}",
                            "seconds": round(time.time() - started, 1)}
    return checks


def preflight(settings: Settings) -> dict:
    """Configuration plus live reachability; credentials are never printed."""
    checks = {"model_key_configured": bool(settings.nvidia_key), "zetaris_key_configured": bool(settings.zetaris_key)}
    probes = {
        "nvidia_models": lambda: httpx.get(
            settings.model_base_url + "/models", headers={"Authorization": "Bearer " + settings.nvidia_key}, timeout=15
        ),
        "zetaris_datasources": lambda: httpx.get(
            settings.zetaris_url + "/datasource/datasources",
            headers={"Authorization": "Bearer " + settings.zetaris_key, "X-Org-ID": settings.org_id},
            timeout=15,
        ),
        "local_api": lambda: httpx.get(API + "/healthz", timeout=3),
    }
    for name, probe in probes.items():
        try:
            checks[name] = probe().status_code
        except httpx.HTTPError as error:
            checks[name] = type(error).__name__
    roles = {}
    for name in ("investigator", "senior_investigator", "rule_learner", "tier_agent"):
        role = settings.role(name)
        try:
            code = httpx.get(role.model_base_url + "/models", headers={"Authorization": "Bearer " + role.nvidia_key}, timeout=15).status_code
        except httpx.HTTPError as error:
            code = type(error).__name__
        roles[name] = {"model": role.model, "endpoint": "nvidia" if "integrate.api.nvidia.com" in role.model_base_url else "self_hosted",
                       "reachable": code}
    return {"roles": roles, **checks, "credentials_printed": False}


def exported_schemas() -> dict[str, dict]:
    """Current public contracts, as committed under schemas/ (a test keeps them in sync)."""
    from .contracts import DECISIONS, CaseRequest, TeamPlan
    from .rule_learning import UpdatePlan
    from .rule_program import RuleProgram

    return {
        "case-request.v2": CaseRequest.model_json_schema(),
        "planner-decision.v2": DECISIONS.json_schema(),
        "team-plan.v1": TeamPlan.model_json_schema(),
        "rule-program.v2": RuleProgram.model_json_schema(),
        "rule-learning-plan.v1": UpdatePlan.model_json_schema(),
    }


def run_team(run_id: str) -> int:
    """Every case runs as a team: coordinator, parallel specialists, World Model aggregator."""
    completed = subprocess.run(
        ["node", "--experimental-transform-types", str(ROOT / "services/orchestrator/team.ts"), run_id],
        cwd=ROOT,
        check=False,
    )
    return completed.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("status")
    sub.add_parser("materialize-features")
    sub.add_parser("export-schemas")
    sub.add_parser("rule-changelog")
    sub.add_parser("model-check")
    dashboard = sub.add_parser("dashboard", help="Export the dashboard data (.runtime/dashboard/data.json)")
    dashboard.add_argument("--snapshot", action="store_true", help="Also update the committed snapshot apps/dashboard/snapshot/data.json")
    viewer = sub.add_parser("serve-dashboard", help="Serve the dashboard from exported or snapshot data; needs no credentials")
    viewer.add_argument("--port", type=int, default=8020)
    sub.add_parser("showcase-metrics", help="Recompute the chain-based validation metrics the dashboard shows")
    index = sub.add_parser("alert-index", help="Export every validation alert with its grading to a local SQLite index")
    index.add_argument("--snapshot", action="store_true", help="Also write the committed gzip copy")
    learning = sub.add_parser("learning-cycle")
    learning.add_argument("--cases", type=int, default=30, help="Investigated alerts; the scan takes the top alert of each development day")
    learning.add_argument("--parallel", type=int, default=3, help="Team cases investigated at once")
    learning.add_argument("--fresh", action="store_true", help="Re-learn the initial rule even if the alias already holds a version")
    learning.add_argument("--resume", default=None, help="Cycle ID whose investigations are already graded")
    learning.add_argument("--rerun", default=None, help="Cycle ID whose alerts are re-investigated and compared")
    learning.add_argument("--rules-only", action="store_true", help="Learn and gate the layer-1 rule without investigations")
    learning.add_argument("--investigator-round", default=None, help="Name of a tier x label team-investigation round")
    learning.add_argument("--split", default="development", choices=["development", "validation"])
    learning.add_argument("--train-investigators", default=None, help="Build an investigator configuration from a graded development round (comma-separated rounds for the reference library)")
    learning.add_argument("--threshold-round", default=None, help="Development round whose suspicion levels set the verdict standard")
    learning.add_argument("--per-cell", type=int, default=8, help="Alerts per (tier, window label) in an investigator round")
    learning.add_argument("--tier-agent", action="store_true", help="Learn a tiering and priority policy (Ultra) and gate it on validation")
    learning.add_argument("--tier-publish", default=None, help="Make a tier-agent policy (e.g. T3) the primary tiering")
    learning.add_argument("--tier-adopt", default=None, help="Carry a grading policy (e.g. T3) over to the active rule version and publish it")
    learning.add_argument("--tier-patterns", action="store_true", help="Give the tier agent laundering patterns mined from development positives and the case library")
    learning.add_argument("--tier-feedback", default=None, help="Comma-separated development rounds whose tier disagreements revise the policy")
    learning.add_argument("--retry-round", default=None, help="Re-investigate a round's unfinished cases and re-grade it")
    learning.add_argument("--adjudicate-round", default=None, help="Adjudicate a stored round's finished cases; graded as <name>-adj")
    learning.add_argument("--model", default=None, help="Model for --adjudicate-round (default: config adjudication.model)")
    learning.add_argument("--regrade-round", default=None, help="Re-score a stored round under the current evaluation")
    learning.add_argument("--update-from", default=None, help="Comma-separated graded development rounds driving a rule update")
    investigate = sub.add_parser("investigate")
    investigate.add_argument("case_file", type=Path)
    run = sub.add_parser("run")
    run.add_argument("run_id")
    for name in ("show", "trace"):
        sub.add_parser(name).add_argument("run_id")
    args = parser.parse_args()
    token = api_token()
    headers = {"Authorization": "Bearer " + token}

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        os.environ["AML_INTERNAL_TOKEN"] = token
        uvicorn.run(create_app(), host="127.0.0.1", port=8010, access_log=False)
    elif args.command == "dashboard":
        from .dashboard import export

        export(snapshot=args.snapshot)
    elif args.command == "serve-dashboard":
        from .dashboard_server import serve

        serve(args.port)
    elif args.command == "alert-index":
        from . import alert_index
        from .learning_cycle import LearningCycle

        result = alert_index.build(LearningCycle(Settings.load(), token))
        if args.snapshot:
            result["snapshot_bytes"] = alert_index.snapshot()
        print(json.dumps(result, indent=2))
    elif args.command == "showcase-metrics":
        from .learning_cycle import LearningCycle
        from .showcase import refresh_metrics

        print(json.dumps(refresh_metrics(LearningCycle(Settings.load(), token)), indent=2))
    elif args.command == "model-check":
        print(json.dumps(model_check(Settings.load()), indent=2))
    elif args.command == "rule-changelog":
        from .rule_changelog import render
        from .store import Store

        store = Store(Settings.load().database)
        path = ROOT / "docs/rules/RULE_VERSIONS.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(store, store.program_pointer("learned-current")))
        print(json.dumps({"written": str(path.relative_to(ROOT))}))
    elif args.command == "export-schemas":
        folder = ROOT / "schemas"
        for old in folder.glob("*.schema.json"):
            old.unlink()
        for name, schema in exported_schemas().items():
            (folder / f"{name}.schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        print(json.dumps(sorted(exported_schemas())))
    elif args.command == "status":
        print(json.dumps(preflight(Settings.load()), indent=2))
    elif args.command == "materialize-features":
        from .evaluation import split_plan
        from .feature_store import FeatureStore

        store, plan = FeatureStore(), split_plan()
        # The test partition is never materialised: it stays closed.
        for split in ("development", "validation", "monitor"):
            day, end = date.fromisoformat(plan[split]["start"][:10]), date.fromisoformat(plan[split]["end"][:10])
            while day < end:
                print(json.dumps({"split": split, **store.write_day(day.isoformat())}), flush=True)
                day += timedelta(days=1)
        store.register()
    elif args.command == "learning-cycle":
        from .learning_cycle import LearningCycle

        cycle = LearningCycle(Settings.load(), token)
        if args.train_investigators:
            from .evaluation import TransactionLabelReader
            from .investigator_training import build

            def development(name: str) -> dict:
                graded = json.loads((ROOT / ".runtime/investigator-rounds" / f"{name}.json").read_text())
                if graded["split"] != "development":
                    raise SystemExit("Investigators are trained on development rounds only")
                return graded

            names = args.train_investigators.split(",")
            library = [case for name in names for case in development(name)["cases"]]
            standard = development(args.threshold_round)["cases"] if args.threshold_round else None
            config = build(cycle.store, TransactionLabelReader(cycle.learner.zetaris), library, "+".join(names), standard)
            print(json.dumps({k: config[k] for k in ("version", "id", "cases", "thresholds")}, indent=2, default=str))
            return
        if args.regrade_round:
            result = cycle.regrade_round(args.regrade_round)
            print(json.dumps(result["summary"]["evaluation"], indent=2))
            return
        if args.update_from:
            result = cycle.update_from_rounds(args.update_from.split(","))
            print(json.dumps({k: result.get(k) for k in ("cycle_id", "decision", "plan_source")}, indent=2))
            return
        if args.tier_adopt:
            print(json.dumps(cycle.tier_adopt(args.tier_adopt), indent=2))
            return
        if args.tier_publish:
            print(json.dumps(cycle.tier_publish(args.tier_publish), indent=2))
            return
        if args.tier_agent:
            result = cycle.tier_agent(args.tier_feedback.split(",") if args.tier_feedback else None, args.tier_patterns)
            print(json.dumps({k: result.get(k) for k in ("label", "status", "decision", "reason", "best_round")}, indent=2))
            return
        if args.adjudicate_round:
            result = cycle.adjudicate_round(args.adjudicate_round, f"{args.adjudicate_round}-adj", min(args.parallel, 2), args.model)
            print(json.dumps(result.get("summary"), indent=2))
            return
        if args.retry_round:
            result = cycle.retry_round(args.retry_round, args.parallel)
            print(json.dumps(result.get("summary"), indent=2))
            return
        if args.investigator_round:
            result = cycle.investigator_round(args.investigator_round, args.split, args.per_cell, args.parallel)
            print(json.dumps(result.get("summary"), indent=2))
            return
        if args.rules_only:
            result = cycle.rules()
        elif args.rerun:
            result = cycle.rerun(args.rerun, args.parallel)
        elif args.resume:
            result = cycle.resume(args.resume, args.parallel)
        else:
            result = cycle.run(args.cases, args.parallel, args.fresh)
        print(json.dumps({k: result.get(k) for k in ("cycle_id", "status", "decision", "result_path")}, indent=2))
    elif args.command == "investigate":
        case = CaseRequest.model_validate_json(args.case_file.read_text())
        response = httpx.post(API + "/v1/cases", json=case.model_dump(mode="json"), headers=headers, timeout=15)
        response.raise_for_status()
        run_id = response.json()["id"]
        print(json.dumps({"run_id": run_id}), flush=True)
        raise SystemExit(run_team(run_id))
    elif args.command == "run":
        raise SystemExit(run_team(args.run_id))
    else:
        route = "/v1/runs/" + args.run_id + ("/trace" if args.command == "trace" else "")
        response = httpx.get(API + route, headers=headers, timeout=15)
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
