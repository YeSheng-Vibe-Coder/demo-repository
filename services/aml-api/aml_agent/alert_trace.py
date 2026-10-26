"""Alert tracing view of the validation period: headline volumes, the investigated alerts and, per alert, the hit
rules, the network and a step-by-step replay of the team (planner, investigators, merge, senior investigator).

Built from stored runs only. Labels appear only in the headline volumes and in `evaluation` (after the fact).
"""

from __future__ import annotations

import json

from .dashboard import condition_text
from .evidence import transaction_ledger
from .rule_learning import atom_true, value
from .rule_program import load_program, program_of
from .settings import ROOT
from .store import Store

TEAM_COLORS = ["#2563EB", "#7C3AED", "#0E9F8E", "#C27C0E"]

ROUNDS = ("val-r2", "val-r3-adj")
SHOWCASE = ROOT / ".runtime/showcase"


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


def account_verdicts(focus: str, edges: list[dict], members: list[dict], conclusion: dict, chain: list[str]) -> list[dict]:
    """Per-account outcome from the aggregator's confirmed/excluded transactions and specialists' suspicions."""
    decisions = {d["transaction_id"]: d for d in conclusion.get("candidate_decisions", [])}
    confirmed = set(conclusion.get("suspect_transaction_ids", [])) | {i for i, d in decisions.items() if d["decision"] == "confirm"}
    excluded = {i for i, d in decisions.items() if d["decision"] == "exclude"}
    chain_text = " ".join(chain)
    accounts: dict[str, dict] = {}
    for edge in edges:
        for side, account in (("payer", edge["from"]), ("payee", edge["to"])):
            item = accounts.setdefault(account, {"account": account, "confirmed": [], "excluded": [], "flagged_by": set(),
                                                 "found_by": set(), "tools": set(), "transactions": 0})
            item["transactions"] += 1
            item["found_by"].update(edge["seen_by"])
            if edge["id"] in confirmed:
                item["confirmed"].append(edge["id"])
            if edge["id"] in excluded:
                item["excluded"].append(edge["id"])
            item["flagged_by"].update(edge["suspected_by"])
    for member in members:
        for action in member["actions"]:
            refs = json.dumps(action["arguments"])
            for account, item in accounts.items():
                if account.split(":")[1] in refs:
                    item["tools"].add(action["tool"])
    case_suspicious = conclusion.get("conclusion") == "suspicious"
    result = []
    for account, item in accounts.items():
        bank, number = account.split(":")
        in_chain = number in chain_text
        if account == focus:
            verdict = "suspicious" if case_suspicious else ("cleared" if conclusion.get("conclusion") == "not_supported" else "unresolved")
        elif item["confirmed"] or (in_chain and case_suspicious):
            verdict = "suspicious"
        elif item["flagged_by"]:
            verdict = "flagged"
        elif item["excluded"]:
            verdict = "cleared"
        else:
            verdict = "observed"
        result.append({**item, "role": None, "verdict": verdict, "in_chain": in_chain,
                       "flagged_by": sorted(item["flagged_by"]), "found_by": sorted(item["found_by"]), "tools": sorted(item["tools"])})
    order = {"suspicious": 0, "flagged": 1, "unresolved": 2, "cleared": 3, "observed": 4}
    return sorted(result, key=lambda a: (a["account"] != focus, order[a["verdict"]], -a["transactions"]))


def case_view(store: Store, run_id: str, program) -> dict:
    """A team case as a network: edges with who found them and the merged verdict, accounts with their outcome."""
    state = store.get(run_id)
    team = state.get("team") or {}
    children = [store.get(child) for child in team.get("children", [])]
    is_team = bool(children)
    if not children:  # Runs from before team mode are shown as a one-member team.
        children = [state]
        team = {"assignments": [{"role": "investigator", "question": state["request"].get("investigation_question", ""),
                                 "tools": [], "accounts": []}], "plan_source": "single"}
    prefetch = [o for o in state["observations"] if o["tool"] == "get_alert_context"]
    prefetch_refs = {o["evidence_ref"] for o in prefetch}
    ref_roles: dict[str, set] = {ref: {"预取"} for ref in prefetch_refs}
    members = []
    for index, (assignment, child) in enumerate(zip(team["assignments"], children)):
        role = assignment["role"]
        actions = []
        for observation in child["observations"]:
            if observation["evidence_ref"] in prefetch_refs:
                continue
            ref_roles.setdefault(observation["evidence_ref"], set()).add(role)
            actions.append({
                "tool": observation["tool"],
                "arguments": {k: v for k, v in (observation.get("arguments") or {}).items() if k != "expected_observation"},
                "digest": observation.get("digest", []),
            })
        conclusion = child.get("conclusion") or {}
        members.append({
            "role": role,
            "color": TEAM_COLORS[index % len(TEAM_COLORS)],
            "question": assignment.get("question"),
            "tools": assignment.get("tools", []),
            "accounts": assignment.get("accounts", []),
            "status": child["status"],
            "conclusion": conclusion.get("conclusion"),
            "brief_reason": conclusion.get("brief_reason"),
            "suspects": conclusion.get("suspect_transaction_ids", []),
            "actions": actions,
            "model_calls": len(child.get("model_calls", [])),
            "sql": child["budget"]["sql_requests"],
        })
    evidence = dict(state["evidence"])
    for child in children:
        evidence.update(child["evidence"])
    ledger = transaction_ledger({**state, "evidence": evidence})
    context, profiles = {}, {}
    for item in evidence.values():
        context.update(item.get("transaction_context") or {})
        for ref, days in (item.get("account_context") or {}).items():
            profiles.setdefault(ref, {}).update(days)
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
            "id": row["transaction_id"],
            "from": row["from_account_ref"],
            "to": row["to_account_ref"],
            "amount": row["amount_paid"],
            "currency": row["payment_currency"],
            "time": row["time"],
            "flags": context.get(row["transaction_id"], {}),
            "seen_by": sorted({r for ref in row["evidence_refs"] for r in ref_roles.get(ref, set())}),
            "suspected_by": suspected.get(row["transaction_id"], []),
            "verdict": verdicts.get(row["transaction_id"]),
        }
        for row in ledger["transactions"][:150]
    ]
    request = state["request"]
    focus = f"{request['account']['bank']}:{request['account']['account']}"
    roles = classify(edges, focus)
    nodes = [
        {"id": ref, "role": role, "profile": (sorted(profiles.get(ref, {}).items()) or [(None, {})])[-1][1]}
        for ref, role in roles.items()
    ]
    accounts = account_verdicts(focus, edges, members, conclusion, conclusion.get("chain", []))
    for account in accounts:
        account["role"] = roles.get(account["account"], "peer")
    features = (state.get("rule_context") or {}).get("alert", {}).get("features") or {}
    return {
        "run_id": run_id,
        "account": focus,
        "day": request["start"][:10],
        "window": {"start": request["start"], "as_of": request["as_of"]},
        "status": state["status"],
        "features": features,
        "rule_hits": rule_hits(program, features) if features else [],
        "team": members,
        "nodes": nodes,
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


def replay_steps(store: Store, state: dict, view: dict) -> list[dict]:
    """Every investigator action in time order, with the reasoning that led to it and the transactions it surfaced."""
    team = state.get("team") or {}
    prefetch = {o["evidence_ref"] for o in state["observations"] if o["tool"] == "get_alert_context"}
    first_seen: dict[str, tuple[int, int]] = {}
    steps = []
    for index, child_id in enumerate(team.get("children", [])):
        child = store.get(child_id)
        actions = [d for d in child.get("decisions", []) if d.get("decision") == "action"]
        observations = [o for o in child["observations"] if o["evidence_ref"] not in prefetch]
        times = [t for tool, t in step_times(store, child_id) if tool != "get_alert_context"]
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
    # Each transaction is drawn at the step whose evidence first contained it.
    ref_step = {s["evidence_ref"]: s for s in steps}
    evidence = dict(state["evidence"])
    for child_id in team.get("children", []):
        evidence.update(store.get(child_id)["evidence"])
    from .evidence import transaction_ledger

    for row in transaction_ledger({**state, "evidence": evidence})["transactions"]:
        owners = [ref_step[r] for r in row["evidence_refs"] if r in ref_step]
        if owners:
            first = min(owners, key=lambda s: s["step"])
            first_seen[row["transaction_id"]] = (first["step"], first["member"])
    for edge in view["edges"]:
        step, member = first_seen.get(edge["id"], (0, None))
        edge["step"], edge["member"] = step, member
    for step in steps:
        step["transactions"] = [e["id"] for e in view["edges"] if e["step"] == step["step"]]
    return steps


def case_row(store: Store, round_name: str, case: dict, policy: dict) -> dict:
    from .showcase import tier_of

    state = store.get(case["run_id"])
    program = program_of(load_program(store, state["request"]["rule_version"]))
    view = case_view(store, case["run_id"], program)
    steps = replay_steps(store, state, view)
    adjudication = state.get("adjudication") or {}
    hits = [h for h in view["rule_hits"] if h["hit"]]
    chain_accounts = [a["account"] for a in view["accounts"] if a["verdict"] == "suspicious"]
    return {
        "key": case["run_id"], "investigated": True, "round": round_name, "account": view["account"], "day": view["day"],
        "rules": [h["group"] for h in hits],
        "tier": tier_of(policy, case["features"]) if policy else {"tier": case.get("tier")},
        "decision": adjudication.get("decision"), "risk_level": adjudication.get("risk_level"),
        "chain_accounts": len(chain_accounts),
        "detail": {
            "hit_rules": hits,
            "team_plan": (state.get("team") or {}).get("rationale"),
            "members": [{k: m[k] for k in ("role", "color", "question", "tools", "accounts", "status", "conclusion", "brief_reason", "suspects")}
                        for m in view["team"]],
            "steps": steps,
            "nodes": view["nodes"], "edges": view["edges"], "accounts": view["accounts"],
            "merge": view["aggregator"],
            "senior": {k: adjudication.get(k) for k in ("prior_level", "adjustment", "risk_level", "decision", "rationale",
                                                         "evidence_flags", "key_evidence", "account_risks", "linked_alerts")},
            "model_calls": view["model_calls"], "sql_requests": view["sql_requests"],
        },
        # Evaluation only (labels, after the fact): chain accounts the rule never alerted on.
        "evaluation": {"new_chain_accounts": case.get("new_chain_accounts_found", []),
                       "chain_accounts_found": case.get("chain_accounts_found", []), "focus_in_chain": case.get("focus_in_chain")},
    }


def run_detail(store: Store, run_id: str) -> dict:
    """A (possibly unfinished) investigation in the shape of a case's detail, for the live view."""
    state = store.get(run_id)
    program = program_of(load_program(store, state["request"]["rule_version"]))
    view = case_view(store, run_id, program)
    adjudication = state.get("adjudication") or {}
    return {
        "team_plan": (state.get("team") or {}).get("rationale"),
        "members": [{k: m[k] for k in ("role", "color", "question", "tools", "accounts", "status", "conclusion", "brief_reason", "suspects")}
                    for m in view["team"]] if state.get("team") else [],
        "steps": replay_steps(store, state, view) if state.get("team") else [],
        "nodes": view["nodes"], "edges": view["edges"], "accounts": view["accounts"],
        "merge": view["aggregator"] if state.get("conclusion") else {},
        "senior": {k: adjudication.get(k) for k in ("prior_level", "adjustment", "risk_level", "decision", "rationale",
                                                     "evidence_flags", "key_evidence", "account_risks", "linked_alerts")} if adjudication else {},
        "model_calls": view["model_calls"], "sql_requests": view["sql_requests"],
    }


def live_case(store: Store, run_id: str, policy: dict) -> dict | None:
    """A dashboard-started investigation as a case, built exactly like the recorded rounds (case_row); None until decided."""
    state = store.get(run_id)
    if not (state.get("adjudication") or {}).get("decision"):
        return None
    return case_row(store, "live", {"run_id": run_id, "status": state["status"], "features": state["rule_context"]["alert"]["features"]}, policy)


def alert_rows(store: Store, policy: dict, investigated: set) -> list[dict]:
    """Uninvestigated validation alerts, highest priority per tier (exported by showcase-metrics): rules and grading only."""
    from .showcase import tier_of

    path = SHOWCASE / "alerts.json"
    if not path.exists():
        return []
    body = json.loads(path.read_text())
    program = program_of(load_program(store, body["rule_version"]))
    rows = []
    for alert in body["alerts"]:
        account = f"{alert['bank']}:{alert['account']}"
        if (account, alert["day"]) in investigated:
            continue
        hits = [h for h in rule_hits(program, alert["features"]) if h["hit"]]
        rows.append({"key": f"alert:{account}:{alert['day']}", "investigated": False, "account": account, "day": alert["day"],
                     "priority": alert["priority"], "rules": [h["group"] for h in hits],
                     "tier": tier_of(policy, alert["features"]) if policy else {"tier": alert["tier"]},
                     "decision": None, "chain_accounts": None, "detail": {"hit_rules": hits},
                     "evaluation": {"new_chain_accounts": []}})
    return rows


def view(store: Store) -> dict:
    from .live import primary_policy

    policy = primary_policy(store)
    volume = json.loads((SHOWCASE / "validation_volume.json").read_text()) if (SHOWCASE / "validation_volume.json").exists() else {}
    versions = json.loads((SHOWCASE / "versions.json").read_text()) if (SHOWCASE / "versions.json").exists() else {}
    active = store.program_pointer("learned-current")
    current = next(({"label": label, **m} for label, m in versions.items() if m.get("id") == active), {})
    cases, seen = [], set()
    for name in ROUNDS:
        path = ROOT / ".runtime/investigator-rounds" / f"{name}.json"
        if not path.exists():
            continue
        for case in json.loads(path.read_text()).get("cases", []):
            if case["run_id"] in seen or case["status"] not in ("completed", "insufficient_evidence"):
                continue
            seen.add(case["run_id"])
            cases.append(case_row(store, name, case, policy))
    # Live investigations from the dashboard count once the senior investigator has decided.
    from .live import RUNS

    for runs in (json.loads(RUNS.read_text()) if RUNS.exists() else {}).values():
        if runs[-1] in seen:
            continue
        seen.add(runs[-1])
        case = live_case(store, runs[-1], policy)
        if case:
            cases.append(case)
    return {
        "validation": {**volume, "rule_version": current.get("label"), "alerts_per_day": current.get("flagged_per_day"),
                       "alerted_accounts": current.get("flagged_accounts"), "chain_recall": current.get("chain_recall"),
                       "chain_precision": current.get("chain_precision")},
        "cases": sorted(cases, key=lambda c: (c["day"], c["account"])),
        "alerts": alert_rows(store, policy, {(c["account"], c["day"]) for c in cases}),
    }
