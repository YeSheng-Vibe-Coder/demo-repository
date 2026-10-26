"""Population calibration for investigators: where a value sits among all development account-days.

Built once in Zetaris from the development feature store and cached. It holds only
population statistics (feature quantiles and the base rate of the alerting rule
groups); no account's label ever reaches an investigator through it.
"""

from __future__ import annotations

import json

from .feature_registry import FEATURES
from .settings import ROOT

CACHE = ROOT / ".runtime/calibration/development.json"
QUANTILES = (0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99)
# Features whose unusual side is the low end (a short receive-then-send gap).
LOW_UNUSUAL = {"transit_gap_hours", "history_active_days_7d"}


def build(learner) -> dict:
    """Feature quantiles over every development account-day, computed in Zetaris."""
    columns = ", ".join(
        f"PERCENTILE_APPROX(CAST({name} AS DOUBLE), ARRAY({', '.join(map(str, QUANTILES))}))" for name in FEATURES
    )
    row = learner.zetaris.run(f"SELECT COUNT(*), {columns} FROM ({learner.features('development')}) p", 2)[0]
    quantiles = {}
    for name, cell in zip(FEATURES, row[1:]):
        values = json.loads(cell) if isinstance(cell, str) else cell
        quantiles[name] = [float(v) for v in values] if values else None
    data = {"account_days": int(row[0]), "quantiles": quantiles, "levels": [round(q * 100) for q in QUANTILES]}
    CACHE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE.write_text(json.dumps(data))
    return data


def load() -> dict | None:
    return json.loads(CACHE.read_text()) if CACHE.exists() else None


def position(name: str, value, data: dict | None) -> str:
    """Population position of one value: 'typical', 'above p90', 'top 1%', 'bottom 5%'; '' when uncalibrated.

    Ties are common (most account-days have one counterparty), so a value equal to the
    median is 'typical' rather than a percentile it shares with half the population.
    """
    if data is None or value is None or not data["quantiles"].get(name):
        return ""
    cuts, levels = data["quantiles"][name], data["levels"]
    if name in LOW_UNUSUAL:
        # With ties the widest matching band is the honest one: 0 shared by 10% is "bottom 10%", not 1%.
        for cut, level in reversed(list(zip(cuts, levels))):
            if level <= 10 and value <= cut:
                return f"bottom {level}%"
        return "typical"
    for cut, level in reversed(list(zip(cuts, levels))):
        if level >= 90 and value > cut:
            return f"top {100 - level}%"
    return "typical"


def unusual(where: str) -> bool:
    return where.startswith(("top", "bottom"))


def annotate(features: dict, data: dict | None) -> str:
    """Alert features with their population position, unusual ones first."""
    items = [(k, v, position(k, v, data)) for k, v in features.items() if v not in (None, 0, 0.0)]
    items.sort(key=lambda item: not unusual(item[2]))
    return ", ".join(f"{k}={v:g}" + (f" ({pos})" if pos else "") for k, v, pos in items)


def base_rates(rule_context: dict | None) -> list[str]:
    """Development precision of the rule groups this alert matched (from the frozen version)."""
    if not rule_context:
        return []
    tier = rule_context.get("tier")
    tier_line = []
    if tier and tier.get("tier"):
        rate = tier.get("tier_base_rate")
        tier_line = [f"risk tier {tier['tier'].upper()} (from the {tier.get('tier_source') or 'tier model'})"
                     + (f": about {rate:.0%} of development alerts in this tier are laundering windows" if rate is not None else "")]
    alert = rule_context.get("alert") or {}
    stats = {s["group_id"]: s for s in rule_context.get("group_stats") or []}
    hit = set(alert.get("triggered_rule_ids", []))
    lines = []
    for group in (rule_context.get("program") or {}).get("groups", []):
        if group["operator"] == "all" and set(group["rule_ids"]) <= hit and group["group_id"] in stats:
            share = stats[group["group_id"]]["estimated_population_precision"]
            lines.append(f"rule group {group['group_id']} matched: about {share:.1%} of development account-days it matches are laundering")
    return tier_line + lines[:3]
