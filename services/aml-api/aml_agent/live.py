"""Live investigation from the dashboard: alert candidates, case request and progress (no labels, no secrets)."""

from __future__ import annotations

import json
import time

from .contracts import CaseRequest
from .settings import ROOT
from .store import Store

LIVE_SLOTS = 2
NEEDS_SETUP = {
    "zh": "实时调查需要你自己的 NVIDIA API key 和数据访问：复制 .env.example 为 .env.local，填入 NVIDIA_API_KEY"
          "（在 https://build.nvidia.com 申请）和 Zetaris 访问配置，然后用 python -m aml_agent.cli serve 启动完整服务。",
    "en": "Live investigation needs your own NVIDIA API key and data access: copy .env.example to .env.local, set NVIDIA_API_KEY "
          "(get one at https://build.nvidia.com) and the Zetaris settings, then start the full API with python -m aml_agent.cli serve.",
}


def missing_setup(settings) -> list[str]:
    """What this server lacks for a live investigation (model keys for every agent role, Zetaris access)."""
    missing = [f"{role} model key" for role in ("investigator", "senior_investigator") if not settings.role(role).nvidia_key]
    if not settings.zetaris_key:
        missing.append("Zetaris API key")
    return missing
ROUNDS = ROOT / ".runtime/investigator-rounds"
QUESTION = ("Investigate this rule alert: which transactions, if any, are suspicious, and what upstream/downstream "
            "evidence supports or contradicts that?")


def primary_policy(store: Store) -> dict:
    from .tier_agent import ALIAS

    pointer = store.program_pointer(ALIAS)
    return (store.read_artifact(pointer, "tier_policy_version") or {}).get("policy") or {} if pointer else {}


def candidates(store: Store, limit: int = 40) -> list[dict]:
    """Stored validation alerts of the investigation rounds, the showcase cases first, tiered by the tier agent."""
    from .showcase import tier_of

    policy = primary_policy(store)
    showcase = set()
    data = ROOT / ".runtime/dashboard/data.json"
    if data.exists():
        for case in json.loads(data.read_text()).get("showcase", {}).get("investigation_highlights", []):
            showcase.add(store.get(case["run_id"])["request"].get("alert_id"))
    seen, out = set(), []
    for path in sorted(ROUNDS.glob("val-*.json"), reverse=True):
        for item in json.loads(path.read_text()).get("alerts", []):
            alert = store.read_artifact(item["alert_id"], "rule_alert")
            key = (alert["account"]["bank"], alert["account"]["account"], alert["day"])
            if key in seen and item["alert_id"] not in showcase:
                continue
            seen.add(key)
            tier = tier_of(policy, alert["features"]) if policy else {"tier": alert.get("tier"), "name": "tier model"}
            out.append({"alert_id": item["alert_id"], "account": alert["account"], "day": alert["day"],
                        "tier": tier["tier"], "tier_rule": tier.get("name"), "tier_reason": tier.get("reason"),
                        "rules_triggered": len(alert["triggered_rule_ids"]), "showcase": item["alert_id"] in showcase})
    order = {"high": 0, "mid": 1, "low": 2}
    out.sort(key=lambda a: (not a["showcase"], order.get(a["tier"], 3)))
    # One entry per account-day (a showcase alert wins over a repeat of the same account-day).
    unique = {}
    for item in out:
        unique.setdefault((item["account"]["bank"], item["account"]["account"], item["day"]), item)
    return list(unique.values())[:limit]


def register_listed(store: Store, key: str) -> str:
    """Store an alert from the full alert index (key alert:<bank>:<account>:<day>) so it can be investigated."""
    from datetime import date, timedelta

    from . import alert_index
    from .feature_registry import FEATURES
    from .harness import content_hash
    from .rule_learning import triggered
    from .rule_program import load_program, program_of

    _, bank, account, day = key.split(":")
    db = alert_index.connect()
    row = db.execute("SELECT * FROM alerts WHERE account = ? AND day = ?", (f"{bank}:{account}", day)).fetchone() if db else None
    if row is None:
        # Not alerted by the active rules (e.g. a case investigated under an earlier version): reuse its stored alert.
        for path in sorted((ROOT / ".runtime/investigator-rounds").glob("*.json"), reverse=True):
            for item in json.loads(path.read_text()).get("alerts", []):
                if item["account"] == {"bank": bank, "account": account} and item["day"] == day:
                    return item["alert_id"]
        raise KeyError("This alert is in neither the alert index nor an investigation round")
    version = db.execute("SELECT value FROM meta WHERE key = 'rule_version'").fetchone()[0]
    from .tier_agent import ALIAS

    grading = store.read_artifact(store.program_pointer(ALIAS), "tier_policy_version")
    features = {n: row[n] for n in FEATURES}
    package = load_program(store, version)
    alert = {"base_version": version, "program_sha256": package["program_sha256"], "account": {"bank": bank, "account": account},
             "start": day + "T00:00:00", "as_of": (date.fromisoformat(day) + timedelta(days=1)).isoformat() + "T00:00:00",
             "triggered_rule_ids": triggered(program_of(package), features), "features": features,
             "scope": "validation_alert_index", "day": day, "split": "validation", "tier": row["tier"], "priority": row["priority"],
             "tier_score": row["priority"], "tier_source": f"risk grading agent {grading['label']}", "tier_model": grading.get("tier_model"),
             "tier_base_rate": ((grading.get("development") or {}).get(row["tier"]) or {}).get("window_precision")}
    alert["alert_sha256"] = content_hash(alert)
    return store.artifact("rule_alert", alert)


def request_for(store: Store, alert_id: str) -> CaseRequest:
    if alert_id.startswith("alert:"):
        alert_id = register_listed(store, alert_id)
    alert = store.read_artifact(alert_id, "rule_alert")
    split = alert.get("split", "development")
    if split not in ("development", "validation"):
        raise ValueError("Live investigation is limited to development and validation alerts")
    return CaseRequest(account=alert["account"], start=alert["start"], as_of=alert["as_of"], split=split,
                       rule_version=alert["base_version"], alert_id=alert_id, investigation_question=QUESTION)


RUNS = ROOT / ".runtime/dashboard/live_runs.json"


def remember(account: str, day: str, run_id: str) -> None:
    runs = json.loads(RUNS.read_text()) if RUNS.exists() else {}
    runs.setdefault(f"{account}|{day}", []).append(run_id)
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    RUNS.write_text(json.dumps(runs))


STALE_SECONDS = 1800


def stalled(store: Store, state: dict) -> bool:
    """A run still marked running with no recorded progress for half an hour (its worker is gone)."""
    if state["status"] not in ("pending", "running") or (state.get("adjudication") or {}).get("decision"):
        return False
    times = [e["at"] for e in store.trace_events(state["id"])] + [n["at"] for n in state.get("setup_log", [])]
    for child in (state.get("team") or {}).get("children", []):
        times += [e["at"] for e in store.trace_events(child)]
    return time.time() - max(times + [state["created"]]) > STALE_SECONDS


def running(store: Store) -> set[tuple[str, str]]:
    """Alerts whose latest live run has not reached the senior investigator's decision and has not failed."""
    out = set()
    for key, runs in (json.loads(RUNS.read_text()) if RUNS.exists() else {}).items():
        try:
            state = store.get(runs[-1])
        except KeyError:
            continue
        if state["status"] in ("pending", "running") and not (state.get("adjudication") or {}).get("decision") and not stalled(store, state):
            account, day = key.split("|")
            out.add((account, day))
    return out


def latest(account: str, day: str) -> str | None:
    runs = json.loads(RUNS.read_text()) if RUNS.exists() else {}
    return (runs.get(f"{account}|{day}") or [None])[-1]


def member_view(store: Store, run_id: str, assignment: dict) -> dict:
    state = store.get(run_id)
    conclusion = state.get("conclusion") or {}
    return {"role": assignment["role"], "question": assignment["question"], "status": state["status"],
            "actions": state["budget"].get("actions"),
            "steps": [{"tool": o["tool"], "arguments": o["arguments"], "found": (o.get("digest") or [])[:4]} for o in state["observations"]],
            "conclusion": conclusion.get("conclusion"), "reason": conclusion.get("brief_reason")}


def progress(store: Store, run_id: str) -> dict:
    """What the team is doing now: phase, each specialist's steps, the team's conclusion and the senior decision."""
    state = store.get(run_id)
    team = state.get("team") or {}
    members = [member_view(store, child, assignment) for assignment, child in zip(team.get("assignments", []), team.get("children", []))]
    conclusion = state.get("conclusion") or {}
    adjudication = state.get("adjudication") or {}
    if adjudication:
        phase = "decided"
    elif conclusion:
        phase = "senior_investigator"
    elif members and all(m["status"] not in ("queued", "running") for m in members):
        phase = "aggregating"
    elif members:
        phase = "investigating"
    else:
        phase = "team_setup"
    if (state["status"] in ("failed", "cancelled", "model_unavailable", "tool_failed") and not conclusion) or stalled(store, state):
        phase = "stopped"
    return {
        "run_id": run_id, "status": state["status"], "phase": phase, "created": state["created"],
        "setup_log": state.get("setup_log", []),
        "account": state["request"]["account"], "day": state["request"]["start"][:10],
        "elapsed_seconds": round(time.time() - state["created"]),
        "tier": (state.get("rule_context") or {}).get("tier"),
        "team_plan": team.get("rationale"), "members": members,
        "conclusion": {k: conclusion.get(k) for k in ("conclusion", "suspicion", "chain", "supported_findings", "counterevidence",
                                                        "suspect_transaction_ids", "account_verdicts", "brief_reason")} if conclusion else None,
        "senior_investigator": {k: adjudication.get(k) for k in ("prior_level", "adjustment", "risk_level", "decision", "rationale",
                                                                  "evidence_flags", "key_evidence", "account_risks", "linked_alerts")}
                               if adjudication else None,
        "error": state.get("error"),
    }
