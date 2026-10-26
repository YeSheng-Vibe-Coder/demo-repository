"""Showcase data: the reasoning of each agent, from the stored records (no model calls, no new labels).

- tier agent: how it tiers alerts (its rules with reasons, the patterns and investigated cases it learned from,
  its revisions round by round) and which rule places a given alert;
- investigation team: what changed between the investigation rounds and how often each tool found laundering;
- rule-update agent: the data it saw, its reasoning, the rule change and its effect per version.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

from .rule_versions import history
from .settings import SHOWCASE
from .store import RULES_POINTER, Store


def version_metrics() -> dict:
    """Chain-based validation metrics of every published rule version, by label (from showcase-metrics)."""
    path = SHOWCASE / "versions.json"
    return json.loads(path.read_text()) if path.exists() else {}


def matched_segment(policy: dict, features: dict) -> dict | None:
    """The segment that grades an alert: the highest-priority one whose conditions all hold (as compile_policy in SQL)."""
    from .rule_learning import value

    for segment in sorted(policy.get("segments", []), key=lambda s: -s["priority"]):
        values = [features.get(c["feature"]) for c in segment["conditions"]]
        if all(value(features, c["feature"]) >= c["value"] if c["op"] == "ge" else value(features, c["feature"]) <= c["value"]
               for c in segment["conditions"]):
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
# Each round ran with the investigator configuration current at the time (investigator_config artifacts inv-v3..v5).
ROUND_NOTES = {
    "val-r0": ("严格判定提示词，无参考案件", "Strict verdict prompt, no reference cases"),
    "val-r1": ("召回优先：宁可复核不漏放；开发集案件库 46 例", "Recall first: review rather than miss; 46 development reference cases"),
    "val-r2": ("案件库扩到 68 例，加入分级判决标准", "68 reference cases and a per-tier verdict standard"),
    "val-r3-adj": ("案件库 88 例，取消分级判决标准；改由高级调查员按证据清单研判，并联动其他告警",
                   "88 reference cases, no per-tier verdict standard; the senior investigator decides from an evidence checklist and linked alerts"),
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


def rule_update_reasoning(store: Store, versions: list[dict]) -> list[dict]:
    """Every agent-made rule version: what the agent saw, what it concluded, what changed and the effect."""
    from .learning_cycle import feature_contrast

    cycles = {(body.get("publication") or {}).get("active_version"): body
              for _, body in store.artifacts("learning_cycle") if body.get("kind") == "rule_update_from_investigations"}
    metrics = version_metrics()
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


def view(store: Store, versions: list[dict]) -> dict:
    _, primary = store.primary_policy()
    metrics = version_metrics()
    return {
        "rule_versions": [{"label": label, **{k: m.get(k) for k in ("chain_recall", "full_chain_account_recall", "chain_precision",
                                                                    "window_precision", "flagged_per_day", "flagged_accounts")}}
                          for label, m in sorted(metrics.items(), key=lambda item: int(item[0].lstrip("v")))],
        "tier_agent": tier_reasoning(store, primary or {}),
        "rule_updates": rule_update_reasoning(store, versions),
    }


def refresh_metrics(cycle, log=print) -> dict:
    """Recompute the chain-based validation metrics the dashboard reads (evaluation side; labels used for scoring only).

    - versions.json: layer-1 metrics of every published rule version (cached per version),
    - metrics.json: the active version's layer 1, the primary risk grading policy and the tier model reference,
    - validation_volume.json: transactions and laundering transactions in the validation period.
    """
    from .rule_program import load_program, program_of
    from .tier_agent import Evaluator, TierPolicy, compile_policy
    from .tiering import score_sql, tier_sql
    from .zetaris import IN_SCOPE

    SHOWCASE.mkdir(parents=True, exist_ok=True)
    store, learner = cycle.store, cycle.learner
    path = SHOWCASE / "versions.json"
    versions = json.loads(path.read_text()) if path.exists() else {}
    for entry in history(store):
        if entry["status"] == "published" and entry.get("objective") == "chain_recall" and entry["label"] not in versions:
            versions[entry["label"]] = {"id": entry["id"], **learner.chain_metrics(program_of(load_program(store, entry["id"])), "validation")}
            path.write_text(json.dumps(versions, indent=2, default=str))
            log(json.dumps({"version_metrics": entry["label"]}))
    version = store.program_pointer(RULES_POINTER)
    program = program_of(load_program(store, version))
    metrics = {"rule_version": version, "created": time.time(), "layer1_validation": learner.chain_metrics(program, "validation")}
    _, stored = cycle.tier_model(version)
    model = stored["model"]
    evaluator = Evaluator(learner, program)
    metrics["tier_model_validation"] = evaluator.evaluate("validation", tier_sql(model), score_sql(model))
    _, primary = store.primary_policy()
    if primary and primary["rule_version"] == version:
        tier, priority = compile_policy(TierPolicy.model_validate(primary["policy"]), score_sql(model))
        metrics[f"tier_agent_{primary['label']}_validation"] = evaluator.evaluate("validation", tier, priority)
        metrics[f"tier_agent_{primary['label']}_policy"] = primary["policy"]
        metrics["primary_tier_policy"] = primary["label"]
    (SHOWCASE / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    if not (SHOWCASE / "validation_volume.json").exists():
        from .evaluation import split_plan

        plan = split_plan()["validation"]
        scope = IN_SCOPE.format(p="")
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
