"""Deterministic observation digests: a few short, cited fact lines per tool result.

Digests replace raw nested JSON in the planner's working context, feed the H-MEM
case memory and are the unit shared between investigators. They are produced by
code from stored evidence only; no model writes or rewrites them.
"""

from __future__ import annotations

MAX_LINES = 6


def flags(context: dict | None) -> str:
    if not context:
        return ""
    parts = []
    if context.get("payer_prior_in_gap_h") is not None:
        parts.append(f"payer received {context['payer_prior_in_gap_h']}h earlier from {context['payer_prior_sources_48h']} source(s)")
    if context.get("payer_prior_other_currency"):
        parts.append("payer had received another currency")
    if context.get("reverse_edge_prior_48h"):
        parts.append("receiver had paid the payer within 48h")
    return "; ".join(parts)


def number(value) -> str:
    return "?" if value is None else f"{float(value):g}"


def account_line(reference: str, days: dict, calibration: dict | None = None) -> str:
    from .calibration import position

    day, f = sorted(days.items())[-1]
    gap = f.get("transit_gap_hours")

    def at(name: str, text: str) -> str:
        where = position(name, f.get(name), calibration)
        return f"{text} ({where})" if where else text

    return (
        f"{reference} on {day}: {at('fan_in_48h', 'sources48h=' + number(f.get('fan_in_48h')))} "
        f"{at('fan_out_24h', 'payees24h=' + number(f.get('fan_out_24h')))} "
        f"{at('currency_count_24h', 'currencies=' + number(f.get('currency_count_24h')))} "
        f"receive-then-send={'none' if gap is None else number(gap) + 'h'} "
        f"{at('activity_change_24h', 'activity x' + number(f.get('activity_change_24h')))}"
        + (f" {at('new_counterparty_share_24h', 'new-counterparty-share=' + number(f.get('new_counterparty_share_24h')))} "
           f"{at('history_active_days_7d', 'active-days-prior-7d=' + number(f.get('history_active_days_7d')))} "
           f"{at('amount_vs_history_24h', 'amount-vs-own-7d-max x' + number(f.get('amount_vs_history_24h')))}"
           if f.get("history_active_days_7d") is not None else "")
    )


def risk_line(reference: str, item: dict) -> str:
    """One account's layer-2 risk on the case day: tier, score and its top drivers."""
    if not item.get("active"):
        return f"{reference}: no activity on the case day"
    if not item["layer1_alert"]:
        return f"{reference}: not alerted by the high-recall layer-1 rule on the case day"
    drivers = "; ".join(f"{d['feature']}={d['value']:g} ({d['position'] or 'n/a'}, {'+' if d['score_effect'] >= 0 else ''}{d['score_effect']})"
                        for d in item["drivers"][:3])
    return f"{reference}: tier {item['tier']} score {item['score']}; drivers {drivers}"


def digest(tool: str, arguments: dict, result: dict, ref: str) -> list[str]:
    """Fact lines for one observation; every line ends with its evidence reference."""
    lines: list[str] = []
    cite = f" [{ref}]"
    status = result.get("status")
    if result.get("duplicate_of"):
        return [f"Repeated {tool}: no new data; stored result {result['duplicate_of']} reused{cite}"]
    if status in ("empty_by_prior_evidence", "insufficient_history"):
        return [f"{tool} {arguments.get('account_ref', '')}: {status}. {result.get('reason', '')}".strip() + cite]
    if tool == "get_alert_context":
        for support in result.get("rule_support", []):
            line = f"Rule {support['rule_id']} ({support.get('condition')}) value {support.get('alert_value')}"
            parties = support.get("supporting_counterparties") or []
            if parties:
                top = ", ".join(f"{p['account_ref']} x{p['count']}" for p in parties[:3])
                line += f"; {support.get('distinct_counterparties_observed')} counterparties, top {top}"
            routes = support.get("receive_then_send_routes") or []
            if routes:
                line += f"; {len(routes)} receive-then-send routes"
            lines.append(line + cite)
        rows = result.get("rows") or []
        lines.append(f"Alert account detail: {len(rows)} transactions in the 48h before cutoff, complete={result.get('complete')}{cite}")
    elif tool == "summarize_account":
        for reference, sides in (result.get("accounts") or {}).items():
            for side, groups in sides.items():
                for group in groups[:2]:
                    top = ", ".join(f"{c['account_ref']} x{c['count']}" for c in group["top_counterparties"][:3])
                    lines.append(
                        f"{reference} {side} {group['currency']}: {group['count']} tx, {group['counterparties']} counterparties, "
                        f"amount {group['amount']}; top {top}{cite}"
                    )
                if not groups:
                    lines.append(f"{reference} {side}: none in a complete window{cite}" if result.get("complete") else f"{reference} {side}: none retrieved (partial){cite}")
    elif tool == "check_structure":
        routes = result.get("routes") or []
        scope = result.get("flow_scope", {})
        head = f"{scope.get('mode', arguments.get('direction'))} from {arguments.get('account_ref')} depth {scope.get('depth')}: {result.get('routes_total', len(routes))} routes, max hops {result.get('hops_reached')}"
        if result.get("convergence_accounts"):
            head += f", converge at {', '.join(result['convergence_accounts'][:3])}"
        if result.get("returns_to_origin"):
            head += ", returns to origin"
        lines.append(head + cite)
        for route in sorted(routes, key=lambda r: -len(r["transaction_ids"]))[:3]:
            if len(route["transaction_ids"]) < 2:
                continue
            lines.append(
                f"route {' > '.join(route['accounts'])} via {','.join(route['transaction_ids'])}; gaps {route['gap_hours']}h; "
                f"{'/'.join(route.get('currencies', []))}; retained {route.get('amount_retained')}{cite}"
            )
    elif tool == "assess_risk":
        for reference, item in list((result.get("accounts") or {}).items())[:5]:
            lines.append(f"Risk {risk_line(reference, item)}{cite}")
    elif tool == "compare_account_baseline":
        comparison = result.get("comparison") or {}
        notable = sorted(
            ((name, c) for name, c in comparison.items() if c.get("z_score") is not None),
            key=lambda item: -abs(item[1]["z_score"]),
        )[:3]
        reliability = "reliable" if result.get("baseline_reliable") else "unreliable (few active days)"
        lines.append(f"Baseline {arguments.get('account_ref')} over {result.get('history_days')}d, {result.get('history_active_days')} active days, {reliability}{cite}")
        for name, c in notable:
            lines.append(f"  {name}: today {number(c['current'])} vs mean {number(c['history_mean'])} (z {c['z_score']}){cite}")
        new = result.get("new_counterparties") or {}
        for side in ("incoming", "outgoing"):
            if new.get(side, {}).get("counterparties_today"):
                lines.append(f"  {side}: {new[side]['new_vs_history']}/{new[side]['counterparties_today']} counterparties new vs history{cite}")
    rows = result.get("rows")
    if rows is not None and tool in ("query_account_transactions", "query_between_accounts", "check_structure"):
        context = result.get("transaction_context") or {}
        notable = [row for row in rows if flags(context.get(str(row["transaction_id"])))]
        if tool != "check_structure":
            lines.append(f"{tool} {arguments.get('account_ref')} {arguments.get('direction', '')}: {len(rows)} rows, complete={result.get('complete')}{cite}")
        for row in notable[:3]:
            lines.append(
                f"  tx {row['transaction_id']} {row['from_bank']}:{row['from_account']} > {row['to_bank']}:{row['to_account']} "
                f"{row['amount_paid']} {row['payment_currency']}: {flags(context.get(str(row['transaction_id'])))}{cite}"
            )
    for reference, days in list((result.get("account_context") or {}).items())[:2]:
        lines.append("  context " + account_line(reference, days) + cite)
    return lines[:MAX_LINES] or [f"{tool}: observed{cite}"]
