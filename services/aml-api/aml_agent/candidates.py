"""Host-listed candidate transactions with observed structural signals.

Signals are label-free facts already present in stored evidence. The host neither
weights nor ranks them: the planner judges each, and calibrated weights may only
come from the separate rule learner on the development split.
"""

from __future__ import annotations

from decimal import Decimal

from .contracts import endpoint_ref

MAX_CANDIDATES = 12


def signalled(state: dict) -> dict[str, set[str]]:
    signals: dict[str, set[str]] = {}

    def mark(identifier: str, signal: str) -> None:
        signals.setdefault(str(identifier), set()).add(signal)

    for evidence in state["evidence"].values():
        for support in evidence.get("rule_support", []):
            for counterparty in support.get("supporting_counterparties", []):
                for identifier in counterparty["transaction_ids"]:
                    mark(identifier, "supports_rule")
            for route in support.get("receive_then_send_routes", []):
                route_signals(route, mark, set())
        if "routes" in evidence and "duplicate_of" not in evidence:
            convergence = set(evidence.get("convergence_accounts", []))
            for route in evidence["routes"]:
                route_signals(route, mark, convergence)
    return signals


def candidate_transactions(state: dict) -> list[dict]:
    rows = {}
    for evidence in state["evidence"].values():
        for row in evidence.get("rows", []):
            rows[str(row["transaction_id"])] = row
    listed = [
        {
            "transaction_id": identifier,
            "signals": sorted(found),
            "from": endpoint_ref(rows[identifier], "from"),
            "to": endpoint_ref(rows[identifier], "to"),
            "amount_paid": str(rows[identifier]["amount_paid"]),
            "currency": rows[identifier]["payment_currency"],
            "time": rows[identifier]["transaction_time"],
        }
        for identifier, found in signalled(state).items()
        if identifier in rows  # Only transactions retained in evidence can be cited.
    ]
    listed.sort(key=lambda item: (item["time"], item["transaction_id"]))
    return listed[:MAX_CANDIDATES]


def route_signals(route: dict, mark, convergence: set[str]) -> None:
    identifiers = route["transaction_ids"]
    if len(identifiers) < 2:
        return  # A single seed payment is not a route.
    retained = route.get("amount_retained")
    high = retained is not None and Decimal("0.8") <= Decimal(retained) <= Decimal("1.05")
    for index, identifier in enumerate(identifiers):
        mark(identifier, "on_route")
        if len(identifiers) >= 3:
            mark(identifier, "multi_hop_route")
        if high:
            mark(identifier, "amount_retained_high")
        if route.get("returns_to_origin"):
            mark(identifier, "returns_to_origin")
        if route.get("currency_changes"):
            mark(identifier, "currency_switch")
        if route["accounts"][index + 1] in convergence:
            mark(identifier, "converges")
    for index, gap in enumerate(route.get("gap_hours", [])):
        if gap <= 6:
            mark(identifiers[index], "fast_hop")
            mark(identifiers[index + 1], "fast_hop")
