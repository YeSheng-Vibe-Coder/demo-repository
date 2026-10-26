"""Alert tracing view of the validation period: headline volumes, the investigated alerts and, per alert, the hit
rules, the network and a step-by-step replay of the team (planner, investigators, merge, senior investigator).

Built from stored runs only. Labels appear only in the headline volumes and in `evaluation` (after the fact).
"""

from __future__ import annotations

import json

from .contracts import account_ref
from .dashboard import condition_text
from .evidence import transaction_ledger
from .rule_learning import atom_true, value
from .rule_program import load_program, program_of
from .settings import ROUNDS as ROUND_FILES, SHOWCASE
from .store import RULES_POINTER, Store

# Investigation rounds whose validation cases the alert tracing page shows.
ROUNDS = ("val-r2", "val-r3-adj")
MEMBER_FIELDS = ("role", "question", "conclusion", "brief_reason")
SENIOR_FIELDS = ("prior_level", "adjustment", "risk_level", "decision", "rationale", "evidence_flags", "key_evidence",
                 "account_risks", "linked_alerts")


def classify(edges: list[dict], focus: str) -> dict[str, str]:
    """Direction of each account relative to the focus, by time-agnostic reachability."""
    roles = {focus: "focus"}
    for _ in range(4):
        for edge in edges:
            source, target = edge["from"], edge["to"]
            if roles.get(target) in ("focus", "upstream") and source not in roles:
                roles[source] = "upstream"
            if roles.get(source) in ("focus", "downstream") and target not in roles:
                roles[target] = "downstream"
    for edge in edges:
        for account in (edge["from"], edge["to"]):
            roles.setdefault(account, "peer")
    return roles


def rule_hits(program, features: dict) -> list[dict]:
    """Every rule group with each atom's value, threshold and whether it held."""
    rules = {r.rule_id: r for r in program.rules}
    view = []
    for group in program.groups:
        atoms = []
        for rule_id in group.rule_ids:
            rule = rules[rule_id]
            atoms.append({"feature": rule.feature_id, "operator": rule.operator, "threshold": rule.threshold,
                          "value": features.get(rule.feature_id), "effective": value(features, rule.feature_id),
                          "hit": atom_true(rule, features), "text": condition_text(rule.model_dump())})
        view.append({"group": group.group_id, "hit": all(a["hit"] for a in atoms), "atoms": atoms})
    return view


def account_verdicts(focus: str, edges: list[dict], members: list[dict], conclusion: dict) -> list[dict]:
    """Per-account outcome of the case, from the team's own verdicts.

    suspicious / cleared: examined and decided; insufficient: examined (a tool queried it) without a conclusion;
    not_examined: seen in the transactions but never queried on its own. Before the team has concluded (a live run),
    the alerted account is unresolved and the others are flagged (a specialist suspects one of their transfers) or observed.
    """
    accounts: dict[str, dict] = {}
    for edge in edges:
        for account in (edge["from"], edge["to"]):
            item = accounts.setdefault(account, {"account": account, "flagged_by": set(), "found_by": set(), "tools": set(), "transactions": 0})
            item["transactions"] += 1
            item["found_by"].update(edge["seen_by"])
            item["flagged_by"].update(edge["suspected_by"])
    for member in members:
        for action in member["actions"]:
            refs = json.dumps(action["arguments"])
            for account, item in accounts.items():
                if account.split(":")[1] in refs:
                    item["tools"].add(action["tool"])
    judged = {v["account_ref"]: v for v in conclusion.get("account_verdicts", [])}
    result = []
    for account, item in accounts.items():
        if judged:
            verdict = {"suspicious": "suspicious", "cleared": "cleared"}.get((judged.get(account) or {}).get("verdict")) \
                or ("insufficient" if account in judged and item["tools"] else "not_examined")
        else:
            verdict = "unresolved" if account == focus else "flagged" if item["flagged_by"] else "observed"
        result.append({**item, "role": None, "verdict": verdict, "reason": (judged.get(account) or {}).get("reason"),
                       "evidence_refs": (judged.get(account) or {}).get("evidence_refs", []),
                       "flagged_by": sorted(item["flagged_by"]), "found_by": sorted(item["found_by"]), "tools": sorted(item["tools"])})
    order = {"suspicious": 0, "flagged": 1, "unresolved": 2, "insufficient": 2, "cleared": 3, "not_examined": 4, "observed": 4}
    return sorted(result, key=lambda a: (a["account"] != focus, order[a["verdict"]], -a["transactions"]))


def case_view(store: Store, state: dict, program) -> dict:
    """A team case as a network and a replay: edges with who found them and the merged verdict, accounts with their
    outcome, and every investigator action in time order."""
    team = state.get("team") or {}
    children = [store.get(child) for child in team.get("children", [])]
    is_team = bool(children)
    if not children:  # Runs from before team mode are shown as a one-member team.
        children = [state]
        team = {"assignments": [{"role": "investigator", "question": state["request"].get("investigation_question", "")}]}
    prefetch_refs = {o["evidence_ref"] for o in state["observations"] if o["tool"] == "get_alert_context"}
    ref_roles: dict[str, set] = {ref: {"预取"} for ref in prefetch_refs}
    members = []
    for assignment, child in zip(team["assignments"], children):
        actions = []
        for observation in child["observations"]:
            if observation["evidence_ref"] in prefetch_refs:
                continue
            ref_roles.setdefault(observation["evidence_ref"], set()).add(assignment["role"])
            actions.append({"tool": observation["tool"],
                            "arguments": {k: v for k, v in (observation.get("arguments") or {}).items() if k != "expected_observation"}})
        conclusion = child.get("conclusion") or {}
        members.append({"role": assignment["role"], "question": assignment.get("question"), "conclusion": conclusion.get("conclusion"),
                        "brief_reason": conclusion.get("brief_reason"), "suspects": conclusion.get("suspect_transaction_ids", []),
                        "actions": actions, "model_calls": len(child.get("model_calls", [])), "sql": child["budget"]["sql_requests"]})
    evidence = dict(state["evidence"])
    for child in children:
        evidence.update(child["evidence"])
    ledger = transaction_ledger({**state, "evidence": evidence})["transactions"]
    context = {}
    for item in evidence.values():
        context.update(item.get("transaction_context") or {})
    conclusion = state.get("conclusion") or {}
    verdicts = {d["transaction_id"]: d["decision"] for d in conclusion.get("candidate_decisions", [])}
    for identifier in conclusion.get("suspect_transaction_ids", []):
        verdicts.setdefault(identifier, "confirm")
    suspected = {}
    for member in members:
        for identifier in member["suspects"]:
            suspected.setdefault(identifier, []).append(member["role"])
    edges = [
        {
            "id": row["transaction_id"], "from": row["from_account_ref"], "to": row["to_account_ref"],
            "amount": row["amount_paid"], "currency": row["payment_currency"], "time": row["time"],
            "flags": context.get(row["transaction_id"], {}),
            "seen_by": sorted({r for ref in row["evidence_refs"] for r in ref_roles.get(ref, set())}),
            "suspected_by": suspected.get(row["transaction_id"], []),
            "verdict": verdicts.get(row["transaction_id"]),
        }
        for row in ledger[:150]
    ]
    focus = account_ref(state["request"]["account"])
    roles = classify(edges, focus)
    accounts = account_verdicts(focus, edges, members, conclusion)
    for account in accounts:
        account["role"] = roles.get(account["account"], "peer")
    features = (state.get("rule_context") or {}).get("alert", {}).get("features") or {}
    return {
        "account": focus,
        "day": state["request"]["start"][:10],
        "rule_hits": rule_hits(program, features) if features else [],
        "team": members,
        "steps": replay_steps(store, team, children, prefetch_refs, ledger, edges) if is_team else [],
        "nodes": [{"id": ref, "role": role} for ref, role in roles.items()],
        "edges": edges,
        "accounts": accounts,
        "sql_requests": state["budget"]["sql_requests"] + (sum(m["sql"] for m in members) if is_team else 0),
        "model_calls": len(state.get("model_calls", [])) + (sum(m["model_calls"] for m in members) if is_team else 0),
        "aggregator": {k: conclusion.get(k) for k in (
            "conclusion", "brief_reason", "supported_findings", "counterevidence", "unresolved_questions", "stop_reason", "chain",
            "candidate_decisions", "suspect_transaction_ids")},
    }


def step_times(store: Store, run_id: str) -> list[tuple[str, float]]:
    """(tool, finish time) of every tool call of a run, in order."""
    return [(e["attributes"].get("tool"), e["at"]) for e in store.trace_events(run_id) if e["stage"] == "tool_finished"]


def replay_steps(store: Store, team: dict, children: list[dict], prefetch: set, ledger: list[dict], edges: list[dict]) -> list[dict]:
    """Every investigator action in time order, with the reasoning that led to it and the transactions it surfaced.

    Also marks each edge with the step (and member) whose evidence first contained it; step 0 is the prefetched context.
    """
    steps = []
    for index, child in enumerate(children):
        actions = [d for d in child.get("decisions", []) if d.get("decision") == "action"]
        observations = [o for o in child["observations"] if o["evidence_ref"] not in prefetch]
        times = [t for tool, t in step_times(store, child["id"]) if tool != "get_alert_context"]
        # Prefetched context is copied in without an action; the remaining observations follow the actions in order.
        offset = max(0, len(observations) - len(actions))
        for number, observation in enumerate(observations):
            decision = actions[number - offset] if number >= offset else None
            steps.append({
                "member": index, "role": team["assignments"][index]["role"], "tool": observation["tool"],
                "arguments": {k: v for k, v in (observation.get("arguments") or {}).items() if k != "expected_observation"},
                "reasoning": (decision or {}).get("brief_reason"),
                "expected": ((decision or {}).get("next_action") or {}).get("expected_observation"),
                "found": observation.get("digest", [])[:6],
                "evidence_ref": observation["evidence_ref"],
                "at": times[number] if number < len(times) else None,
            })
    steps.sort(key=lambda s: (s["at"] is None, s["at"] or 0))
    for order, step in enumerate(steps, 1):
        step["step"] = order
    ref_step = {s["evidence_ref"]: s for s in steps}
    first_seen: dict[str, tuple[int, int]] = {}
    for row in ledger:
        owners = [ref_step[r] for r in row["evidence_refs"] if r in ref_step]
        if owners:
            first = min(owners, key=lambda s: s["step"])
            first_seen[row["transaction_id"]] = (first["step"], first["member"])
    for edge in edges:
        edge["step"], edge["member"] = first_seen.get(edge["id"], (0, None))
    for step in steps:
        step["transactions"] = [e["id"] for e in edges if e["step"] == step["step"]]
    return steps


def case_row(store: Store, round_name: str, case: dict, policy: dict) -> dict:
    """One investigated alert for the list and its drawer; recorded rounds and decided live runs alike."""
    from .showcase import tier_of

    state = store.get(case["run_id"])
    detail = run_detail(store, state)
    adjudication = state.get("adjudication") or {}
    return {
        "key": case["run_id"], "round": round_name, "account": detail.pop("account"), "day": detail.pop("day"),
        "rules": [h["group"] for h in detail["hit_rules"]],
        "tier": tier_of(policy, case["features"]) if policy else {"tier": case.get("tier")},
        "decision": adjudication.get("decision"),
        "chain_accounts": sum(a["verdict"] == "suspicious" for a in detail["accounts"]),
        "detail": detail,
        # Evaluation only (labels, after the fact): chain accounts the rule never alerted on.
        "evaluation": {"new_chain_accounts": case.get("new_chain_accounts_found", [])},
    }


def run_detail(store: Store, state: dict) -> dict:
    """A case's detail (possibly unfinished, for the live view): hit rules, team, replay, network, verdicts."""
    view = case_view(store, state, program_of(load_program(store, state["request"]["rule_version"])))
    adjudication = state.get("adjudication") or {}
    return {
        "account": view["account"], "day": view["day"],
        "hit_rules": [h for h in view["rule_hits"] if h["hit"]],
        "team_plan": (state.get("team") or {}).get("rationale"),
        "members": [{k: m[k] for k in MEMBER_FIELDS} for m in view["team"]] if state.get("team") else [],
        "steps": view["steps"],
        "nodes": view["nodes"], "edges": view["edges"], "accounts": view["accounts"],
        "merge": view["aggregator"] if state.get("conclusion") else {},
        "senior": {k: adjudication.get(k) for k in SENIOR_FIELDS} if adjudication else {},
        "model_calls": view["model_calls"], "sql_requests": view["sql_requests"],
    }


def live_case(store: Store, run_id: str, policy: dict, graded: dict | None = None) -> dict | None:
    """A dashboard-started investigation as a case, built exactly like the recorded rounds (case_row); None until decided.

    `graded` is the evaluation-side grading of the run (LearningCycle.grade, as for the rounds), when available.
    """
    state = store.get(run_id)
    if not (state.get("adjudication") or {}).get("decision"):
        return None
    case = {"run_id": run_id, "status": state["status"], "features": state["rule_context"]["alert"]["features"]}
    return case_row(store, "live", {**case, **(graded or {})}, policy)


def grade_live(store: Store, run_ids: list[str]) -> dict[str, dict]:
    """Grade decided live runs with the same evaluation as the investigation rounds (labels, after the fact)."""
    if not run_ids:
        return {}
    from .cli import api_token
    from .learning_cycle import LearningCycle
    from .settings import Settings

    cycle = LearningCycle(Settings.load(), api_token())
    items = []
    for run_id in run_ids:
        alert_id = store.get(run_id)["request"]["alert_id"]
        items.append({"alert": cycle.alert(alert_id), "run_id": run_id, "worker_exit": 0})
    return {g["run_id"]: g for g in cycle.grade(items)}


def view(store: Store) -> dict:
    from .live import load_runs, primary_policy
    from .showcase import version_metrics

    policy = primary_policy(store)
    volume = json.loads((SHOWCASE / "validation_volume.json").read_text()) if (SHOWCASE / "validation_volume.json").exists() else {}
    active = store.program_pointer(RULES_POINTER)
    current = next(({"label": label, **m} for label, m in version_metrics().items() if m.get("id") == active), {})
    cases, seen = [], set()
    for name in ROUNDS:
        path = ROUND_FILES / f"{name}.json"
        if not path.exists():
            continue
        for case in json.loads(path.read_text()).get("cases", []):
            if case["run_id"] in seen or case["status"] not in ("completed", "insufficient_evidence"):
                continue
            seen.add(case["run_id"])
            cases.append(case_row(store, name, case, policy))
    # Live investigations from the dashboard count once the senior investigator has decided.
    latest = [runs[-1] for runs in load_runs().values()
              if runs[-1] not in seen and (store.get(runs[-1]).get("adjudication") or {}).get("decision")]
    try:
        graded = grade_live(store, latest)
    except Exception:  # no data access here: the cases are still shown, without the label check
        graded = {}
    cases += [live_case(store, run_id, policy, graded.get(run_id)) for run_id in latest]
    return {
        "validation": {**volume, "rule_version": current.get("label"), "alerts_per_day": current.get("flagged_per_day"),
                       "chain_recall": current.get("chain_recall")},
        "cases": sorted(cases, key=lambda c: (c["day"], c["account"])),
    }
