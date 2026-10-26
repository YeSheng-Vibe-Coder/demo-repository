"""Layer 2: risk tiers for layer-1 alerts, scored inside Zetaris.

A small gradient-boosted tree ensemble is fitted on development account-days that
layer 1 alerts on, then stored as plain tree structures and compiled to one SQL
expression, so every alert of a partition is scored in Zetaris without exporting
rows. Tier cut-offs are chosen on development by laundering-chain coverage:
high holds the alerts that catch the first `high` share of chains, mid the next
band, low the rest. Investigators see an alert's tier and its development base
rate, never a label.
"""

from __future__ import annotations

import json

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier

from .chains import Chains
from .feature_registry import FEATURES
from .profile import PROFILE
from .rule_learning import RuleLearner, column_sql, condition_sql, partition_days, program_true, value
from .rule_program import RuleProgram

SETTINGS = PROFILE["risk_tiers"]
TIERS = ("high", "mid", "low")


def export_trees(model: GradientBoostingClassifier, features: list[str], x: np.ndarray) -> dict:
    """Plain, JSON-storable trees: score = base + rate * sum(leaf values)."""
    trees = []
    for estimator in model.estimators_[:, 0]:
        t = estimator.tree_
        trees.append({
            "feature": [features[i] if i >= 0 else None for i in t.feature],
            "threshold": [float(v) for v in t.threshold],
            "left": [int(v) for v in t.children_left],
            "right": [int(v) for v in t.children_right],
            "value": [float(v[0][0]) for v in t.value],
        })
    rate = float(model.learning_rate)
    raw = model.decision_function(x[:1])[0]
    base = float(raw - rate * sum(e.predict(x[:1])[0] for e in model.estimators_[:, 0]))
    return {"features": features, "base": base, "rate": rate, "trees": trees}


def score_rows(model: dict, rows: list[dict]) -> np.ndarray:
    """The stored ensemble evaluated in Python with the same semantics as its SQL."""
    out = np.full(len(rows), model["base"])
    for index, row in enumerate(rows):
        for tree in model["trees"]:
            node = 0
            while tree["left"][node] != -1:
                current = value(row, tree["feature"][node])
                node = tree["left"][node] if current <= tree["threshold"][node] else tree["right"][node]
            out[index] += model["rate"] * tree["value"][node]
    return out


def score_sql(model: dict) -> str:
    def node_sql(tree: dict, node: int) -> str:
        if tree["left"][node] == -1:
            return repr(tree["value"][node])
        name = tree["feature"][node]
        column = column_sql(name)
        return (f"CASE WHEN {column} <= {tree['threshold'][node]!r} THEN {node_sql(tree, tree['left'][node])} "
                f"ELSE {node_sql(tree, tree['right'][node])} END")

    return f"({model['base']!r} + {model['rate']!r} * (" + " + ".join(node_sql(t, 0) for t in model["trees"]) + "))"




def load_model(model_id: str | None, rule_version: str | None) -> dict | None:
    """A stored tier model (with cut-offs) by ID, else the latest one for the rule version."""
    from .settings import ROOT, Settings
    from .store import Store

    cache = ROOT / ".runtime/tiers"
    if model_id and (cache / f"{model_id}.json").exists():
        return json.loads((cache / f"{model_id}.json").read_text())
    found = [(mid, body) for mid, body in Store(Settings.load().database).artifacts("risk_tier_model")
             if mid == model_id or (not model_id and body["rule_version"] == rule_version)]
    if not found:
        return None
    mid, body = found[-1]
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    (cache / f"{mid}.json").write_text(json.dumps(body["model"]))
    return body["model"]


def tier_of(model: dict, score: float) -> str:
    return "high" if score >= model["cutoffs"]["high"] else "mid" if score >= model["cutoffs"]["mid"] else "low"


def explain(model: dict, features: dict, calibration: dict | None) -> dict:
    """Score, tier and the features moving the score most against the population median."""
    from .calibration import position

    score = float(score_rows(model, [features])[0])
    drivers = []
    for name in model["features"]:
        cuts = ((calibration or {}).get("quantiles") or {}).get(name)
        if not cuts or features.get(name) is None:
            continue
        median = cuts[4]  # p50 of the calibration quantiles
        if features[name] == median:
            continue
        typical = float(score_rows(model, [{**features, name: median}])[0])
        if abs(score - typical) < 0.01:
            continue
        drivers.append({"feature": name, "value": features[name], "population_median": median,
                        "position": position(name, features[name], calibration), "score_effect": round(score - typical, 3)})
    drivers.sort(key=lambda d: -abs(d["score_effect"]))
    return {"score": round(score, 3), "tier": tier_of(model, score), "drivers": drivers[:4]}


def tier_sql(model: dict) -> str:
    high, mid = model["cutoffs"]["high"], model["cutoffs"]["mid"]
    score = score_sql(model)
    return f"CASE WHEN {score} >= {high!r} THEN 'high' WHEN {score} >= {mid!r} THEN 'mid' ELSE 'low' END"


class Tiering:
    def __init__(self, learner: RuleLearner, log=print):
        self.learner, self.log = learner, log
        self.chains = Chains(learner.zetaris, log=log)

    def fit(self, program: RuleProgram, rows: list[dict]) -> dict:
        """Fit on the development sample rows that layer 1 alerts on."""
        flagged = [r for r in rows if program_true(program, r)]
        features = list(FEATURES)
        x = RuleLearner.matrix(flagged, features)
        y = np.array([r["label"] for r in flagged])
        model = GradientBoostingClassifier(n_estimators=SETTINGS["trees"], max_depth=SETTINGS["depth"],
                                           learning_rate=SETTINGS["learning_rate"], subsample=0.8, random_state=0)
        model.fit(x, y)
        stored = export_trees(model, features, x)
        # The compiled form must reproduce scikit-learn's scores before it is used anywhere.
        drift = float(np.max(np.abs(score_rows(stored, flagged[:200]) - model.decision_function(x[:200]))))
        if drift > 1e-6:
            raise ValueError(f"Compiled tier model disagrees with scikit-learn by {drift}")
        importance = sorted(zip(features, model.feature_importances_), key=lambda item: -item[1])
        stored["importance"] = [{"feature": f, "importance": round(float(v), 4)} for f, v in importance if v > 0]
        stored["training"] = {"rows": len(flagged), "positives": int(y.sum())}
        self.log(json.dumps({"tier_model": stored["training"], "top_features": stored["importance"][:5]}))
        return stored

    def scored(self, program: RuleProgram, model: dict, split: str) -> str:
        return (f"SELECT bank, account, day, {score_sql(model)} AS score FROM "
                f"({self.learner.features(split, condition_sql(program))}) f")

    def cutoffs(self, program: RuleProgram, model: dict) -> dict:
        """Development score cut-offs at the configured chain-coverage shares."""
        scored = self.scored(program, model, "development")
        members = self.chains.scoped_members("development")
        # Each chain is reached at the best score among its alerted member accounts.
        best = self.learner.zetaris.run(
            f"SELECT MAX(q.score) FROM {members} m JOIN (SELECT CONCAT(bank, ':', account) AS account, MAX(score) AS score "
            f"FROM ({scored}) s GROUP BY 1) q ON q.account = m.account GROUP BY m.chain", 100000)
        values = np.sort(np.array([float(r[0]) for r in best]))[::-1]
        high = float(values[int(len(values) * SETTINGS["high_chain_share"]) - 1])
        mid = float(values[int(len(values) * SETTINGS["mid_chain_share"]) - 1])
        return {"high": high, "mid": mid, "basis": "Development chain coverage: high reaches the first "
                f"{SETTINGS['high_chain_share']:.0%} of caught chains, high+mid {SETTINGS['mid_chain_share']:.0%}"}

    def metrics(self, program: RuleProgram, model: dict, split: str) -> dict:
        """Per tier: alerts, alerts per day, window precision and chains first reached in that tier."""
        tiered = (f"SELECT bank, account, day, {tier_sql(model)} AS tier, label "
                  f"FROM ({self.learner.labelled(split, condition_sql(program))}) f")
        rows = self.learner.zetaris.run(f"SELECT tier, COUNT(*), SUM(label) FROM ({tiered}) t GROUP BY tier", 10)
        members = self.chains.scoped_members(split)
        rank = "CASE tier WHEN 'high' THEN 3 WHEN 'mid' THEN 2 ELSE 1 END"
        reach = self.learner.zetaris.run(
            f"SELECT best, COUNT(*) FROM (SELECT m.chain, MAX({rank}) AS best FROM {members} m "
            f"JOIN (SELECT DISTINCT CONCAT(bank, ':', account) AS account, tier FROM ({tiered}) t) q ON q.account = m.account "
            "GROUP BY m.chain) c GROUP BY best", 10)
        total = self.chains.total(split)
        days = len(partition_days(split))
        by_rank = {int(r[0]): int(r[1]) for r in reach}
        result = {"split": split, "days": days, "chains": total}
        cumulative = 0
        for name, level in zip(TIERS, (3, 2, 1)):
            count, hits = next(((int(r[1]), int(r[2] or 0)) for r in rows if r[0] == name), (0, 0))
            cumulative += by_rank.get(level, 0)
            result[name] = {"alerts": count, "alerts_per_day": round(count / days) if days else None,
                            "window_precision": hits / count if count else 0.0, "positive_windows": hits,
                            "chains_first_reached": by_rank.get(level, 0),
                            "cumulative_chain_recall": cumulative / total if total else None}
        return result
