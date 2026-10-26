"""End-to-end learning cycle on the Zetaris feature store.

initial high-recall rule (learned from development labels, gated on validation chain recall)
  -> full development scan for alerts
  -> parallel investigator agents (no labels)
  -> exact-ID label grading (evaluation side)
  -> rule learner agent refits from development labels plus investigated windows
  -> validation comparison against the actual parent -> publish or hold
  -> monitor-partition rollback check

Investigators and the rule learner are separate agents with separate inputs.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx

from .contracts import CaseRequest
from .evaluation import TransactionLabelReader, grade_samples, has_model_finish
from .evidence import transaction_ledger
from .feature_registry import DESCRIPTIONS, FEATURES
from .feature_store import LABEL_TABLE, FeatureStore
from .harness import content_hash
from .providers import Nvidia
from .rule_learning import SETTINGS, RuleLearner, condition_sql, training_days_note, triggered
from .rule_program import compile_program, load_program, program_of, reference_program
from .rule_versions import diff
from .settings import ROOT, Settings
from .store import Store

API = "http://127.0.0.1:8010"
ALIAS = SETTINGS["alias"]


def feature_contrast(cases: list[dict]) -> dict:
    """Median of every registered feature over investigated positive vs negative windows."""
    def median(values: list[float]) -> float | None:
        values = sorted(values)
        return None if not values else round(values[len(values) // 2], 3)

    return {
        name: {
            "true_positive": median([c["features"][name] for c in cases if c["window_label"] and c["features"].get(name) is not None]),
            "false_positive": median([c["features"][name] for c in cases if not c["window_label"] and c["features"].get(name) is not None]),
        }
        for name in FEATURES
    }


def round_summary(cases: list[dict]) -> dict:
    """Per tier and window label: how often the team concluded suspicious; plus account-level verdict quality."""
    summary: dict = {}
    cases = [c for c in cases if c["usable"]]  # infrastructure failures say nothing about judgement
    risk: dict = {}
    for case in cases:
        level = (case.get("adjudication") or {}).get("risk_level")
        if level:
            cell = risk.setdefault(case.get("tier") or "none", {}).setdefault(level, {"laundering": 0, "normal": 0})
            cell["laundering" if case["window_label"] else "normal"] += 1
    for case in cases:
        cell = summary.setdefault(case.get("tier") or "none", {}).setdefault("laundering" if case["window_label"] else "normal",
                                                                         {"cases": 0, "suspicious": 0, "reported": 0, "by_suspicion": {}})
        cell["cases"] += 1
        cell["suspicious"] += case["conclusion"] == "suspicious"
        cell["reported"] += bool(case.get("reported", case["conclusion"] == "suspicious"))
        level = case.get("suspicion") or "none"
        cell["by_suspicion"][level] = cell["by_suspicion"].get(level, 0) + 1
    verdicts = [v for case in cases for v in case.get("account_verdicts", [])]
    flagged = [v for v in verdicts if v["verdict"] == "suspicious"]
    chain_cases = [c for c in cases if c.get("focus_in_chain")]
    normal_cases = [c for c in cases if "focus_in_chain" in c and not c["focus_in_chain"]]
    identified = sum(len(c.get("identified_accounts", [])) for c in cases)
    found = sum(len(c.get("chain_accounts_found", [])) for c in cases)
    tp, fp = sum(c["sample_check"]["tp"] for c in cases), sum(c["sample_check"]["fp"] for c in cases)
    evaluation = {
        # Investigation team: complete the chain and clear false alarms without losing laundering.
        "chain_cases": len(chain_cases), "normal_cases": len(normal_cases),
        "exclusion_rate": sum(not c["reported"] for c in normal_cases) / len(normal_cases) if normal_cases else None,
        "false_exclusion_rate": sum(not c["reported"] for c in chain_cases) / len(chain_cases) if chain_cases else None,
        "chain_accounts_in_files": sum(len(c.get("chain_accounts_in_file", [])) for c in cases),
        "chain_accounts_found": found,
        "new_chain_accounts_found": sum(len(c.get("new_chain_accounts_found", [])) for c in cases),
        "account_verdict_precision": found / identified if identified else None,
        "transaction_precision": tp / (tp + fp) if tp + fp else None,
        "linked_alerts_per_case": sum(len(c.get("linked_alerted_accounts", [])) for c in cases) / len(cases) if cases else None,
    }
    return {
        "evaluation": evaluation,
        "by_tier": summary,
        "adjudicated_risk": risk,
        "transaction_tp": sum(c["sample_check"]["tp"] for c in cases),
        "transaction_fp": sum(c["sample_check"]["fp"] for c in cases),
        "accounts_judged": len(verdicts),
        "accounts_flagged": len(flagged),
        "accounts_flagged_in_chain": sum(v["in_laundering_chain"] for v in flagged),
        "chain_accounts_judged": sum(v["in_laundering_chain"] for v in verdicts),
    }


class LearningCycle:
    def __init__(self, settings: Settings, token: str):
        self.settings, self.token = settings, token
        self.store = Store(settings.database)
        self.learner = RuleLearner(self.store, Nvidia(settings.role("rule_learner")), FeatureStore(settings), log=self.say)
        self.record: dict = {}
        self.output: Path | None = None

    # ---- bookkeeping ------------------------------------------------------------------

    def say(self, message: str) -> None:
        print(message, flush=True)

    def checkpoint(self, phase: str, **values) -> None:
        self.record.update(values)
        self.record["phases"].append({"phase": phase, "at": time.time()})
        self.output.write_text(json.dumps(self.record, indent=2, default=str))
        self.say(json.dumps({"phase": phase, **{k: v for k, v in values.items() if isinstance(v, (str, int, float, bool))}}))

    def run(self, cases: int, parallel: int, fresh: bool = False) -> dict:
        folder = ROOT / ".runtime/learning-cycles"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (folder / "cycle.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            identifier = uuid.uuid4().hex
            self.output = folder / f"{identifier}.json"
            self.record = {"cycle_id": identifier, "phases": [], "created": time.time(), "test_partition_opened": False,
                           "labels_given_to_investigators": False, "production_eligible": False,
                           "training": training_days_note()}
            try:
                return self._run(cases, parallel, fresh)
            except Exception as error:
                self.checkpoint("failed", status="failed", error=f"{type(error).__name__}: {str(error)[:300]}")
                raise

    # ---- phases -----------------------------------------------------------------------

    def reference_version(self) -> str:
        compiled = compile_program(reference_program())
        for version in [self.store.program_pointer("research-reference")]:
            if version:
                return version
        source = json.loads((ROOT / "sources/ibm_hi_large_manifest.json").read_text())
        version = self.store.artifact(
            "rule_program_version",
            {**compiled, "status": "research_reference", "source_sha256": source["source_sha256"],
             "basis": "Pre-registered hand-set reference thresholds; not learned and not approved", "parent_version": None},
        )
        self.store.set_program_pointer("research-reference", version, expected=None)
        return version

    def learn(self, plan: dict, base_version: str, extra_rows: list[dict] | None = None, trigger: str = "initial_training") -> dict:
        rows, population = self.learner.sample("development", SETTINGS["development_negative_ratio"])
        weights = {}
        for extra in extra_rows or []:
            key = (extra["bank"], extra["account"], extra["day"])
            weights[key] = SETTINGS["investigated_window_weight"]
            if not any((r["bank"], r["account"], str(r["day"])) == key for r in rows):
                rows.append(extra)
        features = list(plan["features"])
        tree = self.learner.fit(rows, features, plan["max_leaf_nodes"], plan["min_samples_leaf"], plan["positive_weight"], weights)
        learned = self.learner.recall_program(tree, features, rows, population)
        program, stats = learned["program"], learned["stats"]
        nodes = self.learner.tree_view(tree, features, rows, population, learned["roots"])
        explanation = self.learner.explain(program, stats)
        scores = {s["group_id"]: s["estimated_population_precision"] for s in stats}
        self.say(json.dumps({"layer1_fit": learned["fit"]}))
        candidate = self.learner.freeze(
            program,
            parent=base_version,
            basis={"learner": "DecisionTreeClassifier", "objective": "chain_recall", "plan": plan, "fit": learned["fit"],
                   "trigger": trigger, "cycle_id": self.record.get("cycle_id"), "investigated_cases": len(extra_rows or []),
                   "development_sample": {"rows": len(rows), "population": population},
                   "group_stats": stats, "group_scores": scores, "explanation": explanation, "tree": nodes},
        )
        return {"candidate_version": candidate, "program": program, "scores": scores, "stats": stats, "explanation": explanation,
                "tree": nodes, "fit": learned["fit"], "rows": rows}

    def compare(self, candidate: dict, base_version: str) -> dict:
        base = load_program(self.store, base_version)
        base_program = program_of(base)
        validation_candidate = self.learner.chain_metrics(candidate["program"], "validation")
        validation_base = self.learner.chain_metrics(base_program, "validation")
        decision, reason = self.learner.gate(validation_candidate, validation_base)
        evaluation = {"decision": decision, "reason": reason, "candidate": validation_candidate, "baseline": validation_base,
                      "base_version": base_version, "candidate_version": candidate["candidate_version"],
                      "diff": diff(base["program"], candidate["program"].model_dump(mode="json")),
                      "test_partition_opened": False, "production_eligible": False}
        self.say(json.dumps({"validation": {k: validation_candidate[k] for k in ("chain_recall", "flagged_per_day", "window_precision")},
                             "parent": {k: validation_base[k] for k in ("chain_recall", "flagged_per_day", "window_precision")},
                             "decision": decision}))
        evaluation["evaluation_id"] = self.store.artifact("rule_program_evaluation", evaluation)
        return evaluation

    def alerts(self, version: str, count: int) -> list[dict]:
        body = load_program(self.store, version)
        program = program_of(body)
        found = self.learner.scan_alerts(program, "development", count, body.get("group_scores"), SETTINGS["investigation_alerts_per_day"])
        alerts = []
        for item in found:
            start = item["day"] + "T00:00:00"
            as_of = (date.fromisoformat(item["day"]) + timedelta(days=1)).isoformat() + "T00:00:00"
            alert = {"base_version": version, "program_sha256": body["program_sha256"],
                     "account": {"bank": item["bank"], "account": item["account"]}, "start": start, "as_of": as_of,
                     "triggered_rule_ids": triggered(program, item["features"]), "features": item["features"],
                     "scope": "development_full_scan", "day": item["day"]}
            alert["alert_sha256"] = content_hash(alert)
            alert["alert_id"] = self.store.artifact("rule_alert", alert)
            alerts.append(alert)
        return alerts

    def tier_model(self, version: str) -> tuple[str, dict]:
        """The latest layer-2 model fitted for a rule version."""
        found = [(mid, body) for mid, body in self.store.artifacts("risk_tier_model") if body["rule_version"] == version]
        if not found:
            raise ValueError("No risk tiers fitted for the active rule version; run learning-cycle --rules-only")
        return found[-1]

    def tiered_alerts(self, version: str, split: str, per_cell: int) -> list[dict]:
        """Alerts sampled per (tier, window label) for investigator training and evaluation.

        Labels only choose which alerts are sampled; they are never part of the alert an
        investigator receives. Each alert carries its tier and the tier's development base rate.
        """
        from .tier_agent import active_tiering, sample

        body = load_program(self.store, version)
        program = program_of(body)
        tiering = active_tiering(self.store, version, self)
        model_id, rates = tiering["model_id"], tiering["rates"]
        sampled = sample(self.learner, program, tiering, split, per_cell)
        alerts = []
        for item in sampled:
            start = item["day"] + "T00:00:00"
            as_of = (date.fromisoformat(item["day"]) + timedelta(days=1)).isoformat() + "T00:00:00"
            alert = {"base_version": version, "program_sha256": body["program_sha256"],
                     "account": {"bank": item["bank"], "account": item["account"]}, "start": start, "as_of": as_of,
                     "triggered_rule_ids": triggered(program, item["features"]), "features": item["features"],
                     "scope": f"{split}_tier_sample", "day": item["day"], "split": split,
                     "tier": item["tier"], "tier_score": round(item["priority"], 4), "tier_model": model_id,
                     "tier_base_rate": rates[item["tier"]], "tier_source": tiering["source"], "priority": round(item["priority"], 4)}
            alert["alert_sha256"] = content_hash(alert)
            alert["alert_id"] = self.store.artifact("rule_alert", alert)
            alerts.append(alert)
        self.say(json.dumps({"tiered_alerts": len(alerts), "split": split}))
        return alerts

    def investigate(self, alerts: list[dict], parallel: int) -> list[dict]:
        runs = []
        for alert in alerts:
            request = CaseRequest(account=alert["account"], start=alert["start"], as_of=alert["as_of"], split=alert.get("split", "development"),
                                  rule_version=alert["base_version"], alert_id=alert["alert_id"],
                                  investigation_question="Investigate this rule alert: which transactions, if any, are suspicious, and what upstream/downstream evidence supports or contradicts that?")
            response = httpx.post(API + "/v1/cases", json=request.model_dump(mode="json"),
                                  headers={"Authorization": "Bearer " + self.token}, timeout=30)
            response.raise_for_status()
            runs.append((alert, response.json()["id"]))
        logs = ROOT / ".runtime/logs"
        pending, active, results = list(runs), [], {}
        while pending or active:
            while pending and len(active) < parallel:
                alert, run_id = pending.pop(0)
                log = (logs / f"worker-{run_id}.log").open("w")
                process = subprocess.Popen(["node", "--experimental-transform-types", str(ROOT / "services/orchestrator/team.ts"), run_id],
                                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                active.append((alert, run_id, process, log))
                self.say(json.dumps({"investigator_started": run_id, "account": alert["account"], "day": alert["day"]}))
            time.sleep(5)
            for item in list(active):
                if item[2].poll() is not None:
                    item[3].close()
                    active.remove(item)
                    results[item[1]] = item[2].returncode
                    self.say(json.dumps({"investigator_finished": item[1], "exit": item[2].returncode, "status": self.store.get(item[1])["status"]}))
        return [{"alert": alert, "run_id": run_id, "worker_exit": results[run_id]} for alert, run_id in runs]

    def window_labels(self, alerts: list[dict]) -> dict:
        keys = " OR ".join(
            f"(bank = '{a['account']['bank']}' AND account = '{a['account']['account']}' AND day = DATE '{a['day']}')" for a in alerts
        )
        rows = self.learner.zetaris.run(f"SELECT bank, account, CAST(day AS STRING), label FROM {LABEL_TABLE} WHERE {keys}", 1000)
        return {(str(r[0]), str(r[1]), str(r[2])): int(r[3]) for r in rows}

    def chain_accounts(self, split: str, accounts: set[str]) -> set[str]:
        """Evaluation side: which of these accounts belong to a laundering chain of the partition."""
        from .chains import Chains

        refs = ", ".join(f"'{a}'" for a in sorted(accounts) if a.replace(":", "").isalnum())
        if not refs:
            return set()
        rows = self.learner.zetaris.run(f"SELECT DISTINCT account FROM {Chains(self.learner.zetaris).members(split)} m WHERE account IN ({refs})", 5000)
        return {str(r[0]) for r in rows}

    def alerted_accounts(self, split: str, accounts: set[str]) -> set[str]:
        """Evaluation side: which of these accounts the active layer-1 rule alerts on (any day of the partition)."""
        refs = ", ".join(f"'{a}'" for a in sorted(accounts) if a.replace(":", "").isalnum())
        if not refs:
            return set()
        program = program_of(load_program(self.store, self.store.program_pointer(ALIAS)))
        rows = self.learner.zetaris.run(
            f"SELECT DISTINCT CONCAT(bank, ':', account) FROM ({self.learner.features(split, condition_sql(program))}) f "
            f"WHERE CONCAT(bank, ':', account) IN ({refs})", 5000)
        return {str(r[0]) for r in rows}

    @staticmethod
    def identified(state: dict) -> set[str]:
        """Accounts the team names as suspicious: aggregator verdicts plus the senior investigator's medium/high risks."""
        conclusion, verdict = state.get("conclusion") or {}, state.get("adjudication") or {}
        found = {v["account_ref"] for v in conclusion.get("account_verdicts", []) if v["verdict"] == "suspicious"}
        found |= {a["account_ref"] for a in verdict.get("account_risks", []) if a.get("risk_level") in ("medium", "high")}
        return found

    def grade(self, investigated: list[dict]) -> list[dict]:
        windows = self.window_labels([item["alert"] for item in investigated])
        # One membership and one alert lookup per split for every account the cases touch.
        touched: dict[str, set] = {}
        for item in investigated:
            state = self.store.get(item["run_id"])
            split = item["alert"].get("split", "development")
            accounts = touched.setdefault(split, set())
            accounts.add(f"{item['alert']['account']['bank']}:{item['alert']['account']['account']}")
            accounts |= {v["account_ref"] for v in (state.get("conclusion") or {}).get("account_verdicts", [])}
            accounts |= self.identified(state)
            for row in transaction_ledger(state)["transactions"]:
                accounts |= {row["from_account_ref"], row["to_account_ref"]}
        laundering = set().union(*(self.chain_accounts(split, accounts) for split, accounts in touched.items())) if touched else set()
        alerted = set().union(*(self.alerted_accounts(split, accounts) for split, accounts in touched.items())) if touched else set()
        graded = []
        for item in investigated:
            state = self.store.get(item["run_id"])
            ids = [row["transaction_id"] for row in transaction_ledger(state)["transactions"]]
            labels = TransactionLabelReader(self.learner.zetaris).read(ids) if ids else {}
            check = grade_samples(state, labels)
            decisions = (state.get("conclusion") or {}).get("candidate_decisions", [])
            alert = item["alert"]
            team = state.get("team") or {}
            members = [self.store.get(child) for child in team.get("children", [])]
            focus = f"{alert['account']['bank']}:{alert['account']['account']}"
            ledger_accounts = {a for row in transaction_ledger(state)["transactions"] for a in (row["from_account_ref"], row["to_account_ref"])}
            found = self.identified(state)
            graded.append({
                "run_id": item["run_id"], "status": state["status"], "worker_exit": item["worker_exit"],
                "usable": has_model_finish(state), "account": alert["account"], "day": alert["day"],
                "window_label": windows.get((alert["account"]["bank"], alert["account"]["account"], alert["day"]), 0),
                "triggered_rule_ids": alert["triggered_rule_ids"], "features": alert["features"],
                "conclusion": (state.get("conclusion") or {}).get("conclusion"),
                "sample_check": check,
                "candidate_judgements": [{**d, "label": labels.get(d["transaction_id"])} for d in decisions],
                "model_calls": len(state.get("model_calls", [])) + sum(len(m.get("model_calls", [])) for m in members),
                "sql_requests": state["budget"]["sql_requests"] + sum(m["budget"]["sql_requests"] for m in members),
                "tools": [o["tool"] for o in state["observations"]], "error": state.get("error"),
                "chain": (state.get("conclusion") or {}).get("chain", []),
                "tier": alert.get("tier"), "split": alert.get("split", "development"),
                "suspicion": (state.get("conclusion") or {}).get("suspicion"),
                # Reported: the adjudicator's decision (report or review); before adjudication existed,
                # the team's own conclusion.
                "reported": (state.get("adjudication") or {}).get(
                    "reported", (state.get("conclusion") or {}).get("conclusion") == "suspicious"),
                "adjudication": {k: (state.get("adjudication") or {}).get(k) for k in ("status", "risk_level", "decision", "model")}
                                if state.get("adjudication") else None,
                "account_verdicts": [{**v, "in_laundering_chain": v["account_ref"] in laundering}
                                     for v in (state.get("conclusion") or {}).get("account_verdicts", [])],
                # Chain-based evaluation (labels: the laundering chains of the partition).
                "focus_in_chain": focus in laundering,
                "identified_accounts": sorted(found),
                "chain_accounts_found": sorted(a for a in found if a in laundering),
                "new_chain_accounts_found": sorted(a for a in found if a in laundering and a not in alerted and a != focus),
                "chain_accounts_in_file": sorted(a for a in ledger_accounts if a in laundering),
                "linked_alerted_accounts": sorted(a for a in ledger_accounts if a in alerted and a != focus),
                "team": [{"role": a["role"], "question": a["question"], "status": m["status"],
                          "actions": m["budget"]["actions"], "conclusion": (m.get("conclusion") or {}).get("conclusion")}
                         for a, m in zip(team.get("assignments", []), members)],
            })
        self.store.artifact("investigation_grades", {"cases": graded})
        return graded

    def monitor(self, version: str) -> dict:
        current = load_program(self.store, version)
        parent = current.get("parent_version")
        if not parent:
            return {"checked": False, "reason": "Active version has no parent"}
        previous = load_program(self.store, parent)
        now = self.learner.chain_metrics(program_of(current), "monitor")
        before = self.learner.chain_metrics(program_of(previous), "monitor")
        target = SETTINGS["chain_recall_target"]
        degraded = (now["chain_recall"] < target <= before["chain_recall"]) or now["chain_recall"] < before["chain_recall"] - 0.02
        result = {"checked": True, "active_version": version, "parent_version": parent, "current": now, "parent": before,
                  "rolled_back": degraded, "policy": SETTINGS["monitor_rollback"]}
        if degraded:
            result["active_version"] = self.store.rollback_program(ALIAS, version)
        self.store.artifact("rule_program_monitoring", result)
        return result

    def rerun(self, cycle_id: str, parallel: int) -> dict:
        """Re-investigate a finished cycle's exact alerts and compare case by case."""
        folder = ROOT / ".runtime/learning-cycles"
        baseline = json.loads((folder / f"{cycle_id}.json").read_text())
        self.output = folder / f"{cycle_id}-rerun-{uuid.uuid4().hex[:8]}.json"
        self.record = {"baseline_cycle": cycle_id, "phases": [], "created": time.time(),
                       "mode": "team", "labels_given_to_investigators": False}
        alerts = [{**self.store.read_artifact(a["alert_id"], "rule_alert"), "alert_id": a["alert_id"]} for a in baseline["alerts"]]
        self.checkpoint("rerun_start", alert_count=len(alerts))
        graded = self.grade(self.investigate(alerts, parallel))
        before = {(c["account"]["bank"], c["account"]["account"], c["day"]): c for c in baseline["cases"]}
        comparison = []
        for case in graded:
            old = before.get((case["account"]["bank"], case["account"]["account"], case["day"]), {})
            comparison.append({
                "account": case["account"], "day": case["day"], "window_label": case["window_label"],
                "before": {k: old.get("sample_check", {}).get(k) for k in ("tp", "fp", "positive_transactions_retrieved", "retrieved_unique_transactions")}
                | {"status": old.get("status"), "conclusion": old.get("conclusion"), "model_calls": old.get("model_calls")},
                "after": {k: case["sample_check"][k] for k in ("tp", "fp", "positive_transactions_retrieved", "retrieved_unique_transactions")}
                | {"status": case["status"], "conclusion": case["conclusion"], "model_calls": case["model_calls"]},
            })
        self.checkpoint("rerun_graded", cases=graded, comparison=comparison, status="completed")
        self.record["result_path"] = str(self.output)
        return self.record

    def recover(self, parallel: int) -> list[dict]:
        """Keep each alert's finished team run; re-investigate alerts whose run never finished."""
        alerts = [{**self.store.read_artifact(a["alert_id"], "rule_alert"), "alert_id": a["alert_id"]} for a in self.record["alerts"]]
        finished, missing = [], []
        with self.store.connect() as db:
            rows = db.execute("SELECT id, state FROM runs ORDER BY rowid DESC").fetchall()
        for alert in alerts:
            run = next((r["id"] for r in rows if (s := json.loads(r["state"]))["request"].get("alert_id") == alert["alert_id"]
                        and not s["request"].get("assignment") and s["status"] in ("completed", "insufficient_evidence")), None)
            if run:
                finished.append({"alert": alert, "run_id": run, "worker_exit": 0})
            else:
                missing.append(alert)
        self.say(json.dumps({"recovered_runs": len(finished), "reinvestigating": len(missing)}))
        return self.grade(finished + self.investigate(missing, parallel))

    def resume(self, cycle_id: str, parallel: int = 3) -> dict:
        """Continue a cycle: re-investigate unfinished alerts if needed, then update and monitor."""
        folder = ROOT / ".runtime/learning-cycles"
        self.output = folder / f"{cycle_id}.json"
        self.record = json.loads(self.output.read_text())
        if "cases" not in self.record and "alerts" not in self.record:
            raise ValueError("Cycle stopped before its alerts were chosen; start a new cycle")
        self.record["phases"].append({"phase": "resumed", "at": time.time()})
        current = self.record.get("active_version") or self.store.program_pointer(ALIAS)
        try:
            if "cases" not in self.record:
                self.record["cases"] = self.recover(parallel)
                self.checkpoint("investigations_graded", cases=self.record["cases"], active_version=current)
            return self.update_and_monitor(current, self.record["cases"])
        except Exception as error:
            self.checkpoint("failed", status="failed", error=f"{type(error).__name__}: {str(error)[:300]}")
            raise

    def rules(self) -> dict:
        """Layer 1 only: learn the initial high-recall rule, gate it on validation, check the monitor partition."""
        folder = ROOT / ".runtime/learning-cycles"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        identifier = uuid.uuid4().hex
        self.output = folder / f"rules-{identifier}.json"
        self.record = {"cycle_id": identifier, "kind": "rules_only", "phases": [], "created": time.time(), "test_partition_opened": False,
                       "training": training_days_note()}
        self.checkpoint("start", status="running")
        reference = self.reference_version()
        parent = self.store.program_pointer(ALIAS) or reference
        initial = self.learn({"features": list(FEATURES), **SETTINGS["initial_tree"]}, parent)
        evaluation = self.compare(initial, parent)
        published = self.learner.publish(initial["candidate_version"], evaluation, ALIAS)
        self.checkpoint("initial_rule_learned", initial_candidate=initial["candidate_version"], initial_fit=initial["fit"],
                        initial_evaluation=evaluation, publication=published, decision=evaluation["decision"])
        current = self.store.program_pointer(ALIAS)
        scan = self.learner.chain_metrics(program_of(load_program(self.store, current)), "development")
        monitoring = self.monitor(current)
        self.checkpoint("monitor_checked", active_version=current, development_scan=scan, monitoring=monitoring)
        tiers = self.tiers(current, initial["rows"] if published["published"] else None)
        self.checkpoint("risk_tiers_fitted", tiers=tiers, status="completed")
        return self.record

    def tier_feedback(self, names: list[str]) -> list[str]:
        """Development cases whose tier disagreed with the outcome, with the senior investigator's evidence (labels: development only)."""
        lines = []
        for name in names:
            graded = json.loads((ROOT / ".runtime/investigator-rounds" / f"{name}.json").read_text())
            if graded["split"] != "development":
                raise ValueError("Tier feedback uses development rounds only")
            for case in graded["cases"]:
                if not case["usable"]:
                    continue
                missed = case["window_label"] and case.get("tier") == "low"
                false_alarm = not case["window_label"] and case.get("tier") == "high"
                if not (missed or false_alarm):
                    continue
                state = self.store.get(case["run_id"])
                verdict = state.get("adjudication") or {}
                features = ", ".join(f"{k}={v:g}" for k, v in case["features"].items() if v not in (None, 0, 0.0))
                lines.append(f"{'LAUNDERING placed LOW' if missed else 'NORMAL placed HIGH'}: {features[:260]} | senior investigator: "
                             f"{verdict.get('risk_level', '?')}/{verdict.get('decision', '?')}; evidence: {'; '.join(verdict.get('key_evidence', [])[:2])[:240]}")
        return lines

    def tier_agent(self, feedback_rounds: list[str] | None = None, patterns: bool = False) -> dict:
        """Run the tier agent on development aggregates (optionally revising the current policy with
        investigation feedback) and gate its policy on validation."""
        from .providers import Nvidia
        from .tier_agent import ALIAS as TIER_ALIAS
        from .tier_agent import TierAgent

        feedback = self.tier_feedback(feedback_rounds) if feedback_rounds else None
        pointer = self.store.program_pointer(TIER_ALIAS)
        start_from = {**self.store.read_artifact(pointer, "tier_policy_version"), "id": pointer} if (pointer and feedback) else None
        result = TierAgent(self, Nvidia(self.settings.role("tier_agent"))).run(feedback, start_from, patterns)
        folder = ROOT / ".runtime/tier-agent"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        (folder / f"{result['label']}.json").write_text(json.dumps(result, indent=2, default=str))
        return result

    def tier_adopt(self, label: str) -> dict:
        """Re-use a grading policy learned on an earlier rule version on the active one: evaluate, record and publish it.

        Grading policies only read alert features, so a policy carries over; it is recorded as "<label>·<rules>" with
        its development and validation effect on the active version, and the original record is left untouched.
        """
        from .rule_versions import history
        from .tier_agent import ALIAS as TIER_ALIAS, Evaluator, TierPolicy, compile_policy
        from .tiering import score_sql, tier_sql

        source_id, source = next(((aid, body) for aid, body in self.store.artifacts("tier_policy_version") if body.get("label") == label), (None, None))
        if source is None or not source.get("policy"):
            raise ValueError(f"No tier policy {label}")
        version = self.store.program_pointer(ALIAS)
        rules_label = next((e["label"] for e in history(self.store) if e["id"] == version), version[:8])
        program = program_of(load_program(self.store, version))
        model_id, stored = self.tier_model(version)
        tier, priority = compile_policy(TierPolicy.model_validate(source["policy"]), score_sql(stored["model"]))
        evaluator = Evaluator(self.learner, program)
        record = {key: source.get(key) for key in ("policy", "rounds", "patterns", "case_library", "best_round", "development_reference")}
        record.update(
            rule_version=version, tier_model=model_id, created=time.time(), trigger="adopted", source=label, source_id=source_id,
            label=f"{label}·{rules_label}", status="published", decision="publish",
            reason=f"Policy {label} carried over to {rules_label} and published as the primary grading",
            development=evaluator.evaluate("development", tier, priority), validation=evaluator.evaluate("validation", tier, priority),
            validation_reference=evaluator.evaluate("validation", tier_sql(stored["model"]), score_sql(stored["model"])),
        )
        identifier = self.store.artifact("tier_policy_version", record)
        self.store.set_program_pointer_any(TIER_ALIAS, identifier)
        return {"published": record["label"], "policy_id": identifier}

    def tier_publish(self, label: str) -> dict:
        """Make a recorded tier-agent policy the primary tiering (the tier model stays its reference input)."""
        from .tier_agent import ALIAS as TIER_ALIAS

        found = next(((aid, body) for aid, body in self.store.artifacts("tier_policy_version") if body.get("label") == label), None)
        if found is None or not found[1].get("policy"):
            raise ValueError(f"No tier policy {label}")
        self.store.set_program_pointer_any(TIER_ALIAS, found[0])
        self.store.artifact("tier_policy_publication", {"label": label, "policy_id": found[0], "at": time.time(),
                                                         "basis": "tier agent is the primary tiering; tier model is a reference input"})
        return {"published": label, "policy_id": found[0]}

    def investigator_round(self, name: str, split: str, per_cell: int, parallel: int) -> dict:
        """Team investigations of a tier x label sample, graded per tier (training or evaluation round)."""
        folder = ROOT / ".runtime/investigator-rounds"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.output = folder / f"{name}.json"
        version = self.store.program_pointer(ALIAS)
        self.record = {"round": name, "split": split, "rule_version": version, "phases": [], "created": time.time(),
                       "labels_given_to_investigators": False}
        self.checkpoint("start", status="running")
        alerts = self.tiered_alerts(version, split, per_cell)
        self.checkpoint("alerts_sampled", alerts=[{k: a[k] for k in ("alert_id", "account", "day", "tier")} for a in alerts])
        graded = self.grade(self.investigate(alerts, parallel))
        self.checkpoint("graded", cases=graded, summary=round_summary(graded), status="completed")
        return self.record

    def update_from_rounds(self, names: list[str]) -> dict:
        """Rule update driven by graded development investigation rounds: agent plan, refit, gate, monitor, tiers."""
        folder = ROOT / ".runtime/learning-cycles"
        identifier = uuid.uuid4().hex
        self.output = folder / f"update-{identifier}.json"
        cases = []
        for name in names:
            graded = json.loads((ROOT / ".runtime/investigator-rounds" / f"{name}.json").read_text())
            if graded["split"] != "development":
                raise ValueError("Rule updates learn from development rounds only")
            cases += graded["cases"]
        current = self.store.program_pointer(ALIAS)
        self.record = {"cycle_id": identifier, "kind": "rule_update_from_investigations", "rounds": names, "phases": [],
                       "created": time.time(), "test_partition_opened": False, "training": training_days_note(), "cases": cases}
        self.checkpoint("start", status="running", base_version=current, investigated_cases=len(cases))
        record = self.update_and_monitor(current, cases)
        active = self.store.program_pointer(ALIAS)
        if active != current:
            self.checkpoint("risk_tiers_fitted", tiers=self.tiers(active), status="completed")
        return record

    def adjudicate_round(self, name: str, out: str, parallel: int, model: str | None = None) -> dict:
        """Adjudicate every finished case of a stored round (no re-investigation) and re-grade it as a new round."""
        from concurrent.futures import ThreadPoolExecutor

        from .adjudication import adjudicate
        from .tools import Tools
        from .zetaris import Zetaris

        source = json.loads((ROOT / ".runtime/investigator-rounds" / f"{name}.json").read_text())
        tools = Tools(Zetaris(self.settings))
        finished = [c for c in source["cases"] if c["status"] in ("completed", "insufficient_evidence")]

        def one(case: dict):
            try:
                result = adjudicate(self.store, tools, case["run_id"], model)
                self.say(json.dumps({"adjudicated": case["run_id"][:8], "tier": case.get("tier"), "label": case["window_label"],
                                     "risk": result.get("risk_level"), "decision": result.get("decision")}))
            except Exception as error:  # one failed case never stops the round
                self.say(json.dumps({"adjudication_failed": case["run_id"][:8], "error": str(error)[:200]}))

        with ThreadPoolExecutor(parallel) as pool:
            list(pool.map(one, finished))
        alerts = {(a["account"]["bank"], a["account"]["account"], a["day"]): a for a in source["alerts"]}
        investigated = []
        for case in source["cases"]:
            alert = alerts[(case["account"]["bank"], case["account"]["account"], case["day"])]
            investigated.append({"alert": {**self.store.read_artifact(alert["alert_id"], "rule_alert"), "alert_id": alert["alert_id"]},
                                 "run_id": case["run_id"], "worker_exit": case.get("worker_exit", 0)})
        self.output = ROOT / ".runtime/investigator-rounds" / f"{out}.json"
        self.record = {"round": out, "split": source["split"], "rule_version": source.get("rule_version"), "phases": [],
                       "created": time.time(), "adjudicated_from": name, "alerts": source["alerts"],
                       "labels_given_to_investigators": False}
        graded = self.grade(investigated)
        self.checkpoint("graded", cases=graded, summary=round_summary(graded), status="completed")
        return self.record

    def regrade_round(self, name: str) -> dict:
        """Re-score a stored round's cases under the current evaluation (no new investigation or adjudication)."""
        path = ROOT / ".runtime/investigator-rounds" / f"{name}.json"
        self.output, self.record = path, json.loads(path.read_text())
        alerts = {(a["account"]["bank"], a["account"]["account"], a["day"]): a for a in self.record["alerts"]}
        investigated = []
        for case in self.record.get("cases", []):
            alert = alerts[(case["account"]["bank"], case["account"]["account"], case["day"])]
            investigated.append({"alert": {**self.store.read_artifact(alert["alert_id"], "rule_alert"), "alert_id": alert["alert_id"]},
                                 "run_id": case["run_id"], "worker_exit": case.get("worker_exit", 0)})
        graded = self.grade(investigated)
        self.checkpoint("regraded", cases=graded, summary=round_summary(graded))
        return self.record

    def retry_round(self, name: str, parallel: int) -> dict:
        """Re-investigate a round's alerts whose case did not finish (infrastructure), then re-grade the round."""
        self.output = ROOT / ".runtime/investigator-rounds" / f"{name}.json"
        self.record = json.loads(self.output.read_text())
        failed = {c["run_id"] for c in self.record["cases"] if c["status"] not in ("completed", "insufficient_evidence")}
        alerts = [{**self.store.read_artifact(a["alert_id"], "rule_alert"), "alert_id": a["alert_id"]}
                  for a, c in zip(self.record["alerts"], self.record["cases"]) if c["run_id"] in failed]
        self.say(json.dumps({"retrying": len(alerts), "round": name}))
        retried = {(r["account"]["bank"], r["account"]["account"], r["day"]): r for r in self.grade(self.investigate(alerts, parallel))}
        cases = [retried.get((c["account"]["bank"], c["account"]["account"], c["day"]), c) if c["run_id"] in failed else c
                 for c in self.record["cases"]]
        self.checkpoint("retried", cases=cases, summary=round_summary(cases), retried=len(alerts), status="completed")
        return self.record

    def tiers(self, version: str, rows: list[dict] | None = None) -> dict:
        """Layer 2: fit risk tiers on the active rule's development alerts; measure them per partition."""
        from .tiering import Tiering

        body = load_program(self.store, version)
        program = program_of(body)
        if rows is None:
            rows, _ = self.learner.sample("development", SETTINGS["development_negative_ratio"])
        tiering = Tiering(self.learner, log=self.say)
        model = tiering.fit(program, rows)
        model["cutoffs"] = tiering.cutoffs(program, model)
        metrics = {split: tiering.metrics(program, model, split) for split in ("development", "validation")}
        self.say(json.dumps({"tiers_validation": metrics["validation"]}, default=str))
        model_id = self.store.artifact("risk_tier_model", {"rule_version": version, "model": model, "metrics": metrics,
                                                           "created": time.time()})
        return {"model_id": model_id, "rule_version": version, "cutoffs": model["cutoffs"], "metrics": metrics,
                "importance": model["importance"][:8]}

    def _run(self, cases: int, parallel: int, fresh: bool) -> dict:
        self.checkpoint("start", status="running", fresh=fresh)
        reference = self.reference_version()
        current = self.store.program_pointer(ALIAS)
        if current is None or fresh:
            # A fresh cycle re-learns the initial rule from development labels and competes
            # against the reference rule, whatever version the alias held before.
            initial = self.learn({"features": list(FEATURES), **SETTINGS["initial_tree"]}, reference)
            evaluation = self.compare(initial, reference)
            self.checkpoint("initial_rule_learned", initial_candidate=initial["candidate_version"],
                            initial_program=initial["program"].model_dump(mode="json"), initial_group_stats=initial["stats"], initial_tree=initial["tree"],
                            initial_explanation=initial["explanation"], initial_evaluation=evaluation, decision=evaluation["decision"])
            published = self.learner.publish(initial["candidate_version"], evaluation, ALIAS)
            if not published["published"]:
                self.store.set_program_pointer(ALIAS, reference, expected=self.store.program_pointer(ALIAS))
            current = self.store.program_pointer(ALIAS)
            self.checkpoint("initial_rule_registered", active_version=current, published=published["published"])
        body = load_program(self.store, current)
        scan = self.learner.chain_metrics(program_of(body), "development")
        alerts = self.alerts(current, cases)
        self.checkpoint("alerts_from_full_scan", development_scan=scan, alerts=[{k: a[k] for k in ("alert_id", "account", "day", "triggered_rule_ids")} for a in alerts],
                        alert_count=len(alerts))
        investigated = self.investigate(alerts, parallel)
        graded = self.grade(investigated)
        self.checkpoint("investigations_graded", cases=graded, active_version=current)
        return self.update_and_monitor(current, graded)

    def update_and_monitor(self, current: str, graded: list[dict]) -> dict:
        usable = [c for c in graded if c["usable"]]
        body = load_program(self.store, current)
        program = program_of(body)
        rules = {r.rule_id: r for r in program.rules}
        stats = {g["group_id"]: g for g in body.get("group_stats") or []}
        # Compact text throughout: the agent needs the rules, their population effect and how the
        # investigated alerts of each rule turned out, not the raw JSON of the program.
        per_group: dict[str, dict] = {}
        for case in usable:
            for group in program.groups:
                if set(group.rule_ids) <= set(case["triggered_rule_ids"]):
                    cell = per_group.setdefault(group.group_id, {"cases": 0, "laundering": 0, "team_suspicious": 0})
                    cell["cases"] += 1
                    cell["laundering"] += case["window_label"]
                    cell["team_suspicious"] += case["conclusion"] == "suspicious"
        context = {
            "objective": f"Keep chain recall at or above {SETTINGS['chain_recall_target']:.0%} and alert fewer account-days",
            "current_fit": body.get("fit"),
            "current_plan": {k: v for k, v in (body.get("plan") or {}).items() if k != "rationale"},
            "current_rules": [
                f"{g.group_id}: " + " AND ".join(f"{rules[r].feature_id} {'>=' if rules[r].operator == 'ge' else '<='} {rules[r].threshold:g}" for r in g.rule_ids)
                + (f" | est. precision {stats[g.group_id]['estimated_population_precision']:.3f}, ~{stats[g.group_id]['estimated_population_windows']} account-days"
                   if g.group_id in stats else "")
                for g in program.groups
            ],
            "registered_features": DESCRIPTIONS,
            "investigations_by_rule": per_group,
            "investigated_cases": len(usable),
            "feature_contrast": feature_contrast(usable),
            "note": "Window labels are development ground truth for the investigated alerts; they show which rule hits were false positives. "
                    "feature_contrast gives the median of each feature over investigated true-positive and false-positive windows.",
        }
        previous = body.get("plan") if "max_leaf_nodes" in (body.get("plan") or {}) else {"features": list(FEATURES), **SETTINGS["initial_tree"]}
        fallback = {**previous, "rationale": "Fallback: current tree configuration reused; agent plan unavailable"}
        plan, plan_source = self.learner.plan(context, fallback)
        # The agent's inputs are kept with its plan so the update can be explained from the data it saw.
        self.checkpoint("rule_learner_plan", plan=plan, plan_source=plan_source,
                        plan_inputs={k: context[k] for k in ("objective", "investigated_cases", "investigations_by_rule", "feature_contrast", "current_rules")})
        # Path C: the same agent proposes new risk features from investigation evidence (recorded; materialised later).
        try:
            from .feature_proposals import propose, record
            from .investigator_training import evidence_table

            table = evidence_table(self.store, usable)
            if table:
                proposal = propose(self.learner.provider, table, list(FEATURES))
                self.checkpoint("feature_proposals", feature_proposals=proposal, feature_proposal_id=record(self.store, proposal))
        except Exception as error:  # proposals are advisory; the rule update never depends on them
            self.say(json.dumps({"feature_proposal_error": str(error)[:200]}))
        extra = [{"bank": c["account"]["bank"], "account": c["account"]["account"], "day": c["day"], "label": c["window_label"], **c["features"]}
                 for c in graded]
        updated = self.learn(plan, current, extra, trigger="rule_update_agent" if plan_source == "agent" else "rule_update_fallback")
        evaluation = self.compare(updated, current)
        published = self.learner.publish(updated["candidate_version"], evaluation, ALIAS)
        self.checkpoint("rule_update_evaluated", candidate_program=updated["program"].model_dump(mode="json"),
                        candidate_group_stats=updated["stats"], candidate_tree=updated["tree"], candidate_explanation=updated["explanation"],
                        update_evaluation=evaluation, publication=published, decision=evaluation["decision"])
        monitoring = self.monitor(self.store.program_pointer(ALIAS))
        self.checkpoint("monitor_checked", monitoring=monitoring, status="completed")
        self.record["duration_seconds"] = round(time.time() - self.record["created"], 1)
        self.record["result_path"] = str(self.output)
        self.output.write_text(json.dumps(self.record, indent=2, default=str))
        self.store.artifact("learning_cycle", self.record)
        return self.record
