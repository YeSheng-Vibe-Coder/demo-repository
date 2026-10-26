"""Domain tools resolve registered references, never model-provided SQL.

Each tool answers one investigation question. Aggregation and multi-hop tracing
run in Zetaris; paging, retries and provably empty scopes are handled by the host.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .contracts import Account, Action
from .feature_registry import EVIDENCE_SIDE
from .profile import EVIDENCE, TOOLS
from .zetaris import Zetaris

PAGE_SIZE = EVIDENCE["max_visible_transactions"]

CATALOG = {
    "get_alert_context": {
        "question": "Why was this alert raised and by which frozen rule version?",
        "arguments": {},
        "result": "Frozen rule, alert features and the Zetaris-fetched transactions and counterparties behind each triggered condition (prefetched by the host for rule alerts)",
    },
    "assess_risk": {
        "question": "How risky do these accounts look to the layer-2 tier model on the case day, and which features drive that?",
        "arguments": {"account_refs": "1..5 known references (the alerted account and/or counterparties)"},
        "result": "Per account: tier (high/mid/low), model score, and the features that raise or lower the score most "
                  "against the population median; accounts without activity that day have no score",
    },
    "summarize_account": {
        "question": "What are the payment scale, frequency and counterparty distribution of these accounts?",
        "arguments": {
            "account_refs": "1..5 known references",
            "window_ref": "current|history|lookback",
        },
        "result": "One Zetaris scan: per account, direction and currency exact counts, amounts, distinct counterparties, first/last time and the top counterparties; registers up to five new counterparties",
    },
    "query_account_transactions": {
        "arguments": {
            "account_ref": "known reference",
            "window_ref": "current|history|lookback",
            "direction": "incoming|outgoing|both",
        },
        "question": "Within the window, who paid this account and whom did it pay?",
        "result": "Up to 200 transactions with complete/partial status; repeating the identical query returns the next stored page without a new source call",
    },
    "compare_account_baseline": {
        "question": "How does the case day differ from the account's own history over a chosen period?",
        "arguments": {
            "account_ref": "known reference",
            "history_days": "7|14|30",
        },
        "result": "Zetaris feature-store baseline: mean, std, max and z-score of counts, counterparties, currencies and pass-through per day, plus the share of today's counterparties never seen in the history window; reliability flagged",
    },
    "query_between_accounts": {
        "question": "Are there direct or reverse transfers between two accounts?",
        "arguments": {
            "account_ref": "known reference",
            "other_account_ref": "known reference",
            "window_ref": "current|history|lookback",
        },
        "result": "Bounded direct and reverse transfers",
    },
    "check_structure": {
        "question": "Where did the funds come from or go, and do routes converge, disperse or return?",
        "arguments": {
            "account_ref": "known reference",
            "window_ref": "current|history|lookback",
            "direction": "upstream|downstream|through",
            "max_gap_hours": "6|24|48",
            "depth": "2..5",
        },
        "result": "Zetaris multi-hop fund tracing (same currency, strictly ordered, amount continuity, closest-amount beam): routes with transaction IDs, gaps, amount retained, convergence accounts and return-to-origin; registers key route accounts",
    },
    "propose_rule_change": {
        "question": "Does the evidence justify an unapproved rule-change hypothesis?",
        "arguments": {
            "base_version": "frozen rule version",
            "evidence_refs": "known IDs",
            "change_type": "threshold|feature|combination",
            "hypothesis": "testable claim",
            "changes": "typed payload",
        },
        "result": "Unapproved proposal; never changes active rules",
    },
}


def case_windows(request: dict) -> dict:
    start = datetime.fromisoformat(request["start"])
    end = datetime.fromisoformat(request["as_of"])
    earliest = max(datetime(2022, 8, 1), end - timedelta(days=7))
    return {
        "current": {"start": start.isoformat(), "end": end.isoformat()},
        "history": {
            "start": max(earliest, start - (end - start)).isoformat(),
            "end": start.isoformat(),
        },
        "lookback": {"start": earliest.isoformat(), "end": end.isoformat()},
    }


def covers(outer: dict | None, inner: dict) -> bool:
    return bool(outer) and outer["start"] <= inner["start"] and inner["end"] <= outer["end"]


def known_inactive(state: dict, reference: str, side: str, window: dict) -> str | None:
    """Evidence ref proving no `side` activity for the account over `window`, if any.

    Only complete source results whose window covers the requested one qualify;
    partial or narrower evidence never establishes absence.
    """
    for ref, evidence in state["evidence"].items():
        if not evidence.get("complete") or not covers(evidence.get("window"), window):
            continue
        profile = evidence.get("accounts", {}).get(reference)
        if isinstance(profile, dict) and side in profile and not profile[side]:
            return ref
        scope = evidence.get("query_scope") or {}
        if scope.get("account_ref") == reference and scope.get("direction") == "both" and not scope.get("other_account_ref"):
            endpoint = "to" if side == "incoming" else "from"
            if not any(f"{row[endpoint + '_bank']}:{row[endpoint + '_account']}" == reference for row in evidence.get("rows", [])):
                return ref
    return None


def note(state: dict, zh: str, en: str) -> None:
    """A progress line for the live view; saved with the next state write."""
    state.setdefault("setup_log", []).append({"at": time.time(), "zh": zh, "en": en})


class Tools:
    def __init__(self, zetaris: Zetaris):
        self.zetaris = zetaris

    def assess_risk(self, state: dict, references: list[str], reserve) -> dict:
        """Layer-2 scores and score drivers for accounts on the case day (population statistics only)."""
        from . import calibration
        from .tiering import TIERS, explain, load_model

        day = state["request"]["start"][:10]
        if datetime.fromisoformat(day) + timedelta(hours=24) > datetime.fromisoformat(state["request"]["as_of"]):
            return {"status": "insufficient_history", "complete": False, "reason": "Case day ends after the cutoff"}
        model = load_model((state.get("rule_context") or {}).get("tier", {}).get("tier_model"), state["request"].get("rule_version"))
        if model is None:
            return {"status": "insufficient_history", "complete": False, "reason": "No layer-2 model for this rule version"}
        from .rule_learning import triggered
        from .rule_program import RuleProgram

        program_body = (state.get("rule_context") or {}).get("program")
        program = RuleProgram.model_validate(program_body) if program_body else None
        rows = self.zetaris.risk_features(references, day, reserve)
        accounts = {}
        for reference in references:
            features = rows.get(reference)
            if features is None:
                accounts[reference] = {"active": False}
                continue
            hits = set(triggered(program, features)) if program else set()
            alerted = bool(program) and any(set(g.rule_ids) <= hits for g in program.groups)
            # The tier model is fitted on layer-1 alerts only; outside them its score is not calibrated.
            accounts[reference] = {"active": True, "layer1_alert": alerted,
                                   **(explain(model, features, calibration.load()) if alerted else {})}
        return {"day": day, "complete": True, "accounts": accounts, "tiers": list(TIERS),
                "basis": "Layer-2 tier model fitted on development alerts; scores are population statistics, not labels"}

    @staticmethod
    def register(state: dict, source_ref: str, references: list[str]) -> list[str]:
        """Register up to five new reachable accounts within depth and case limits."""
        depth = state.get("account_depths", {}).get(source_ref, 0) + 1
        added = []
        for reference in dict.fromkeys(references):
            if reference in state["accounts"]:
                continue
            if (
                len(added) >= TOOLS["max_new_accounts_per_action"]
                or len(state["accounts"]) >= TOOLS["max_accounts_per_case"]
                or depth > TOOLS["max_graph_depth"]
            ):
                break
            bank, account = reference.split(":", 1)
            state["accounts"][reference] = Account(bank=bank, account=account).model_dump()
            state.setdefault("account_depths", {})[reference] = depth
            added.append(reference)
        return added

    @staticmethod
    def case_days(state: dict) -> list[str]:
        start = datetime.fromisoformat(state["request"]["start"]).date()
        end = datetime.fromisoformat(state["request"]["as_of"])
        days, day = [], start
        while datetime.combine(day, datetime.min.time()) + timedelta(hours=24) <= end and len(days) < 7:
            days.append(day.isoformat())
            day += timedelta(days=1)
        return days or [start.isoformat()]

    def enrich(self, result: dict, state: dict, reserve, keep_rows: bool = True) -> dict:
        """Attach Zetaris-computed context to retrieved transactions.

        transaction_context: backward-looking structure per payment (nothing after it);
        account_context: feature-store profile of both endpoints for each transaction day,
        only for days that end at or before the case cutoff.
        """
        rows = result.get("rows") or []
        if not rows:
            return result if keep_rows else {}
        note(state, "计算每笔交易此前 48 小时的资金结构", "Computing each payment's prior 48h structure")
        transaction_context = self.zetaris.transaction_context(rows, reserve)
        note(state, "读取相关账户当天的特征画像", "Reading the involved accounts' feature profiles")
        context = {
            "transaction_context": transaction_context,
            "account_context": self.zetaris.account_features(
                [
                    (f"{row[side + '_bank']}:{row[side + '_account']}", row["transaction_time"][:10])
                    for row in rows[:200]
                    for side in ("from", "to")
                ],
                state["request"]["as_of"],
                reserve,
            ),
        }
        return {**result, **context} if keep_rows else context

    def alert_support(self, state: dict, alert: dict, reserve) -> dict:
        """Transactions behind each triggered rule condition, fetched from Zetaris.

        Rule features use trailing 24h/48h windows ending at the alert cutoff; the
        detail rows enter the canonical ledger so the planner can cite exact IDs,
        and the main supporting counterparties are registered for follow-up. Conditions
        sharing a transit gap reuse one route trace; progress notes feed the live view.
        """
        focus = Account.model_validate(state["request"]["account"])
        end = datetime.fromisoformat(state["request"]["as_of"])
        start = (end - timedelta(hours=48)).isoformat()
        note(state, "查询告警账户 48 小时内的全部交易", "Fetching the account's transactions of the last 48h")
        detail = self.zetaris.transactions(
            focus, start, end.isoformat(), "both", reserve, 200
        )
        rows = {str(row["transaction_id"]): row for row in detail["rows"]}
        complete = detail["complete"]
        program = (state.get("rule_context") or {}).get("program") or {}
        rules = {rule["rule_id"]: rule for rule in program.get("rules", [])}
        support, ranked_counterparties, flows = [], [], {}
        for rule_id in alert.get("triggered_rule_ids", []):
            rule = rules.get(rule_id)
            if rule is None:
                support.append({"rule_id": rule_id, "support": "rule definition unavailable"})
                continue
            feature = rule["feature_id"]
            entry = {
                "rule_id": rule_id,
                "condition": f"{feature} {rule['operator']} {rule['threshold']}",
                "alert_value": alert.get("features", {}).get(feature),
            }
            side = EVIDENCE_SIDE.get(feature, "both")
            if side in ("incoming", "outgoing"):
                incoming = side == "incoming"
                since = end - timedelta(hours=48 if feature.endswith("48h") else 24)
                groups: dict[tuple, list[str]] = {}
                for identifier, row in rows.items():
                    receiver = (row["to_bank"], row["to_account"]) == (focus.bank, focus.account)
                    if receiver != incoming or datetime.fromisoformat(row["transaction_time"]) < since:
                        continue
                    other = "from" if incoming else "to"
                    currency = row["receiving_currency" if incoming else "payment_currency"]
                    key = (f"{row[other + '_bank']}:{row[other + '_account']}", currency)
                    groups.setdefault(key, []).append(identifier)
                ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
                ranked_counterparties += [ref for (ref, _), _ in ordered]
                entry["supporting_counterparties"] = [
                    {"account_ref": ref, "currency": currency, "count": len(ids), "transaction_ids": ids[:5]}
                    for (ref, currency), ids in ordered[:15]
                ]
                entry["distinct_counterparties_observed"] = len(groups)
            elif side == "through":
                gap = 48 if feature != "transit_gap_hours" else next(
                    h for h in (6, 24, 48) if h >= min(48, rule["threshold"])
                )
                if gap not in flows:  # many triggered conditions share a gap; trace each route set once
                    note(state, f"追踪资金过境路径（收到后 {gap} 小时内转出）", f"Tracing pass-through routes (onward within {gap}h)")
                    flows[gap] = self.zetaris.flow(focus, "through", start, end.isoformat(), gap, 2, reserve)
                flow = flows[gap]
                complete = complete and flow["complete"]
                rows.update({row["transaction_id"]: row for row in flow["rows"]})
                entry["receive_then_send_routes"] = flow["routes"][:8]
                ranked_counterparties += [r["accounts"][-1] for r in flow["routes"]]
            else:
                entry["support"] = "account-level activity feature; see the prefetched 48h detail rows"
            support.append(entry)
        focus_ref = focus.reference
        # Then every counterparty in the prefetched detail by activity, so the team
        # coordinator can assign branches even when no condition names a counterparty.
        activity: dict[str, int] = {}
        for row in rows.values():
            for side in ("from", "to"):
                endpoint = f"{row[side + '_bank']}:{row[side + '_account']}"
                if endpoint != focus_ref:
                    activity[endpoint] = activity.get(endpoint, 0) + 1
        ranked_counterparties += sorted(activity, key=lambda ref: (-activity[ref], ref))
        return {
            "rows": list(rows.values()),
            "complete": complete,
            "window": {"start": start, "end": end.isoformat()},
            "query_scope": {"account_ref": focus_ref, "direction": "both", "other_account_ref": None},
            "rule_support": support,
            "registered_account_refs": self.register(state, focus_ref, ranked_counterparties),
            "support_scope": "Zetaris-fetched transactions behind each triggered condition; a rule hit is a lead, not a laundering label",
        } | self.enrich({"rows": list(rows.values())}, state, reserve, keep_rows=False)

    def execute(self, action: Action, state: dict, reserve) -> dict:
        args = action.arguments
        if set(args) - set(CATALOG[action.tool]["arguments"]):
            raise ValueError("Unknown tool arguments")
        windows = case_windows(state["request"])
        if action.tool == "get_alert_context":
            context = {
                "rule_version": state["request"]["rule_version"],
                "as_of": state["request"]["as_of"],
                "mode": "explicit_account_investigation"
                if not state["request"]["rule_version"]
                else "explicit_account_investigation_with_frozen_rule",
                "rule": "See frozen_rule in the packet",
            }
            alert = (state.get("rule_context") or {}).get("alert")
            if not alert:
                return context
            return {**context, **self.alert_support(state, alert, reserve)}
        if action.tool == "propose_rule_change":
            refs = args["evidence_refs"]
            if not refs or any(ref not in state["evidence"] for ref in refs):
                raise ValueError("Unknown evidence reference")
            if args["base_version"] != state["request"]["rule_version"]:
                raise ValueError("Rule proposal must name the frozen base version")
            proposal = {
                "proposal_id": f"proposal_{len(state['proposals']) + 1}",
                **args,
                "status": "pending_independent_evaluation"
                if args["change_type"] == "threshold"
                else "pending_rule_program_compilation",
                "published": False,
            }
            state["proposals"].append(proposal)
            return proposal
        references = args.get("account_refs") or [args["account_ref"]]
        for reference in references + [args.get("other_account_ref")] * ("other_account_ref" in args):
            if reference not in state["accounts"]:
                raise ValueError("Unknown account reference")
        window = windows[args.get("window_ref", "current")]
        if datetime.fromisoformat(window["end"]) <= datetime.fromisoformat(window["start"]):
            return {"status": "insufficient_history", "complete": False, "window": window}
        if action.tool == "assess_risk":
            return self.assess_risk(state, references, reserve)
        if action.tool == "summarize_account":
            result = self.zetaris.profile(
                [Account.model_validate(state["accounts"][r]) for r in references],
                window["start"],
                window["end"],
                reserve,
            )
            top = [
                counterparty["account_ref"]
                for reference in references
                for groups in result["accounts"][reference].values()
                for group in groups
                for counterparty in group["top_counterparties"]
            ]
            result["registered_account_refs"] = self.register(state, references[0], top)
            result["account_context"] = self.zetaris.account_features(
                [(r, d) for r in references for d in self.case_days(state)], state["request"]["as_of"], reserve
            )
            return result
        reference = references[0]
        account = Account.model_validate(state["accounts"][reference])
        if action.tool == "check_structure":
            needed = {"downstream": ["outgoing"], "upstream": ["incoming"], "through": ["incoming", "outgoing"]}[args["direction"]]
            for side in needed:
                basis = known_inactive(state, reference, side, window)
                if basis:
                    # Provably empty: a complete covering result already shows no such activity.
                    return {
                        "status": "empty_by_prior_evidence",
                        "complete": True,
                        "routes": [],
                        "basis_evidence_ref": basis,
                        "reason": f"No {side} activity for {reference} in a complete covering result; no source call made",
                        "window": window,
                    }
            result = self.zetaris.flow(
                account,
                args["direction"],
                window["start"],
                window["end"],
                args["max_gap_hours"],
                args["depth"],
                reserve,
            )
            # Convergence points first, then route accounts in route order (capped by register).
            key_accounts = result["convergence_accounts"] + [
                account for route in result["routes"] for account in route["accounts"]
            ]
            result["registered_account_refs"] = self.register(state, reference, key_accounts)
            return self.enrich(result, state, reserve)
        if action.tool == "compare_account_baseline":
            day = state["request"]["start"][:10]
            if datetime.fromisoformat(day) + timedelta(hours=24) > datetime.fromisoformat(state["request"]["as_of"]):
                return {"status": "insufficient_history", "complete": False, "reason": "Case day ends after the cutoff"}
            baseline = self.zetaris.baseline(account, day, args["history_days"], reserve)
            baseline["new_counterparties"] = self.zetaris.new_counterparties(account, day, args["history_days"], reserve)
            baseline["complete"] = True
            return baseline
        direction = args.get("direction", "both")
        other = None
        if action.tool == "query_between_accounts":
            other = Account.model_validate(state["accounts"][args["other_account_ref"]])
            direction = "both"
        elif direction != "both" and known_inactive(state, reference, direction, window):
            return {
                "status": "empty_by_prior_evidence",
                "complete": True,
                "rows": [],
                "basis_evidence_ref": known_inactive(state, reference, direction, window),
                "window": window,
                "query_scope": {"account_ref": reference, "direction": direction, "other_account_ref": None},
            }
        result = self.zetaris.transactions(
            account, window["start"], window["end"], direction, reserve, 200, other
        )
        result["query_scope"] = {
            "account_ref": reference,
            "direction": direction,
            "other_account_ref": args.get("other_account_ref"),
        }
        endpoints = [
            f"{row[side + '_bank']}:{row[side + '_account']}"
            for row in result["rows"]
            for side in ("from", "to")
        ]
        result["registered_account_refs"] = self.register(state, reference, endpoints)
        return self.enrich(result, state, reserve)
