"""Registered account-window features: unit, policy provenance and meaning.

Every feature is computed by a fixed Zetaris template over transactions strictly
before the window cutoff. A rule may only use registered features and must cite
one of the feature's verified AUSTRAC paragraphs. Features without a verified
paragraph (e.g. round-amount share) are deliberately absent.
"""

FEATURES = {
    "fan_in_24h": ("counterparties", ("P02",), "Max distinct paying counterparties in one currency, last 24h"),
    "fan_in_48h": ("counterparties", ("P02",), "Max distinct paying counterparties in one currency, last 48h"),
    "fan_out_24h": ("counterparties", ("P03",), "Max distinct receiving counterparties in one currency, last 24h"),
    "fan_out_48h": ("counterparties", ("P03",), "Max distinct receiving counterparties in one currency, last 48h"),
    "in_count_24h": ("transactions", ("P02",), "Incoming payments, last 24h"),
    "out_count_24h": ("transactions", ("P03",), "Outgoing payments, last 24h"),
    "in_banks_24h": ("banks", ("P02",), "Max distinct paying banks in one currency, last 24h"),
    "out_banks_24h": ("banks", ("P03",), "Max distinct receiving banks in one currency, last 24h"),
    "pass_through_ratio_24h": ("ratio", ("P03",), "Max over currencies of outgoing/incoming amount, capped at 10, last 24h"),
    "transit_gap_hours": ("hours", ("P03",), "Shortest gap from an incoming to a later outgoing payment (any currency), outgoing in last 24h, incoming within 48h; null if none"),
    "transit_pairs_24h": ("pairs", ("P03",), "Incoming-then-outgoing pairs within 24h, outgoing in last 24h"),
    "round_trip_48h": ("transactions", ("P03",), "Outgoing payments to an account that paid this account earlier within 48h"),
    "currency_count_24h": ("currencies", ("P03",), "Distinct currencies across incoming and outgoing payments, last 24h"),
    "onward_currency_switch_48h": ("transactions", ("P03",), "Outgoing payments in a different currency from an earlier incoming payment within 48h"),
    "activity_change_24h": ("ratio", ("P04",), "Payments in the last 24h divided by (payments in the prior 24h + 1)"),
    # The account against its own prior 7 days ([D - 7d, D)); current window [D, D + 24h).
    "new_counterparty_share_24h": ("ratio", ("P04",), "Share of last-24h counterparties (either direction) with no transaction with this account in the prior 7 days"),
    "new_counterparties_24h": ("counterparties", ("P04",), "Last-24h counterparties with no transaction with this account in the prior 7 days"),
    "history_active_days_7d": ("days", ("P04",), "Days with any payment in the prior 7 days (0 = dormant before today)"),
    "amount_vs_history_24h": ("ratio", ("P04",), "Max over currencies of the largest last-24h payment / (largest payment in the prior 7 days in that currency + 1), capped at 1000"),
}

UNITS = {name: unit for name, (unit, _, _) in FEATURES.items()}
POLICY = {name: refs for name, (_, refs, _) in FEATURES.items()}
DESCRIPTIONS = {name: text for name, (_, _, text) in FEATURES.items()}
# Where an alert's supporting transactions live, for the host's alert prefetch.
EVIDENCE_SIDE = {
    name: (
        "incoming"
        if name.startswith(("fan_in", "in_"))
        else "outgoing"
        if name.startswith(("fan_out", "out_"))
        else "through"
        if name in ("transit_gap_hours", "transit_pairs_24h", "round_trip_48h", "onward_currency_switch_48h", "pass_through_ratio_24h")
        else "both"
    )
    for name in FEATURES
}
INTEGER_FEATURES = {
    name for name, unit in UNITS.items() if unit in ("counterparties", "transactions", "banks", "pairs", "currencies", "days")
}
