"""Showcase data: the reasoning of each agent, from the stored records (no model calls, no new labels).

- tier agent: how it tiers alerts (its rules with reasons, the patterns and investigated cases it learned from,
  its revisions round by round) and which rule places a given alert;
- investigation team: cases where it found chain accounts the layer-1 rule never alerted on, with the
  specialists' work, the evidence and the senior investigator's reasoning;
- rule-update agent: the data it saw, its reasoning, the rule change and its effect per version.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

from .rule_versions import history
from .settings import ROOT
from .store import Store

ROUNDS = ROOT / ".runtime/investigator-rounds"
# Alerts listed on the alert tracing page besides the investigated cases (highest priority per tier).
ALERT_QUOTA = {"high": 600, "mid": 250, "low": 150}
SHOWCASE = ROOT / ".runtime/showcase"


def matched_segment(policy: dict, features: dict) -> dict | None:
    """The first policy segment whose conditions all hold for the alert (the policy is an ordered list)."""
    for segment in policy.get("segments", []):
        values = [features.get(c["feature"]) for c in segment["conditions"]]
        if all(v is not None and (v >= c["value"] if c["op"] == "ge" else v <= c["value"])
               for v, c in zip(values, segment["conditions"])):
            return {"name": segment["name"], "tier": segment["tier"], "priority": segment["priority"], "reason": segment["reason"],
                    "conditions": [{**c, "actual": v} for c, v in zip(segment["conditions"], values)]}
    return None


def tier_of(policy: dict, features: dict) -> dict:
    match = matched_segment(policy, features)
    return match or {"name": "default", "tier": policy.get("default_tier", "low"), "reason": "no high-risk pattern matched"}


def tier_reasoning(store: Store, primary: dict) -> dict:
    if not primary:
        return {}
    policy = primary["policy"]
    rounds = [{"round": r["round"], "analysis": r["proposal"]["analysis"], "rationale": r["proposal"]["policy"]["rationale"],
               "segments": len(r["proposal"]["policy"]["segments"]), "segment_results": r.get("segments", []),
               "capacity": [{k: b.get(k) for k in ("alerts_per_day", "chain_recall", "chain_precision")} for b in r["development"]["budgets"]],
               "high": {k: r["development"]["high"].get(k) for k in ("alerts_per_day", "window_precision", "chain_precision")},
               "high_rules": sum(s["tier"] == "high" for s in r["proposal"]["policy"]["segments"]),
               "chosen": r["round"] == primary.get("best_round")}
              for r in primary.get("rounds", []) if r.get("proposal")]
    return {
        "label": primary.get("label"),
        "rationale": policy["rationale"],
        "segments": policy["segments"],
        "default_tier": policy.get("default_tier"),
        "uses_tier_model_score": policy.get("use_model_score"),
        "patterns": primary.get("patterns") or [],
        "investigation_evidence": {
            "case_library": primary.get("case_library") or [],
            "cited_in_segments": [s["name"] for s in policy["segments"] if "case " in s.get("reason", "")],
            "feedback_cases": primary.get("feedback_cases", 0),
        },
        "rounds": rounds,
        "reference_high": {k: (primary.get("development_reference") or {}).get("high", {}).get(k) for k in ("alerts_per_day", "window_precision")},
    }


# What changed between the investigation rounds (experiment log; the metrics come from the graded rounds).
ROUND_NOTES = {
    "val-r0": ("严格判定提示词", "Strict verdict prompt"),
    "val-r1": ("召回优先：宁可复核不漏放", "Recall first: review rather than miss"),
    "val-r2": ("加入开发集案件库与分级判决标准", "Development case library and per-tier decision standard"),
    "val-r3-adj": ("加入证据清单与告警联动", "Evidence checklist and alert linkage"),
}


def investigation_learning(evaluation: dict) -> dict:
    from . import investigator_training

    rounds = [{"round": name, "change": zh, "change_en": en, **{k: (evaluation.get(name) or {}).get(k) for k in (
                  "cases", "exclusion_rate", "false_exclusion_rate", "chain_accounts_found", "chain_accounts_in_files",
                  "new_chain_accounts_found", "account_verdict_precision")}}
              for name, (zh, en) in ROUND_NOTES.items() if evaluation.get(name)]
    config = investigator_training.load() or {}
    tools: dict[str, list[int]] = {}
    for per_tool in (config.get("tool_effectiveness") or {}).values():
        for tool, stats in per_tool.items():
            cell = tools.setdefault(tool, [0, 0])
            cell[0] += stats["uses_in_laundering_cases"]
            cell[1] += stats["found_laundering_transfer"]
    return {"rounds": rounds, "trained_on": config.get("trained_on"),
            "tools": sorted(({"tool": t, "uses": u, "found": f, "rate": f / u if u else None} for t, (u, f) in tools.items()),
                            key=lambda x: -(x["rate"] or 0))}


def investigation_highlights(store: Store, primary: dict, rounds: tuple[str, ...] = ("val-r3-adj", "val-r3", "val-r2")) -> list[dict]:
    """Validation cases where the team named chain accounts the layer-1 rule never alerted on."""
    policy = (primary or {}).get("policy") or {}
    out, seen = [], set()
    for name in rounds:
        path = ROUNDS / f"{name}.json"
        if not path.exists():
            continue
        for case in json.loads(path.read_text()).get("cases", []):
            if not case.get("new_chain_accounts_found") or case["run_id"] in seen:
                continue
            seen.add(case["run_id"])
            state = store.get(case["run_id"])
            team = state.get("team") or {}
            members = []
            for assignment, child in zip(team.get("assignments", []), team.get("children", [])):
                member = store.get(child)
                members.append({
                    "role": assignment["role"], "question": assignment["question"], "accounts": assignment.get("accounts"),
                    "steps": [{"tool": o["tool"], "arguments": o["arguments"], "found": (o.get("digest") or [])[:4]}
                              for o in member.get("observations", [])],
                    "conclusion": (member.get("conclusion") or {}).get("conclusion"),
                    "reason": (member.get("conclusion") or {}).get("brief_reason"),
                })
            conclusion = state.get("conclusion") or {}
            adjudication = state.get("adjudication") or {}
            new = set(case["new_chain_accounts_found"])
            out.append({
                "round": name, "run_id": case["run_id"], "account": case["account"], "day": case["day"],
                "tier": tier_of(policy, case["features"]) if policy else {"tier": case.get("tier")},
                "triggered_rules": len(case["triggered_rule_ids"]),
                "team_plan": team.get("rationale"), "members": members,
                "chain": conclusion.get("chain", []),
                "findings": conclusion.get("supported_findings", []), "counterevidence": conclusion.get("counterevidence", []),
                "team_conclusion": conclusion.get("conclusion"), "team_reason": conclusion.get("brief_reason"),
                "suspect_transactions": conclusion.get("suspect_transaction_ids", []),
                "new_accounts": [{"account_ref": v["account_ref"], "why_found": v["reason"], "evidence_refs": v.get("evidence_refs", [])}
                                 for v in case.get("account_verdicts", []) if v["account_ref"] in new],
                "chain_accounts_found": case["chain_accounts_found"],
                "senior_investigator": {k: adjudication.get(k) for k in ("prior_level", "adjustment", "risk_level", "decision", "rationale",
                                                                          "evidence_flags", "key_evidence", "account_risks", "linked_alerts")},
                "model_calls": case.get("model_calls"), "sql_requests": case.get("sql_requests"),
            })
    # Strongest first: higher tier, reported, more red flags cited.
    order = {"high": 0, "mid": 1, "low": 2}
    out.sort(key=lambda h: (order.get(h["tier"].get("tier"), 3), h["senior_investigator"].get("decision") != "report",
                            -len(h["senior_investigator"].get("evidence_flags") or [])))
    return out


def rule_update_reasoning(store: Store, versions: list[dict]) -> list[dict]:
    """Every agent-made rule version: what the agent saw, what it concluded, what changed and the effect."""
    from .learning_cycle import feature_contrast

    cycles = {(body.get("publication") or {}).get("active_version"): body
              for _, body in store.artifacts("learning_cycle") if body.get("kind") == "rule_update_from_investigations"}
    metrics = json.loads((SHOWCASE / "versions.json").read_text()) if (SHOWCASE / "versions.json").exists() else {}
    out = []
    for version in versions:
        if version.get("trigger") != "rule_update_agent" or version.get("status") != "published":
            continue
        cycle = cycles.get(version["id"], {})
        inputs = cycle.get("plan_inputs") or {}
        cases = [c for c in cycle.get("cases", []) if c.get("usable")]
        contrast = inputs.get("feature_contrast") or (feature_contrast(cases) if cases else {})
        evaluation = cycle.get("update_evaluation") or {}
        out.append({
            "label": version["label"], "parent": version.get("parent_label"),
            "inputs": {"investigated_cases": inputs.get("investigated_cases", len(cases)), "rounds": cycle.get("rounds"),
                       "investigations_by_rule": inputs.get("investigations_by_rule"),
                       "feature_contrast": contrast},
            "reasoning": (version.get("plan") or {}).get("rationale"),
            "plan": {k: v for k, v in (version.get("plan") or {}).items() if k != "rationale"},
            "diff": version.get("diff"),
            "diff_counts": {k: len(v) for k, v in (version.get("diff") or {}).items() if isinstance(v, list)},
            "decision": evaluation.get("decision"), "reason": evaluation.get("reason"),
            "effect": {"before": metrics.get(version.get("parent_label")), "after": metrics.get(version["label"])},
        })
    return out


def tier_versions(store: Store, versions: list[dict], primary_id: str | None) -> list[dict]:
    """Every grading policy with its validation result on the rule version it was learned on (from showcase-metrics)."""
    path = SHOWCASE / "tier_versions.json"
    metrics = json.loads(path.read_text()) if path.exists() else {}
    label_of = {v["id"]: v["label"] for v in versions}
    out = []
    for policy_id, body in store.artifacts("tier_policy_version"):
        validation = (metrics.get(body.get("label")) or {}).get("validation") or {}
        out.append({
            "label": body.get("label"), "primary": policy_id == primary_id, "rule_version": label_of.get(body.get("rule_version")),
            "segments": len((body.get("policy") or {}).get("segments", [])), "best_round": body.get("best_round"), "source": body.get("source"),
            "tiers": {t: {k: (validation.get(t) or {}).get(k) for k in ("alerts_per_day", "chain_precision")} for t in ("high", "mid", "low")},
            "capacity": [{k: b.get(k) for k in ("alerts_per_day", "chain_recall", "chain_precision")} for b in validation.get("budgets", [])],
        })
    return out


def view(store: Store, versions: list[dict]) -> dict:
    from .tier_agent import ALIAS

    pointer = store.program_pointer(ALIAS)
    primary = store.read_artifact(pointer, "tier_policy_version") if pointer else {}
    metrics = json.loads((SHOWCASE / "versions.json").read_text()) if (SHOWCASE / "versions.json").exists() else {}
    return {
        "rule_versions": [{"label": label, **{k: m.get(k) for k in ("chain_recall", "full_chain_account_recall", "chain_precision",
                                                                    "window_precision", "flagged_per_day", "flagged_accounts")}}
                          for label, m in sorted(metrics.items(), key=lambda item: int(item[0].lstrip("v")))],
        "tier_agent": tier_reasoning(store, primary),
        "tier_versions": tier_versions(store, versions, pointer),
        "investigation_highlights": investigation_highlights(store, primary),
        "rule_updates": rule_update_reasoning(store, versions),
    }


def refresh_metrics(cycle, log=print) -> dict:
    """Recompute the chain-based validation metrics the dashboard reads (evaluation side; labels used for scoring only).

    - versions.json: layer-1 metrics of every published rule version (cached per version),
    - metrics.json: the active version's layer 1, the primary risk grading policy and the tier model reference,
    - validation_volume.json: transactions and laundering transactions in the validation period.
    """
    from .chains import IN_SCOPE
    from .rule_program import load_program, program_of
    from .tier_agent import ALIAS, Evaluator, TierPolicy, compile_policy
    from .tiering import score_sql, tier_sql

    SHOWCASE.mkdir(parents=True, exist_ok=True)
    store, learner = cycle.store, cycle.learner
    path = SHOWCASE / "versions.json"
    versions = json.loads(path.read_text()) if path.exists() else {}
    for entry in history(store):
        if entry["status"] == "published" and entry.get("objective") == "chain_recall" and entry["label"] not in versions:
            versions[entry["label"]] = {"id": entry["id"], **learner.chain_metrics(program_of(load_program(store, entry["id"])), "validation")}
            path.write_text(json.dumps(versions, indent=2, default=str))
            log(json.dumps({"version_metrics": entry["label"]}))
    version = store.program_pointer("learned-current")
    program = program_of(load_program(store, version))
    metrics = {"rule_version": version, "created": time.time(), "layer1_validation": learner.chain_metrics(program, "validation")}
    _, stored = cycle.tier_model(version)
    model = stored["model"]
    evaluator = Evaluator(learner, program)
    metrics["tier_model_validation"] = evaluator.evaluate("validation", tier_sql(model), score_sql(model))
    pointer, primary_tiering = store.program_pointer(ALIAS), None
    if pointer:
        primary = store.read_artifact(pointer, "tier_policy_version")
        if primary["rule_version"] == version:
            tier, priority = compile_policy(TierPolicy.model_validate(primary["policy"]), score_sql(model))
            primary_tiering = {"tier": tier, "priority": priority}
            metrics[f"tier_agent_{primary['label']}_validation"] = evaluator.evaluate("validation", tier, priority)
            metrics[f"tier_agent_{primary['label']}_policy"] = primary["policy"]
            metrics["primary_tier_policy"] = primary["label"]
    (SHOWCASE / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    # Every grading policy keeps its own record (evaluated on the rule version it was learned on): nothing is overwritten.
    tiers_path = SHOWCASE / "tier_versions.json"
    tiers = json.loads(tiers_path.read_text()) if tiers_path.exists() else {}
    for policy_id, body in store.artifacts("tier_policy_version"):
        if body.get("label") in tiers or not body.get("policy"):
            continue
        own = program_of(load_program(store, body["rule_version"]))
        _, own_model = cycle.tier_model(body["rule_version"])
        own_tier, own_priority = compile_policy(TierPolicy.model_validate(body["policy"]), score_sql(own_model["model"]))
        tiers[body["label"]] = {"policy_id": policy_id, "rule_version": body["rule_version"],
                                "validation": Evaluator(learner, own).evaluate("validation", own_tier, own_priority)}
        tiers_path.write_text(json.dumps(tiers, indent=2, default=str))
        log(json.dumps({"tier_version_metrics": body["label"]}))
    # The alert list: the highest-priority validation alerts of each tier under the primary grading policy.
    if primary_tiering:
        from .tier_agent import ranked

        rows = ranked(learner, program, primary_tiering, "validation", ALERT_QUOTA)
        (SHOWCASE / "alerts.json").write_text(json.dumps({"rule_version": version, "policy": primary["label"], "alerts": rows}, default=str))
        log(json.dumps({"ranked_alerts": len(rows)}))
    if not (SHOWCASE / "validation_volume.json").exists():
        from .evaluation import split_plan

        plan = split_plan()["validation"]
        scope = f"payment_format IN {IN_SCOPE} AND (from_bank <> to_bank OR from_account <> to_account)"
        row = learner.zetaris.run(
            f"SELECT COUNT(*), SUM(CASE WHEN is_laundering = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN {scope} THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN is_laundering = 1 AND {scope} THEN 1 ELSE 0 END) FROM IBM_AML.hi_large_transactions "
            f"WHERE transaction_time >= TIMESTAMP '{plan['start'].replace('T', ' ')}' AND transaction_time < TIMESTAMP '{plan['end'].replace('T', ' ')}'", 2)[0]
        days = (date.fromisoformat(plan["end"][:10]) - date.fromisoformat(plan["start"][:10])).days
        (SHOWCASE / "validation_volume.json").write_text(json.dumps({
            "period": f"{plan['start'][:10]} – {(date.fromisoformat(plan['end'][:10]) - timedelta(days=1)).isoformat()}", "days": days,
            "transactions": int(row[0]), "laundering_transactions": int(row[1]),
            "in_scope_transactions": int(row[2]), "in_scope_laundering_transactions": int(row[3])}, indent=2))
    return {"versions": sorted(versions), "primary_tier_policy": metrics.get("primary_tier_policy"), "rule_version": version}
