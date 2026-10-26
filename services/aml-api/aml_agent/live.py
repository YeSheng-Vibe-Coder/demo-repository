"""Live investigation from the dashboard: case request, run status and progress (no labels, no secrets)."""

from __future__ import annotations

import json
import time

from .contracts import ALERT_QUESTION, CaseRequest
from .settings import ROOT, ROUNDS
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


def primary_policy(store: Store) -> dict:
    """The primary grading policy's segments (empty before one is published)."""
    return (store.primary_policy()[1] or {}).get("policy") or {}

def register_listed(store: Store, key: str) -> str:
    """Store an alert from the full alert index (key alert:<bank>:<account>:<day>) so it can be investigated."""
    from . import alert_index
    from .feature_registry import FEATURES
    from .learning_cycle import store_alert
    from .rule_program import load_program

    _, bank, account, day = key.split(":")
    db = alert_index.connect()
    row = db.execute("SELECT * FROM alerts WHERE account = ? AND day = ?", (f"{bank}:{account}", day)).fetchone() if db else None
    if row is None:
        # Not alerted by the active rules (e.g. a case investigated under an earlier version): reuse its stored alert.
        for path in sorted(ROUNDS.glob("*.json"), reverse=True):
            for item in json.loads(path.read_text()).get("alerts", []):
                if item["account"] == {"bank": bank, "account": account} and item["day"] == day:
                    return item["alert_id"]
        raise KeyError("This alert is in neither the alert index nor an investigation round")
    version = db.execute("SELECT value FROM meta WHERE key = 'rule_version'").fetchone()[0]
    _, grading = store.primary_policy()
    item = {"bank": bank, "account": account, "day": day, "features": {n: row[n] for n in FEATURES}}
    return store_alert(store, version, load_program(store, version), item, scope="validation_alert_index", split="validation",
                       tier=row["tier"], priority=row["priority"], tier_score=row["priority"],
                       tier_source=f"risk grading agent {grading['label']}", tier_model=grading.get("tier_model"),
                       tier_base_rate=((grading.get("development") or {}).get(row["tier"]) or {}).get("window_precision"))


def request_for(store: Store, alert_id: str) -> CaseRequest:
    if alert_id.startswith("alert:"):
        alert_id = register_listed(store, alert_id)
    alert = store.read_artifact(alert_id, "rule_alert")
    split = alert.get("split", "development")
    if split not in ("development", "validation"):
        raise ValueError("Live investigation is limited to development and validation alerts")
    return CaseRequest(account=alert["account"], start=alert["start"], as_of=alert["as_of"], split=split,
                       rule_version=alert["base_version"], alert_id=alert_id, investigation_question=ALERT_QUESTION)


RUNS = ROOT / ".runtime/dashboard/live_runs.json"


def load_runs() -> dict[str, list[str]]:
    """Every live run per alert ("account|day"), oldest first; the last one is the alert's current investigation."""
    return json.loads(RUNS.read_text()) if RUNS.exists() else {}


def remember(account: str, day: str, run_id: str) -> None:
    runs = load_runs()
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


MAX_RETRIES = 2


def outcome(store: Store, state: dict) -> str:
    """investigated (the senior investigator decided), running, or failed (ended or stalled without a decision)."""
    if (state.get("adjudication") or {}).get("decision"):
        return "investigated"
    if state["status"] in ("pending", "running") and not stalled(store, state):
        return "running"
    return "failed"


def statuses(store: Store) -> dict[tuple[str, str], dict]:
    """Status of every alert with a live investigation, from its latest run (a retry replaces a failed attempt)."""
    out = {}
    for key, runs in load_runs().items():
        try:
            state = store.get(runs[-1])
        except KeyError:
            continue
        account, day = key.split("|")
        out[(account, day)] = {"status": outcome(store, state), "error": state.get("error"), "attempt": state.get("attempt", 1),
                               "run_id": runs[-1]}
    return out


def latest(account: str, day: str) -> str | None:
    return (load_runs().get(f"{account}|{day}") or [None])[-1]


def progress(store: Store, run_id: str) -> dict:
    """Where a live investigation stands: phase, outcome, retries and setup notes (the case itself comes from run_detail)."""
    state = store.get(run_id)
    children = [store.get(child)["status"] for child in (state.get("team") or {}).get("children", [])]
    if state.get("adjudication"):
        phase = "decided"
    elif state.get("conclusion"):
        phase = "senior_investigator"
    elif children and all(status not in ("queued", "running") for status in children):
        phase = "aggregating"
    elif children:
        phase = "investigating"
    else:
        phase = "team_setup"
    if (state["status"] in ("failed", "cancelled", "model_unavailable", "tool_failed") and not state.get("conclusion")) or stalled(store, state):
        phase = "stopped"
    return {
        "run_id": run_id, "phase": phase, "created": state["created"], "elapsed_seconds": round(time.time() - state["created"]),
        "outcome": outcome(store, state), "attempt": state.get("attempt", 1), "max_attempts": MAX_RETRIES + 1,
        "retried_by": state.get("retried_by"), "setup_log": state.get("setup_log", []), "error": state.get("error"),
    }
