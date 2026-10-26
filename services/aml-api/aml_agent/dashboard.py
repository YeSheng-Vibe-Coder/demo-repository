"""Dashboard data: one JSON file with everything the dashboard shows.

Rule versions and features (rules page), the validation alert tracing with each case's replay
(alert tracing page), and the reasoning and effect of the three agents (rule evolution page).
Built from stored run state and records; investigation views are label-free, and labels appear
only in evaluation fields, shown as after-the-fact checking.
"""

from __future__ import annotations

import json
import shutil
import time

from .feature_registry import DESCRIPTIONS, FEATURES, POLICY, UNITS
from .rule_learning import RuleLearner
from .rule_program import policy_registry
from .rule_versions import history
from .settings import ROOT, Settings
from .store import Store

OUTPUT = ROOT / ".runtime/dashboard"
# Committed copy for running the dashboard without any data access (see README).
SNAPSHOT = ROOT / "apps/dashboard/snapshot/data.json"
WINDOW = "告警日 D 的窗口：截止 D+24h；24h 指 [D, D+24h)，48h 指 [D−24h, D+24h)。仅 ACH/Wire、排除自转账。"


# How each registered feature is computed by the Zetaris feature-store templates
# (feature_store.aggregate_sql / transit_sql), for display next to the rules.
FEATURE_DOCS = {
    "fan_in_24h": ("24h 汇入对手方数", "按币种分组，统计最近 24h 向该账户付款的不同账户数，取各币种最大值。",
                   "MAX over currency( COUNT(DISTINCT from_bank:from_account) WHERE side=incoming AND t ≥ D )"),
    "fan_in_48h": ("48h 汇入对手方数", "按币种分组，统计最近 48h 向该账户付款的不同账户数，取各币种最大值。",
                   "MAX over currency( COUNT(DISTINCT from_bank:from_account) WHERE side=incoming )"),
    "fan_out_24h": ("24h 汇出对手方数", "按币种分组，统计最近 24h 该账户付款给的不同账户数，取各币种最大值。",
                    "MAX over currency( COUNT(DISTINCT to_bank:to_account) WHERE side=outgoing AND t ≥ D )"),
    "fan_out_48h": ("48h 汇出对手方数", "按币种分组，统计最近 48h 该账户付款给的不同账户数，取各币种最大值。",
                    "MAX over currency( COUNT(DISTINCT to_bank:to_account) WHERE side=outgoing )"),
    "in_count_24h": ("24h 汇入笔数", "最近 24h 该账户收到的付款笔数（所有币种合计）。",
                     "SUM( COUNT(*) WHERE side=incoming AND t ≥ D )"),
    "out_count_24h": ("24h 汇出笔数", "最近 24h 该账户发出的付款笔数（所有币种合计）。",
                      "SUM( COUNT(*) WHERE side=outgoing AND t ≥ D )"),
    "in_banks_24h": ("24h 汇入银行数", "按币种分组，最近 24h 向该账户付款的不同银行数，取最大值。",
                     "MAX over currency( COUNT(DISTINCT from_bank) WHERE side=incoming AND t ≥ D )"),
    "out_banks_24h": ("24h 汇出银行数", "按币种分组，最近 24h 该账户付款去往的不同银行数，取最大值。",
                      "MAX over currency( COUNT(DISTINCT to_bank) WHERE side=outgoing AND t ≥ D )"),
    "pass_through_ratio_24h": ("24h 过账比", "按币种计算最近 24h 汇出金额 / 汇入金额，上限 10，取各币种最大值；接近 1 表示收多少转多少。",
                               "MAX over currency( LEAST(SUM(amount_paid out) / SUM(amount_received in), 10) )"),
    "transit_gap_hours": ("收后转出最短间隔（小时）", "收到一笔款后到下一笔转出的最短时间：转出在最近 24h，转入在 48h 内，币种不限；没有这样的配对记为 49h。",
                          "MIN( (b.time − a.time)/3600 ) FROM in a JOIN out b ON b.payer = a.receiver AND a.time < b.time ≤ a.time + 48h"),
    "transit_pairs_24h": ("24h 收转配对数", "转入后 24h 内又转出的（转入, 转出）配对个数，转出在最近 24h。",
                          "SUM( 1 WHERE gap ≤ 24h ) over receive→send pairs"),
    "round_trip_48h": ("48h 回流笔数", "该账户转出给 48h 内曾经付款给它的账户的笔数（资金回流）。",
                       "COUNT(DISTINCT b.id WHERE b.receiver = a.payer)"),
    "currency_count_24h": ("24h 币种数", "最近 24h 收付涉及的不同币种个数。",
                           "COUNT(DISTINCT currency WHERE n_in + n_out > 0)"),
    "onward_currency_switch_48h": ("48h 换币转出笔数", "转出币种与之前 48h 内某笔转入币种不同的转出笔数（换汇后转出）。",
                                   "COUNT(DISTINCT b.id WHERE b.payment_currency ≠ a.receiving_currency)"),
    "activity_change_24h": ("24h 活跃度变化", "最近 24h 收付笔数 ÷（之前 24h 收付笔数 + 1），衡量相对自身历史的突增。",
                            "SUM(n_in + n_out) / (SUM(n_prior) + 1)"),
    "new_counterparty_share_24h": ("24h 新对手方占比", "最近 24h 的收付对手方中，过去 7 天从未与该账户交易过的比例。",
                                   "COUNT(DISTINCT cp WHERE (account, cp) ∉ prior 7d) / COUNT(DISTINCT cp)"),
    "new_counterparties_24h": ("24h 新对手方数", "最近 24h 的收付对手方中，过去 7 天从未与该账户交易过的个数。",
                               "COUNT(DISTINCT cp WHERE (account, cp) ∉ prior 7d)"),
    "history_active_days_7d": ("近 7 天活跃天数", "告警日之前 7 天里有收付交易的天数；0 表示此前沉寂。",
                               "COUNT(DISTINCT DATE(t)) over [D−7d, D)"),
    "amount_vs_history_24h": ("24h 金额相对历史倍数", "按币种：最近 24h 最大一笔 ÷（过去 7 天同币种最大一笔 + 1），取各币种最大值，上限 1000。",
                              "MAX over currency( MAX(amount 24h) / (MAX(amount prior 7d) + 1) )"),
}
SOURCE_TABLE = {name: "IBM_AML.agent_day_transit" if name in ("transit_gap_hours", "transit_pairs_24h", "round_trip_48h",
                                                                "onward_currency_switch_48h")
                else "IBM_AML.agent_day_history" if name in ("new_counterparty_share_24h", "new_counterparties_24h",
                                                             "history_active_days_7d", "amount_vs_history_24h")
                else "IBM_AML.agent_day_aggregates"
                for name in FEATURES}


def symbol(operator: str) -> str:
    return "≥" if operator == "ge" else "≤"


def condition_text(rule: dict) -> str:
    return f"{rule['feature_id']} {symbol(rule['operator'])} {rule['threshold']:g}"


def program_view(program: dict, stats: list[dict] | None, explanation: dict | None, tree: list[dict] | None) -> dict:
    rules = {r["rule_id"]: r for r in program["rules"]}
    notes = {e["group_id"]: e for e in (explanation or {}).get("rules", [])}
    stats = {s["group_id"]: s for s in (stats or [])}
    groups = []
    for group in program["groups"]:
        atoms = [rules[r] for r in group["rule_ids"]]
        note = notes.get(group["group_id"], {})
        groups.append({
            "id": group["group_id"],
            "operator": group["operator"],
            "atoms": [{"rule_id": a["rule_id"], "feature": a["feature_id"], "operator": a["operator"], "threshold": a["threshold"],
                       "policy": a["source_refs"], "text": condition_text(a)} for a in atoms],
            "policy": sorted({p for a in atoms for p in a["source_refs"]}),
            "austrac_ref": note.get("austrac_ref"),
            "estimated_precision": stats.get(group["group_id"], {}).get("estimated_population_precision"),
            "estimated_windows": stats.get(group["group_id"], {}).get("estimated_population_windows"),
            "explanation": note.get("explanation"),
            "concerns": note.get("concerns"),
        })
    return {"groups": groups, "combine": program["group_operator"], "overall": (explanation or {}).get("overall"),
            "atom_count": len(program["rules"]), "tree": tree}


def metrics_view(metrics: dict | None) -> dict | None:
    if not metrics:
        return None
    keys = ("days", "chains", "chains_hit", "chain_recall", "flagged_account_days", "flagged_per_day", "flagged_share",
            "flagged_accounts", "window_precision", "window_recall",
            # Versions evaluated before the recall objective carry daily-budget metrics.
            "rule_hits", "alerts", "tp", "fp", "precision", "recall", "daily_alert_budget")
    return {k: metrics.get(k) for k in keys if metrics.get(k) is not None}


def versions_view(store: Store) -> list[dict]:
    """Version history for the rule library: rules, effect and change against the parent."""
    views = []
    for entry in history(store):
        validation = (entry.get("metrics") or {}).get("validation")
        parent = (entry.get("metrics") or {}).get("parent_validation")
        views.append({
            "id": entry["id"], "label": entry["label"], "status": entry["status"], "decision": entry.get("decision"),
            "reason": entry.get("reason"), "objective": entry.get("objective", "reference"), "parent_label": entry.get("parent_label"),
            "plan": entry.get("plan"), "fit": entry.get("fit"), "trigger": entry.get("trigger"), "cycle_id": entry.get("cycle_id"),
            "investigated_cases": entry.get("investigated_cases"), "development_sample": entry.get("development_sample"),
            "created": entry.get("created"),
            "rules": program_view(entry["program"], entry.get("group_stats"), entry.get("explanation"), entry.get("tree")),
            "validation": metrics_view(validation), "parent_validation": metrics_view(parent), "diff": entry.get("diff"),
        })
    return views


def percentiles(learner: RuleLearner) -> dict:
    """Development-population distribution of every feature (p50/p90/p99), computed in Zetaris; cached."""
    cache = OUTPUT / "feature-percentiles.json"
    if cache.exists():
        return json.loads(cache.read_text())
    names = list(FEATURES)
    columns = ", ".join(f"PERCENTILE_APPROX(CAST({n} AS DOUBLE), ARRAY(0.5, 0.9, 0.99))" for n in names)
    row = learner.zetaris.run(f"SELECT {columns} FROM ({learner.features('development')}) p", 2)[0]
    result = {}
    for name, cell in zip(names, row):
        values = json.loads(cell) if isinstance(cell, str) else cell
        result[name] = dict(zip(("p50", "p90", "p99"), [float(v) for v in values])) if values else None
    cache.write_text(json.dumps(result))
    return result


def evaluation_view(store: Store) -> dict:
    """The chain-based evaluation of every layer for the showcase: rules, tiering, investigation, proposals."""
    showcase = ROOT / ".runtime/showcase/metrics.json"
    metrics = json.loads(showcase.read_text()) if showcase.exists() else {}

    def tiering(m: dict | None) -> dict | None:
        if not m:
            return None
        return {"tiers": {t: m[t] for t in ("high", "mid", "low")}, "capacity": m.get("budgets"),
                "alerts_per_day_for_60pct_chains": m.get("alerts_per_day_for_60pct_chains"),
                "alerts_per_day_for_90pct_chains": m.get("alerts_per_day_for_90pct_chains")}

    layer1 = metrics.get("layer1_validation") or {}
    from .tier_agent import ALIAS as TIER_ALIAS

    primary_id = store.program_pointer(TIER_ALIAS)
    primary = store.read_artifact(primary_id, "tier_policy_version") if primary_id else {}
    tier_versions = [{k: body.get(k) for k in ("label", "status", "decision", "reason", "trigger", "created", "best_round")}
                     | {"primary": aid == primary_id} | ({"status": "primary"} if aid == primary_id else {})
                     | {"validation": tiering(body.get("validation")), "validation_reference": tiering(body.get("validation_reference")),
                        "high_risk_segments": [s for s in (body.get("policy") or {}).get("segments", []) if s.get("tier") == "high"],
                        "patterns": body.get("patterns")}
                     for aid, body in store.artifacts("tier_policy_version")]
    proposals = [body for _, body in store.artifacts("feature_proposal")]
    rounds = {}
    for path in sorted((ROOT / ".runtime/investigator-rounds").glob("*.json")):
        body = json.loads(path.read_text())
        summary = body.get("summary") or {}
        if summary.get("evaluation"):
            rounds[body["round"]] = {"split": body["split"], "cases": len(body.get("cases", [])),
                                     "usable": sum(c["usable"] for c in body.get("cases", [])), **summary["evaluation"]}
    return {
        "definitions": {
            "chain_recall": "Share of in-scope laundering chains with any member account alerted on any day",
            "full_chain_account_recall": "Share of member accounts of recalled chains that are alerted (or, after investigation, found)",
            "chain_precision": "Share of alerted accounts that belong to a laundering chain (any day of the partition)",
            "window_precision": "Share of alerted account-days on which the account took part in a laundering transfer",
            "capacity": "Top of the priority queue at 2,000 / 5,000 / 10,000 investigated alerts a day: chain recall and chain precision",
            "exclusion_rate": "Investigated alerts whose account is in no chain that the senior investigator did not report",
            "false_exclusion_rate": "Investigated alerts whose account is in a chain that were not reported (should be ~0)",
            "new_chain_accounts_found": "Chain accounts the team named that the layer-1 rule never alerted on",
            "account_verdict_precision": "Accounts named suspicious that belong to a laundering chain",
            "transaction_precision": "Suspect transactions labelled laundering",
        },
        "layer1": {k: layer1.get(k) for k in ("chain_recall", "full_chain_account_recall", "chain_precision", "window_precision",
                                              "flagged_per_day", "flagged_accounts", "chains", "recalled_chain_accounts")} if layer1 else None,
        # The tier agent's published policy is the primary tiering; the tier model is its reference input.
        "tiering": {"primary": "tier_agent", "tier_agent_label": primary.get("label"),
                    # Chain-based re-evaluation in the showcase metrics supersedes the record's own evaluation.
                    "tier_agent": tiering(metrics.get(f"tier_agent_{primary.get('label')}_validation") or primary.get("validation")),
                    "tier_model_reference": tiering(metrics.get("tier_model_validation") or primary.get("validation_reference"))},
        "tier_agent_versions": tier_versions,
        "investigation_rounds": rounds,
        "feature_proposals": proposals[-3:],
    }


def features_view(distribution: dict) -> dict:
    return {
        name: {"zh": FEATURE_DOCS[name][0], "calc": FEATURE_DOCS[name][1], "formula": FEATURE_DOCS[name][2],
               "description": DESCRIPTIONS[name], "unit": UNITS[name], "policy": list(POLICY[name]), "table": SOURCE_TABLE[name],
               "distribution": distribution.get(name)}
        for name in FEATURES
    }


def export(log=print, snapshot: bool = False) -> dict:
    """Write .runtime/dashboard/data.json (and, with `snapshot`, the committed copy under apps/dashboard)."""
    from .alert_trace import view as trace_view
    from .showcase import investigation_learning, view as showcase_view

    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = Settings.load()
    store = Store(settings.database)
    cached = OUTPUT / "feature-percentiles.json"
    if cached.exists():
        distribution = json.loads(cached.read_text())
    else:
        from .feature_store import FeatureStore

        distribution = percentiles(RuleLearner(store, None, FeatureStore(settings), log=log))
    versions = versions_view(store)
    evaluation = evaluation_view(store)
    data = {
        "generated_at": time.time(),
        "dataset": {"window": WINDOW},
        "features": features_view(distribution),
        "policies": {p["paragraph_id"]: {"text": p["anchor_text"], "zh": p.get("paraphrase_zh"), "section": p["section"], "url": p["source_url"]}
                     for p in policy_registry()},
        "versions": versions,
        "active_version": store.program_pointer("learned-current"),
        "evaluation": evaluation,
        "showcase": {**showcase_view(store, versions), "investigation_agent": investigation_learning(evaluation["investigation_rounds"])},
        "trace": trace_view(store),
    }
    path = OUTPUT / "data.json"
    path.write_text(json.dumps(data, ensure_ascii=False, default=str))
    if snapshot:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, SNAPSHOT)
    summary = {"versions": len(versions), "cases": len(data["trace"]["cases"]), "bytes": path.stat().st_size, "snapshot": snapshot}
    log(json.dumps(summary))
    return summary
