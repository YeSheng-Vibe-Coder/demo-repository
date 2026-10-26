"""Investigator training from a labelled development round (evaluation side).

The model's weights do not change. A training round of team investigations on
development alerts, graded with labels afterwards, produces three things the team
reads at run time, saved together as a numbered investigator configuration:

  reference cases     compact summaries of trained cases with their outcome, recalled
                      by tier and feature similarity (never the case under investigation)
  tool effectiveness  per tier, how often each tool's observations contained a
                      laundering transfer in laundering cases, and how often it was
                      used in false-positive cases
  verdict thresholds  per tier, the lowest aggregator suspicion level that still keeps
                      the configured share of laundering cases, measured on the round

Investigators never see the label of the case they investigate; they see outcomes of
other, already-closed development cases, as an analyst would see past case files.
"""

from __future__ import annotations

import json
import math
import time

from .calibration import position
from .contracts import account_ref
from .feature_registry import FEATURES
from .profile import PROFILE
from .settings import ROOT

SETTINGS = PROFILE["investigator_training"]
LEVELS = ("low", "medium", "high")
CACHE = ROOT / ".runtime/investigator-config.json"


def observation_hits(store, labels_reader, run_ids: list[str]) -> list[dict]:
    """Every tool observation of the team (all children) with whether it returned a laundering transfer."""
    out = []
    for run_id in run_ids:
        state = store.get(run_id)
        members = [store.get(child) for child in (state.get("team") or {}).get("children", [])] or [state]
        for member in members:
            role = (member["request"].get("assignment") or {}).get("role", "investigator")
            for observation in member["observations"]:
                if observation["tool"] == "get_alert_context":
                    continue
                rows = member["evidence"].get(observation["evidence_ref"], {}).get("rows", [])
                ids = [str(r["transaction_id"]) for r in rows if str(r.get("transaction_id", "")).isdigit()][:400]
                labels = labels_reader.read(ids) if ids else {}
                out.append({"run_id": run_id, "role": role, "tool": observation["tool"], "rows": len(ids),
                            "laundering_rows": sum(labels.values())})
    return out


def tool_effectiveness(cases: list[dict], hits: list[dict]) -> dict:
    by_run = {c["run_id"]: c for c in cases}
    table: dict = {}
    for hit in hits:
        case = by_run.get(hit["run_id"])
        if not case:
            continue
        cell = table.setdefault(case.get("tier") or "none", {}).setdefault(hit["tool"], {
            "uses_in_laundering_cases": 0, "found_laundering_transfer": 0, "uses_in_normal_cases": 0, "empty_results": 0})
        if case["window_label"]:
            cell["uses_in_laundering_cases"] += 1
            cell["found_laundering_transfer"] += hit["laundering_rows"] > 0
        else:
            cell["uses_in_normal_cases"] += 1
        cell["empty_results"] += hit["rows"] == 0
    for tools in table.values():
        for cell in tools.values():
            uses = cell["uses_in_laundering_cases"]
            cell["hit_rate_in_laundering_cases"] = round(cell["found_laundering_transfer"] / uses, 3) if uses else None
    return table


def thresholds(cases: list[dict]) -> dict:
    """Per tier: the lowest suspicion level that keeps the target share of laundering cases reported."""
    target = SETTINGS["case_recall_target"]
    result = {}
    for tier in ("high", "mid", "low"):
        rows = [c for c in cases if c.get("tier") == tier and c.get("suspicion") in LEVELS]
        positives = [c for c in rows if c["window_label"]]
        negatives = [c for c in rows if not c["window_label"]]
        options = []
        for cutoff in LEVELS:
            rank = LEVELS.index(cutoff)
            kept = sum(LEVELS.index(c["suspicion"]) >= rank for c in positives)
            flagged = sum(LEVELS.index(c["suspicion"]) >= rank for c in negatives)
            options.append({"report_at_or_above": cutoff, "laundering_cases_reported": kept, "laundering_cases": len(positives),
                            "normal_cases_reported": flagged, "normal_cases": len(negatives),
                            "case_recall": kept / len(positives) if positives else None})
        # Highest cutoff that still meets the recall target (fewest false reports); otherwise report everything.
        eligible = [o for o in options if o["case_recall"] is not None and o["case_recall"] >= target]
        chosen = eligible[-1] if eligible else options[0]
        result[tier] = {"chosen": chosen, "options": options, "basis": f"case recall >= {target:.0%} on the training round"}
    return result


def case_summary(case: dict, state: dict, team: list[dict]) -> dict:
    conclusion = state.get("conclusion") or {}
    return {
        "run_id": case["run_id"], "account": account_ref(case["account"]), "day": case["day"],
        "tier": case.get("tier"), "features": case["features"], "outcome": "laundering" if case["window_label"] else "normal",
        "team_conclusion": case["conclusion"], "suspicion": case.get("suspicion"),
        "roles": [f"{m['role']}:{','.join(m.get('tools', []))}" for m in team][:4],
        "key_findings": (conclusion.get("supported_findings") or [])[:2] + (conclusion.get("counterevidence") or [])[:1],
        "laundering_transfers_found": case["sample_check"]["positive_transactions_retrieved"],
        "correct": (case["conclusion"] == "suspicious") == bool(case["window_label"]),
    }


def evidence_table(store, cases: list[dict]) -> dict:
    """Path A: laundering rate of investigated development cases per evidence flag (labels: chain membership)."""
    table: dict = {}
    labelled = [c for c in cases if "focus_in_chain" in c]
    for case in labelled:
        flags = {f["flag"] for f in (store.get(case["run_id"]).get("adjudication") or {}).get("evidence_flags", [])}
        for flag in flags:
            cell = table.setdefault(flag, {"cases": 0, "laundering": 0})
            cell["cases"] += 1
            cell["laundering"] += bool(case["focus_in_chain"])
    for cell in table.values():
        cell["laundering_rate"] = round(cell["laundering"] / cell["cases"], 3)
    if labelled:
        table["_base"] = {"cases": len(labelled), "laundering_rate": round(sum(bool(c["focus_in_chain"]) for c in labelled) / len(labelled), 3)}
    return table


def build(store, labels_reader, cases: list[dict], round_name: str, threshold_cases: list[dict] | None = None) -> dict:
    """An investigator configuration from graded development rounds.

    `threshold_cases` (default: `cases`) set the verdict standard; they must come from a round run
    under the same prompts, since suspicion levels are only comparable within one prompt regime.
    """
    usable = [c for c in cases if c["status"] in ("completed", "insufficient_evidence")]
    standard = [c for c in (threshold_cases or []) if c["status"] in ("completed", "insufficient_evidence")]
    hits = observation_hits(store, labels_reader, [c["run_id"] for c in usable])
    library = []
    for case in usable:
        state = store.get(case["run_id"])
        team = (state.get("team") or {}).get("assignments", [])
        library.append(case_summary(case, state, team))
    existing = [body for _, body in store.artifacts("investigator_config")]
    config = {
        "version": f"inv-v{len(existing) + 1}",
        "trained_on": round_name, "created": time.time(), "cases": len(usable),
        "reference_cases": library,
        "tool_effectiveness": tool_effectiveness(usable, hits),
        # No standard rounds: the evidence-consistent conclusion decides what is reported.
        "thresholds": thresholds(standard) if standard else {},
        "evidence_table": evidence_table(store, usable),
    }
    config["id"] = store.artifact("investigator_config", config)
    CACHE.write_text(json.dumps(config))
    return config


def load() -> dict | None:
    return json.loads(CACHE.read_text()) if CACHE.exists() else None


def similar(config: dict, tier: str | None, features: dict, exclude: tuple[str, str], calibration: dict | None, per_outcome: int = 2) -> list[dict]:
    """Nearest trained cases of the same tier (any tier if too few), by population-normalised feature distance."""
    def scaled(name: str, value) -> float:
        if value is None:
            return 0.0
        cuts = (calibration or {}).get("quantiles", {}).get(name) or [0, 1]
        spread = max(cuts[-1] - cuts[0], 1e-6)
        return min(float(value) / spread, 5.0)

    def distance(case: dict) -> float:
        return math.sqrt(sum((scaled(n, case["features"].get(n)) - scaled(n, features.get(n))) ** 2 for n in FEATURES))

    pool = [c for c in config["reference_cases"] if (c["account"], c["day"]) != exclude]
    same = [c for c in pool if c["tier"] == tier]
    picked = []
    for outcome in ("laundering", "normal"):
        candidates = [c for c in same if c["outcome"] == outcome] or [c for c in pool if c["outcome"] == outcome]
        picked += sorted(candidates, key=distance)[:per_outcome]
    return picked


def brief_lines(config: dict | None, rule_context: dict | None, request: dict, calibration: dict | None, role: str) -> dict:
    """Brief sections from the trained configuration for one case and team role."""
    if not config or not rule_context:
        return {}
    tier = (rule_context.get("tier") or {}).get("tier")
    alert = rule_context.get("alert") or {}
    focus = (account_ref(request["account"]), request["start"][:10])
    cases = similar(config, tier, alert.get("features") or {}, focus, calibration)
    sections = {
        "REFERENCE CASES (closed development cases with known outcome; not this case)": [
            f"{c['outcome'].upper()} tier {c['tier']} {c['day']}: "
            + ", ".join(f"{k}={v:g}" + (f"({position(k, v, calibration)})" if position(k, v, calibration) not in ("", "typical") else "")
                        for k, v in c["features"].items() if v not in (None, 0, 0.0))[:220]
            + f" | team said {c['team_conclusion']}/{c['suspicion'] or '-'} ({'right' if c['correct'] else 'wrong'})"
            + f" | laundering transfers found {c['laundering_transfers_found']}"
            + (f" | {c['key_findings'][0][:120]}" if c["key_findings"] else "")
            for c in cases
        ]
    }
    if role in ("coordinator", "aggregator", "specialist"):
        tools = (config["tool_effectiveness"].get(tier) or {})
        sections["TOOL EFFECTIVENESS (this tier, training round)"] = [
            f"{name}: found a laundering transfer in {cell['found_laundering_transfer']}/{cell['uses_in_laundering_cases']} uses on laundering cases; "
            f"{cell['uses_in_normal_cases']} uses on normal cases; {cell['empty_results']} empty results"
            for name, cell in sorted(tools.items(), key=lambda item: -(item[1]["hit_rate_in_laundering_cases"] or 0))
        ][:8]
    if role == "aggregator" and tier in config["thresholds"]:
        chosen = config["thresholds"][tier]["chosen"]
        sections["VERDICT STANDARD (this tier)"] = [
            f"Cases rated suspicion '{chosen['report_at_or_above']}' or higher are reported: on the training round this kept "
            f"{chosen['laundering_cases_reported']}/{chosen['laundering_cases']} laundering cases and reported "
            f"{chosen['normal_cases_reported']}/{chosen['normal_cases']} normal cases. Rate suspicion from the evidence; "
            "recall comes first in this screening funnel."
        ]
    return sections
