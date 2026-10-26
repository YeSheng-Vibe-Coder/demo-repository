"""Rule learner agent: Zetaris features + development labels -> decision tree -> rule program.

Layer 1 of the screening funnel is built for recall: a laundering chain (a connected
component of laundering transfers) counts as caught when any of its accounts is
alerted on any day, and the rule must catch at least the target share of chains.
Leaves of the fitted tree are added in order of chains newly caught per alerted
account-day until the development target is met; precision is then improved by
later layers and by updates that keep recall while alerting less.

Separate from the investigator agents. It may read development labels (to fit)
and validation/monitor labels (to compare frozen versions); it never feeds
labels to an investigator. All population queries run inside Zetaris on the
day-sharded feature store; only sampled feature rows and counts come back.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from sklearn.tree import DecisionTreeClassifier

from .contracts import StrictModel
from .evaluation import split_plan
from .feature_registry import DESCRIPTIONS, FEATURES, INTEGER_FEATURES, POLICY
from .chains import Chains
from .feature_store import LABEL_TABLE, FeatureStore, features_relation
from .profile import PROFILE
from .rule_program import RuleAtom, RuleGroup, RuleProgram, compile_program
from .settings import ROOT

SETTINGS = PROFILE["rule_learning"]
# No receive-then-send pair within 48h is encoded as 49h for trees and SQL alike.
MISSING = {"transit_gap_hours": 49.0}
POLICY_TEXT = {
    "P02": "has an account that receives deposits from multiple third parties",
    "P03": "makes rapid or complex funds transfers through multiple accounts",
    "P04": "has transactions that are inconsistent with their profile or transaction history",
}


def day_range(split: str) -> tuple[str, str]:
    """Window days D with cutoff D + 24h inside the partition: start <= D < end."""
    plan = split_plan()[split]
    return plan["start"][:10], plan["end"][:10]


def partition_days(split: str) -> list[str]:
    """Every window day of the partition, in order."""
    start, end = (date.fromisoformat(d) for d in day_range(split))
    return [(start + timedelta(days=k)).isoformat() for k in range((end - start).days)]


def days_clause(split: str, alias: str = "g") -> str:
    start, end = day_range(split)
    return f"{alias}.day >= DATE '{start}' AND {alias}.day < DATE '{end}'"


def value(features: dict, name: str) -> float:
    raw = features.get(name)
    return float(MISSING.get(name, 0.0) if raw is None else raw)


def atom_true(rule: RuleAtom, features: dict) -> bool:
    current = value(features, rule.feature_id)
    return current >= rule.threshold if rule.operator == "ge" else current <= rule.threshold


def program_true(program: RuleProgram, features: dict) -> bool:
    """Whether the program alerts on one account-day; the Python form of condition_sql."""
    hits = {rule.rule_id: atom_true(rule, features) for rule in program.rules}
    groups = []
    for group in program.groups:
        if group.operator == "weighted":
            groups.append(sum(w for r, w in zip(group.rule_ids, group.weights) if hits[r]) >= group.cutoff)
        else:
            groups.append((all if group.operator == "all" else any)(hits[r] for r in group.rule_ids))
    return (all if program.group_operator == "all" else any)(groups)


def column_sql(name: str) -> str:
    """A feature as a double with the missing-value default, matching value()."""
    return f"COALESCE(CAST({name} AS DOUBLE), {MISSING.get(name, 0.0)})"


def alert_row(columns: list[str], row: list) -> dict:
    """One account-day row from Zetaris: identity plus decoded features."""
    record = dict(zip(columns, row))
    return {"bank": str(record["bank"]), "account": str(record["account"]), "day": str(record["day"]),
            "features": {n: None if record[n] is None else float(record[n]) for n in FEATURES}}


def atom_sql(rule: RuleAtom) -> str:
    column = f"COALESCE({rule.feature_id}, {MISSING.get(rule.feature_id, 0)})"
    return f"({column} {'>=' if rule.operator == 'ge' else '<='} {rule.threshold!r})"


def condition_sql(program: RuleProgram) -> str:
    """Compile a rule program to a Zetaris predicate with the same semantics as atom_true."""
    atoms = {rule.rule_id: atom_sql(rule) for rule in program.rules}
    groups = []
    for group in program.groups:
        if group.operator == "weighted":
            score = " + ".join(
                f"(CASE WHEN {atoms[r]} THEN {w!r} ELSE 0 END)"
                for r, w in zip(group.rule_ids, group.weights)
            )
            groups.append(f"(({score}) >= {group.cutoff!r})")
        else:
            joiner = " AND " if group.operator == "all" else " OR "
            groups.append("(" + joiner.join(atoms[r] for r in group.rule_ids) + ")")
    return "(" + (" AND " if program.group_operator == "all" else " OR ").join(groups) + ")"


def score_sql(program: RuleProgram, scores: dict | None) -> str:
    """Development-calibrated group precision as an alert priority; 0 when uncalibrated."""
    if not scores:
        return "0.0"
    atoms = {rule.rule_id: atom_sql(rule) for rule in program.rules}
    cases = [
        f"WHEN {' AND '.join(atoms[r] for r in group.rule_ids)} THEN {float(scores[group.group_id])!r}"
        for group in sorted(program.groups, key=lambda g: -scores.get(g.group_id, 0))
        if group.operator == "all" and group.group_id in scores
    ]
    return "CASE " + " ".join(cases) + " ELSE 0.0 END" if cases else "0.0"


HASH_ORDER = "MD5(CONCAT(bank, ':', account, ':', CAST(day AS STRING)))"
SHARD_ROW_LIMIT = 200_000


def triggered(program: RuleProgram, features: dict) -> list[str]:
    return [rule.rule_id for rule in program.rules if atom_true(rule, features)]


class UpdatePlan(StrictModel):
    # Bounded formats rather than enums: an unlisted value would dead-end the grammar.
    features: list[str] = Field(min_length=3, max_length=len(FEATURES))
    max_leaf_nodes: int = Field(ge=8, le=48)
    min_samples_leaf: int = Field(ge=5, le=500)
    positive_weight: float = Field(ge=1, le=20)
    rationale: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def registered_only(self) -> "UpdatePlan":
        known = [name for name in dict.fromkeys(self.features) if name in FEATURES]
        if len(known) < 3:
            raise ValueError(f"Use at least three registered features: {', '.join(FEATURES)}")
        self.features = known  # Unregistered names are dropped, never computed ad hoc.
        return self


class RuleExplanation(StrictModel):
    group_id: str = Field(max_length=40)
    austrac_ref: Literal["P02", "P03", "P04"]
    explanation: str = Field(min_length=1, max_length=240)
    concerns: str = Field(default="", max_length=160)


class ProgramExplanation(StrictModel):
    rules: list[RuleExplanation] = Field(max_length=8)
    overall: str = Field(min_length=1, max_length=300)


class RuleLearner:
    def __init__(self, store, provider, feature_store: FeatureStore | None = None, log=print):
        self.store, self.provider = store, provider
        self.zetaris = feature_store or FeatureStore()
        self.log = log
        self.totals: dict[str, dict] = {}

    # ---- Zetaris population queries -------------------------------------------------

    @staticmethod
    def features(split: str, where: str = "TRUE", day: str | None = None) -> str:
        """Composite feature relation for a partition (or one of its day shards), filtered on its output columns."""
        days = f"g.day = DATE '{day}'" if day else days_clause(split)
        return f"SELECT * FROM ({features_relation(days)}) f WHERE {where}"

    def labelled(self, split: str, where: str = "TRUE", day: str | None = None) -> str:
        """Feature rows joined with evaluation-only window labels (rule learner only)."""
        return (
            "SELECT f.*, COALESCE(l.label, 0) AS label FROM ("
            + self.features(split, where, day)
            + f") f LEFT JOIN {LABEL_TABLE} l ON l.bank = f.bank AND l.account = f.account AND l.day = f.day"
        )

    def population(self, split: str) -> dict[int, int]:
        """Account-days of the partition per window label (evaluation side), cached."""
        if split not in self.totals:
            self.totals[split] = {int(r[0]): int(r[1]) for r in self.zetaris.run(
                f"SELECT label, COUNT(*) FROM ({self.labelled(split)}) j GROUP BY label")}
        return self.totals[split]

    def sample(self, split: str, negative_ratio: float) -> tuple[list[dict], dict]:
        """Every positive window of the partition plus a deterministic hash sample of negatives.

        Read day shard by day shard so each Zetaris request stays bounded.
        """
        population = self.population(split)
        rate = min(1.0, negative_ratio * population.get(1, 0) / max(1, population.get(0, 0)))
        keep = int(rate * 1_000_000)
        columns = ["bank", "account", "day", *FEATURES, "label"]
        days = partition_days(split)

        def shard(day: str) -> list[list]:
            rows = self.zetaris.run(
                "SELECT " + ", ".join(columns) + " FROM (" + self.labelled(split, day=day) + ") j WHERE label = 1 OR "
                f"PMOD(CAST(CONV(SUBSTR({HASH_ORDER}, 1, 8), 16, 10) AS BIGINT), 1000000) < {keep}",
                SHARD_ROW_LIMIT,
            )
            if len(rows) >= SHARD_ROW_LIMIT:
                raise ValueError(f"Training shard {day} reached the row limit; lower the negative ratio")
            return rows

        with ThreadPoolExecutor(SETTINGS["training_shard_parallelism"]) as pool:
            rows = [row for part in pool.map(shard, days) for row in part]
        self.log(json.dumps({"training_sample": len(rows), "days": len(days), "negative_rate": round(rate, 5)}))
        records = [dict(zip(columns, row)) for row in rows]
        for record in records:
            for name in FEATURES:
                record[name] = None if record[name] is None else float(record[name])
            record["label"] = int(record["label"])
        return records, population

    def scan_alerts(self, program: RuleProgram, split: str, count: int, per_day: int, scores: dict | None = None) -> list[dict]:
        """Full Zetaris scan of the partition: the top `per_day` alerts of every day, label-blind,
        so the sample covers the whole partition instead of the highest-scoring days only."""
        columns = ["bank", "account", "day", *FEATURES]
        ranked = (
            f"SELECT a.*, ROW_NUMBER() OVER (PARTITION BY day ORDER BY {score_sql(program, scores)} DESC, {HASH_ORDER}) AS r FROM ("
            + self.features(split, condition_sql(program))
            + ") a"
        )
        rows = self.zetaris.run(
            "SELECT " + ", ".join(columns) + f" FROM ({ranked}) s WHERE r <= {per_day} ORDER BY day, r LIMIT {count}", count + 1
        )
        return [alert_row(columns, row) for row in rows]

    # ---- learning -------------------------------------------------------------------

    @staticmethod
    def matrix(rows: list[dict], features: list[str]) -> np.ndarray:
        return np.array([[value(row, name) for name in features] for row in rows], dtype=float)

    def fit(self, rows: list[dict], features: list[str], leaves: int, min_leaf: int, positive_weight: float, extra_weight: dict | None = None):
        x = self.matrix(rows, features)
        y = np.array([row["label"] for row in rows])
        positives, negatives = max(1, int(y.sum())), max(1, int(len(y) - y.sum()))
        weights = np.where(y == 1, positive_weight * negatives / positives, 1.0)
        for index, row in enumerate(rows):
            weights[index] *= (extra_weight or {}).get((row["bank"], row["account"], str(row["day"])), 1.0)
        tree = DecisionTreeClassifier(max_leaf_nodes=leaves, min_samples_leaf=min_leaf, random_state=0)
        tree.fit(x, y, sample_weight=weights)
        return tree

    @staticmethod
    def leaf_sql(tree: DecisionTreeClassifier, features: list[str]) -> str:
        """The tree as a Zetaris expression returning each account-day's leaf."""
        structure = tree.tree_

        def node_sql(node: int) -> str:
            if structure.children_left[node] == structure.children_right[node]:
                return str(int(node))
            name = features[structure.feature[node]]
            column = column_sql(name)
            return (f"CASE WHEN {column} <= {float(structure.threshold[node])!r} "
                    f"THEN {node_sql(structure.children_left[node])} ELSE {node_sql(structure.children_right[node])} END")

        return node_sql(0)

    def leaf_coverage(self, tree: DecisionTreeClassifier, features: list[str], split: str) -> tuple[dict, dict, int]:
        """Account-days per leaf and in-scope chains reachable from each leaf, computed in Zetaris."""
        leafed = f"SELECT bank, account, day, {self.leaf_sql(tree, features)} AS leaf FROM ({self.features(split)}) f"
        volumes = {int(leaf): int(count) for leaf, count in self.zetaris.run(f"SELECT leaf, COUNT(*) FROM ({leafed}) q GROUP BY leaf", 5000)}
        cover, total = Chains(self.zetaris, log=self.log).coverage(split, leafed)
        return volumes, cover, total

    @staticmethod
    def select_leaves(volumes: dict, cover: dict, total: int, target: float) -> list[int]:
        """Greedy: add the leaf catching the most new chains per alerted account-day until the target."""
        chosen, caught = [], set()
        while total and len(caught) / total < target:
            gain = lambda leaf: len(cover.get(leaf, set()) - caught) / max(volumes[leaf], 1)
            best = max((leaf for leaf in volumes if leaf not in chosen), key=gain, default=None)
            if best is None or not cover.get(best, set()) - caught:
                break
            chosen.append(best)
            caught |= cover[best]
        return chosen

    @staticmethod
    def group_roots(tree: DecisionTreeClassifier, chosen: list[int], volumes: dict) -> list[int]:
        """Maximal subtrees whose leaves are all chosen (leaves with no account-days count as chosen)."""
        structure, chosen = tree.tree_, set(chosen)

        def leaves(node: int) -> list[int]:
            if structure.children_left[node] == structure.children_right[node]:
                return [node]
            return leaves(structure.children_left[node]) + leaves(structure.children_right[node])

        roots = []

        def walk(node: int) -> None:
            under = leaves(node)
            if all(leaf in chosen or leaf not in volumes for leaf in under) and any(leaf in chosen for leaf in under):
                roots.append(node)
            elif structure.children_left[node] != structure.children_right[node]:
                walk(structure.children_left[node])
                walk(structure.children_right[node])

        walk(0)
        return roots

    @staticmethod
    def to_program(
        tree: DecisionTreeClassifier, features: list[str], roots: list[int], rows: list[dict], population: dict
    ) -> tuple[RuleProgram, list[dict]]:
        """One AND group per chosen subtree; estimated precision re-weighted from the sample to the population."""
        structure = tree.tree_
        path = tree.decision_path(RuleLearner.matrix(rows, features))
        labels = np.array([row["label"] for row in rows])
        weight_pos = population.get(1, 0) / max(1, int(labels.sum()))
        weight_neg = population.get(0, 0) / max(1, int(len(labels) - labels.sum()))
        conditions: dict[int, list] = {}

        def walk(node: int, acc: list) -> None:
            conditions[node] = acc
            if structure.children_left[node] != structure.children_right[node]:
                name, threshold = features[structure.feature[node]], float(structure.threshold[node])
                walk(structure.children_left[node], acc + [(name, "le", threshold)])
                walk(structure.children_right[node], acc + [(name, "gt", threshold)])

        walk(0, [])
        rules, groups, stats = [], [], []
        for node in roots:
            tightest: dict[tuple, float] = {}
            for name, operator, threshold in conditions[node]:
                key = (name, operator)
                tightest[key] = min(tightest.get(key, math.inf), threshold) if operator == "le" else max(tightest.get(key, -math.inf), threshold)
            group_id, ids = f"g{node}", []
            for (name, operator), threshold in sorted(tightest.items()):
                if operator == "gt":
                    # x > t  <=>  x >= next value: exact for counts, epsilon for continuous features.
                    threshold = float(math.floor(threshold) + 1) if name in INTEGER_FEATURES else round(threshold + 1e-6, 6)
                rule_id = f"IBM-L1-{node}-{name}-{'ge' if operator == 'gt' else 'le'}"
                rules.append(RuleAtom(rule_id=rule_id, feature_id=name, operator="ge" if operator == "gt" else "le",
                                      threshold=round(max(threshold, 0.0), 6), source_refs=list(POLICY[name])))
                ids.append(rule_id)
            if not ids:  # The root itself was chosen: every account-day alerts; anchor on activity.
                rule_id = f"IBM-L1-{node}-activity_change_24h-ge"
                rules.append(RuleAtom(rule_id=rule_id, feature_id="activity_change_24h", operator="ge", threshold=0.0,
                                      source_refs=list(POLICY["activity_change_24h"])))
                ids.append(rule_id)
            groups.append(RuleGroup(group_id=group_id, operator="all", rule_ids=ids))
            members = path[:, node].toarray().ravel().astype(bool)
            positives = int(labels[members].sum())
            negatives = int(members.sum()) - positives
            estimated = (positives * weight_pos) / (positives * weight_pos + negatives * weight_neg or 1)
            stats.append({"group_id": group_id, "sample_positives": positives, "sample_negatives": negatives,
                          "estimated_population_precision": round(estimated, 5),
                          "estimated_population_windows": round(positives * weight_pos + negatives * weight_neg)})
        return RuleProgram(rules=rules, groups=groups, group_operator="any"), stats

    def recall_program(self, tree: DecisionTreeClassifier, features: list[str], rows: list[dict], population: dict) -> dict:
        """Layer-1 program meeting the development chain-recall target, with per-group coverage."""
        volumes, cover, total = self.leaf_coverage(tree, features, "development")
        chosen = self.select_leaves(volumes, cover, total, SETTINGS["development_chain_recall_target"])
        roots = self.group_roots(tree, chosen, volumes)
        program, stats = self.to_program(tree, features, roots, rows, population)
        structure = tree.tree_

        def leaves(node: int) -> list[int]:
            if structure.children_left[node] == structure.children_right[node]:
                return [node]
            return leaves(structure.children_left[node]) + leaves(structure.children_right[node])

        for node, item in zip(roots, stats):
            under = leaves(node)
            item["development_account_days"] = sum(volumes.get(leaf, 0) for leaf in under)
            item["development_chains"] = len(set().union(*(cover.get(leaf, set()) for leaf in under)))
        caught = set().union(*(cover.get(leaf, set()) for leaf in chosen)) if chosen else set()
        fit = {"development_chain_recall": len(caught) / total if total else None, "development_chains": total,
               "development_account_days": sum(volumes.get(leaf, 0) for leaf in chosen), "leaves": len(volumes),
               "chosen_leaves": len(chosen), "groups": len(roots)}
        return {"program": program, "stats": stats, "roots": {int(n): f"g{n}" for n in roots}, "fit": fit}

    @staticmethod
    def tree_view(tree: DecisionTreeClassifier, features: list[str], rows: list[dict], population: dict, roots: dict) -> list[dict]:
        """The fitted tree as nodes with sample counts and population-weighted precision; group roots name their rule."""
        structure = tree.tree_
        labels = np.array([row["label"] for row in rows])
        path = tree.decision_path(RuleLearner.matrix(rows, features))
        weight_pos = population.get(1, 0) / max(1, int(labels.sum()))
        weight_neg = population.get(0, 0) / max(1, int(len(labels) - labels.sum()))
        nodes = []
        for node in range(structure.node_count):
            members = path[:, node].toarray().ravel().astype(bool)
            positives = int(labels[members].sum())
            negatives = int(members.sum()) - positives
            leaf = structure.children_left[node] == structure.children_right[node]
            nodes.append({
                "id": node,
                "feature": None if leaf else features[structure.feature[node]],
                "threshold": None if leaf else round(float(structure.threshold[node]), 6),
                "left": None if leaf else int(structure.children_left[node]),
                "right": None if leaf else int(structure.children_right[node]),
                "sample_positives": positives,
                "sample_negatives": negatives,
                "estimated_population_windows": round(positives * weight_pos + negatives * weight_neg),
                "estimated_population_precision": round((positives * weight_pos) / (positives * weight_pos + negatives * weight_neg or 1), 5),
                "group_id": roots.get(node),
            })
        return nodes

    def chain_metrics(self, program: RuleProgram, split: str) -> dict:
        """Layer-1 effect on a whole partition, in Zetaris: chain recall, alert volume and window precision."""
        condition = condition_sql(program)
        flagged = self.zetaris.run(
            f"SELECT COUNT(*), SUM(label), COUNT(DISTINCT CONCAT(bank, ':', account)) FROM ({self.labelled(split, condition)}) j", 2
        )[0]
        totals = self.population(split)
        days = len(partition_days(split))
        flagged_sql = f"SELECT bank, account, day FROM ({self.features(split, condition)}) f"
        chain_view = Chains(self.zetaris, log=self.log)
        chains = chain_view.recall(split, flagged_sql)
        accounts = chain_view.accounts(split, flagged_sql)
        alerts, hits = int(flagged[0] or 0), int(flagged[1] or 0)
        everything = sum(totals.values())
        return {
            "split": split, "days": days, **chains, **accounts,
            "flagged_account_days": alerts, "flagged_per_day": round(alerts / days) if days else None,
            "flagged_share": alerts / everything if everything else None, "flagged_accounts": int(flagged[2] or 0),
            "window_precision": hits / alerts if alerts else 0.0,
            "window_recall": hits / totals.get(1, 0) if totals.get(1) else None,
            "account_days": everything, "positive_windows": totals.get(1, 0),
            "basis": "Chain recall: share of in-scope laundering chains with any member account alerted on any day of the partition. "
                     "Full-chain account recall: share of member accounts of recalled chains that are alerted. Chain precision: "
                     "share of alerted accounts that belong to a laundering chain (any day of the partition); window precision is per account-day.",
        }

    # ---- agent model calls ----------------------------------------------------------

    def plan(self, context: dict, fallback: dict) -> tuple[dict, str]:
        """The agent's update plan, with one repair; a recorded fallback if both fail."""
        schema = UpdatePlan.model_json_schema()
        schema["properties"]["features"]["items"] = {"type": "string", "pattern": "^[a-z0-9_]{1,40}$", "maxLength": 40}
        # Numeric bounds in the grammar dead-end the model when it wants an out-of-range value
        # (whitespace runaway); the host validates them instead and returns the error as a repair.
        for name in ("max_leaf_nodes", "min_samples_leaf", "positive_weight"):
            schema["properties"][name] = {"type": "integer"}
        # Field order matters to the grammar: right after the capacity field the model reaches for
        # other scikit-learn parameters and dead-ends. Reason first, tree capacity last.
        order = ["rationale", "features", "min_samples_leaf", "positive_weight", "max_leaf_nodes"]
        schema["properties"] = {name: schema["properties"][name] for name in order}
        schema["required"] = order
        packet = dict(context)
        for attempt in range(2):
            raw, usage = self.provider.generate(
                "You are the AML rule-learning agent for the high-recall screening layer. The rule must keep catching at "
                "least the target share of laundering chains (any member account alerted on any day); within that, fewer "
                "alerted account-days is better. Choose which registered features and how many tree leaves (8-48) the next "
                "development fit should use: more leaves usually alert less at the same recall but give more, longer rules. "
                "Use the development fit, investigation outcomes and label-checked false positives; scikit-learn fits the "
                "thresholds, you do not write them. Validation and test outcomes are not available to you. Return one JSON instance.",
                packet,
                schema,
            )
            self.record_call("rule_learner_plan", usage)
            try:
                return UpdatePlan.model_validate(raw).model_dump(), "agent"
            except ValueError as error:
                packet["repair"] = "Previous answer was invalid: " + str(error)[:300]
                self.log(json.dumps({"rule_learner_plan_invalid": attempt + 1, "error": str(error)[:300]}))
        return fallback, "fallback_after_two_invalid_agent_plans"

    def explain(self, program: RuleProgram, stats: list[dict]) -> dict:
        schema = ProgramExplanation.model_json_schema()
        schema["$defs"]["RuleExplanation"]["properties"]["group_id"] = {"type": "string", "enum": [g.group_id for g in program.groups]}
        schema["properties"]["rules"]["maxItems"] = len(program.groups)
        packet = {
            "program": program.model_dump(mode="json"),
            "development_leaf_stats": stats,
            "features": {r.feature_id: DESCRIPTIONS[r.feature_id] for r in program.rules},
            "policy_paragraphs": POLICY_TEXT,
        }
        try:
            raw, usage = self.provider.generate(
                "Explain each learned AML rule group (an AND of feature conditions) in plain language, map it to the most "
                "fitting verified AUSTRAC paragraph, and note concerns such as overfitting, proxy effects or thresholds "
                "that are research hypotheses, not regulatory limits. Do not invent metrics. Return one JSON instance.",
                packet,
                schema,
            )
            self.record_call("rule_learner_explain", usage)
            return ProgramExplanation.model_validate(raw).model_dump()
        except Exception as error:  # Explanation is advisory; publication never depends on it.
            return {"rules": [], "overall": f"Explanation unavailable: {type(error).__name__}"}

    def record_call(self, role: str, usage: dict) -> None:
        self.store.artifact("rule_model_call", {"role": role, "usage": usage, "model": self.provider.settings.model})

    # ---- versions -------------------------------------------------------------------

    def freeze(self, program: RuleProgram, *, parent: str | None, basis: dict) -> str:
        compiled = compile_program(program)
        source = json.loads((ROOT / "sources/ibm_hi_large_manifest.json").read_text())
        return self.store.artifact(
            "rule_program_candidate",
            {
                **compiled,
                "status": "frozen_candidate",
                "base_version": parent,
                "source_sha256": source["source_sha256"],
                "feature_store": "IBM_AML.agent_day_aggregates + IBM_AML.agent_day_transit + IBM_AML.agent_day_history",
                "created": time.time(),
                **basis,
            },
        )

    def gate(self, candidate: dict, base: dict) -> tuple[str, str]:
        """Recall first: the candidate must meet the chain-recall target; then it must alert less."""
        target = SETTINGS["chain_recall_target"]
        if candidate["chains"] < SETTINGS["minimum_validation_chains"]:
            return "hold", "Too few laundering chains in the comparison split"
        if candidate["chain_recall"] < target:
            return "hold", f"Chain recall {candidate['chain_recall']:.1%} is below the {target:.0%} target"
        if base.get("chain_recall") is None or base["chain_recall"] < target:
            return "accept_for_experiment", f"Meets the {target:.0%} chain-recall target, which the parent does not"
        precision = "chain_precision" if candidate.get("chain_precision") is not None and base.get("chain_precision") is not None else "window_precision"
        if candidate["flagged_account_days"] < base["flagged_account_days"] and candidate[precision] >= base[precision]:
            return "accept_for_experiment", f"Keeps the chain-recall target with fewer alerts and no lower {precision.replace('_', ' ')}"
        return "hold", "Keeps the recall target but does not alert less than the parent"

    def publish(self, candidate_id: str, evaluation: dict, alias: str) -> dict:
        candidate = self.store.read_artifact(candidate_id, "rule_program_candidate")
        if evaluation["decision"] != "accept_for_experiment":
            return {"published": False, "active_version": self.store.program_pointer(alias), "reason": evaluation["reason"]}
        number = 1 + sum(1 for _, body in self.store.artifacts("rule_program_version") if body.get("status") == "accepted_experiment")
        version = self.store.artifact(
            "rule_program_version",
            {**candidate, "status": "accepted_experiment", "parent_version": candidate["base_version"], "evaluation": evaluation,
             "version_number": number, "published_at": time.time()},
        )
        self.store.set_program_pointer(alias, version, expected=self.store.program_pointer(alias))
        return {"published": True, "active_version": version, "previous_version": candidate["base_version"], "production_eligible": False}


def training_days_note() -> dict:
    days = partition_days("development")
    return {"development_days": len(days), "first_day": days[0], "last_day": days[-1]}
