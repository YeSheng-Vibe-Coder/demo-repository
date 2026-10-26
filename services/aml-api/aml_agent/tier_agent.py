"""Tier agent: an LLM that learns a risk-tiering and priority policy for layer-1 alerts.

It works on development aggregates computed in Zetaris (the distribution of every feature
among alerts, laundering rates per feature bucket and per feature pair, the tier model's
tiers as a reference) and writes a policy: ordered segments of feature conditions, each with
a tier and a priority. The policy is compiled to SQL and applied to every alert in Zetaris,
so all ~20k alerts a day are tiered in one batch; the alerts then form a priority queue for
the senior investigator.

The agent iterates: propose a policy, see its development metrics next to the tier model's,
ask for more statistics, revise. A policy is published only if, on validation, it reaches
60% and 90% of laundering chains with no more alerts than the tier model (gate); otherwise
the tier model stays and the attempt is recorded. The agent never sees a single label, only
development aggregates; validation is used by the gate alone.
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .chains import Chains
from .contracts import StrictModel
from .feature_registry import DESCRIPTIONS, FEATURES
from .feature_store import LABEL_TABLE
from .profile import PROFILE
from .providers import ContextBudgetError
from .rule_learning import HASH_ORDER, MISSING, RuleLearner, condition_sql, day_range
from .rule_program import RuleProgram

SETTINGS = PROFILE["tier_agent"]
TIERS = ("high", "mid", "low")
ALIAS = "tier-policy-current"


class Condition(StrictModel):
    feature: str = Field(max_length=40)
    op: Literal["ge", "le"]
    value: float


class Segment(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=160)
    conditions: list[Condition] = Field(min_length=1, max_length=4)
    tier: Literal["high", "mid", "low"]
    priority: int

    @model_validator(mode="after")
    def bounded(self) -> "Segment":
        unknown = [c.feature for c in self.conditions if c.feature not in FEATURES]
        if unknown:
            raise ValueError(f"Unregistered feature {unknown[0]}; use one of: {', '.join(FEATURES)}")
        self.priority = max(0, min(100, self.priority))
        return self


class Request(StrictModel):
    features: list[str] = Field(min_length=2, max_length=2)


class TierPolicy(StrictModel):
    rationale: str = Field(min_length=1, max_length=600)
    segments: list[Segment] = Field(min_length=1, max_length=12)
    default_tier: Literal["mid", "low"]
    use_model_score: bool


class Proposal(StrictModel):
    analysis: str = Field(min_length=1, max_length=600)
    requests: list[Request] = Field(default_factory=list, max_length=3)
    policy: TierPolicy


PROMPT = """You are the AML tier agent. The team can investigate a limited number of alerts a day (2,000 or 5,000); your
priority order decides which. Judge a policy by the top-of-queue results: the share of laundering chains reached and
the share of those alerts whose account belongs to a laundering chain, at 2,000 and 5,000 alerts a day.
When COMMON LAUNDERING PATTERNS are given, build several high-risk segments
from them (one per distinct pattern, adjusted with the statistics), so the high tier covers many laundering
typologies at high precision instead of one. Layer-1 rules alert on ~20,000 account-days a day to keep laundering
recall high; most alerts are false. Your job: a policy that sorts these alerts into risk tiers (high / mid / low)
and a priority order, so investigators see laundering first. Laundering is measured by chains (connected
laundering transfers); a good policy reaches 60% and 90% of the chains with as few alerts as possible.

You get development statistics computed over all development alerts: each feature's distribution among
alerts and the laundering rate per bucket, laundering rates for feature pairs you ask for, the tier model's
tiers (a gradient-boosted reference model), and after each proposal its measured effect next to the tier
model's. Write the policy as segments: each segment is up to 4 conditions on registered features (AND), a tier,
a priority 0-100 and a reason. An alert takes the matching segment with the highest priority; unmatched alerts
get default_tier and priority 0. use_model_score breaks ties within a segment by the tier model's score, which
you may use as a reference when your own segments are not discriminating enough. Ask for up to 3 feature pairs
per round in requests. Base every threshold on the statistics shown. Return one JSON instance."""


def atom(condition: Condition) -> str:
    column = f"COALESCE(CAST({condition.feature} AS DOUBLE), {MISSING.get(condition.feature, 0.0)})"
    return f"({column} {'>=' if condition.op == 'ge' else '<='} {condition.value!r})"


def compile_policy(policy: TierPolicy, model_score: str | None) -> tuple[str, str]:
    """(tier expression, ordering expression) for one alert row."""
    ordered = sorted(policy.segments, key=lambda s: -s.priority)
    tests = [" AND ".join(atom(c) for c in s.conditions) for s in ordered]
    tier = "CASE " + " ".join(f"WHEN {t} THEN '{s.tier}'" for t, s in zip(tests, ordered)) + f" ELSE '{policy.default_tier}' END"
    rank = {"high": 3, "mid": 2, "low": 1}
    priority = "CASE " + " ".join(f"WHEN {t} THEN {rank[s.tier] * 1000 + s.priority}" for t, s in zip(tests, ordered)) \
        + f" ELSE {rank[policy.default_tier] * 1000} END"
    if policy.use_model_score and model_score:
        priority = f"({priority}) + 0.001 * (1.0 / (1.0 + EXP(-({model_score}))))"
    return tier, priority


class Evaluator:
    """Tier and ordering metrics for any (tier, priority) expressions, computed in Zetaris."""

    def __init__(self, learner: RuleLearner, program: RuleProgram):
        self.learner, self.program = learner, program
        self.chains = Chains(learner.zetaris, log=learner.log)

    def ranked(self, split: str, tier: str, priority: str) -> str:
        # in_chain: the alerted account belongs to a laundering chain of the partition (chain precision).
        chain_accounts = f"(SELECT DISTINCT account FROM {self.chains.members(split)} x)"
        return (f"SELECT f.bank, f.account, f.day, {tier} AS tier, COALESCE(l.label, 0) AS label, "
                "CASE WHEN c.account IS NULL THEN 0 ELSE 1 END AS in_chain, "
                f"ROW_NUMBER() OVER (ORDER BY {priority} DESC, MD5(CONCAT(f.bank, ':', f.account, ':', CAST(f.day AS STRING)))) AS r "
                f"FROM ({self.learner.features(split, condition_sql(self.program))}) f LEFT JOIN {LABEL_TABLE} l "
                "ON l.bank = f.bank AND l.account = f.account AND l.day = f.day "
                f"LEFT JOIN {chain_accounts} c ON c.account = CONCAT(f.bank, ':', f.account)")

    def evaluate(self, split: str, tier: str, priority: str) -> dict:
        ranked = self.ranked(split, tier, priority)
        run = self.learner.zetaris.run
        per_tier = {r[0]: (int(r[1]), int(r[2] or 0), int(r[3] or 0))
                    for r in run(f"SELECT tier, COUNT(*), SUM(label), SUM(in_chain) FROM ({ranked}) t GROUP BY tier", 10)}
        members = f"(SELECT chain, account FROM {self.chains.members(split)} x WHERE in_scope = 1)"
        first = [int(r[0]) for r in run(
            f"SELECT MIN(q.r) FROM {members} m JOIN (SELECT CONCAT(bank, ':', account) AS account, MIN(r) AS r FROM ({ranked}) t GROUP BY 1) q "
            "ON q.account = m.account GROUP BY m.chain", 100000)]
        total_chains = int(run(f"SELECT COUNT(DISTINCT chain) FROM {members} m", 2)[0][0])
        alerts = sum(c for c, _, _ in per_tier.values())
        start, end = day_range(split)
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        first.sort()

        def alerts_for(share: float) -> int | None:
            need = int(share * total_chains + 0.999)
            return first[need - 1] if need <= len(first) else None

        budgets = [int(b * days) for b in SETTINGS["budget_points_per_day"]]
        hits = run("SELECT " + ", ".join(f"SUM(CASE WHEN r <= {n} THEN label ELSE 0 END), SUM(CASE WHEN r <= {n} THEN in_chain ELSE 0 END)"
                                         for n in budgets) + f" FROM ({ranked}) t", 2)[0]
        out = {}
        for name in TIERS:
            count, positives, in_chain = per_tier.get(name, (0, 0, 0))
            out[name] = {"alerts_per_day": round(count / days), "window_precision": round(positives / count, 4) if count else 0.0,
                         "chain_precision": round(in_chain / count, 4) if count else 0.0}
        for share in (0.6, 0.9):
            need = alerts_for(share)
            out[f"alerts_per_day_for_{int(share * 100)}pct_chains"] = round(need / days) if need else None
        out["budgets"] = [{"alerts_per_day": b, "chain_recall": round(sum(1 for f in first if f <= n) / total_chains, 4),
                           "window_precision": round(int(hits[2 * k] or 0) / n, 4) if n else None,
                           "chain_precision": round(int(hits[2 * k + 1] or 0) / n, 4) if n else None}
                          for k, (b, n) in enumerate(zip(SETTINGS["budget_points_per_day"], budgets))]
        out.update({"split": split, "days": days, "alerts_per_day": round(alerts / days), "chains": total_chains})
        return out

    # ---- statistics for the agent ---------------------------------------------------

    def alerts(self) -> str:
        return (f"SELECT f.*, COALESCE(l.label, 0) AS label FROM ({self.learner.features('development', condition_sql(self.program))}) f "
                f"LEFT JOIN {LABEL_TABLE} l ON l.bank = f.bank AND l.account = f.account AND l.day = f.day")

    def cuts(self) -> dict[str, list[float]]:
        columns = ", ".join(f"PERCENTILE_APPROX(CAST({n} AS DOUBLE), ARRAY(0.2, 0.4, 0.6, 0.8, 0.95))" for n in FEATURES)
        row = self.learner.zetaris.run(f"SELECT {columns} FROM ({self.alerts()}) a", 2)[0]
        out = {}
        for name, cell in zip(FEATURES, row):
            values = json.loads(cell) if isinstance(cell, str) else cell
            out[name] = sorted(set(float(v) for v in values or []))
        return out

    def bucket(self, name: str, cuts: list[float]) -> str:
        column = f"COALESCE(CAST({name} AS DOUBLE), {MISSING.get(name, 0.0)})"
        return "CASE " + " ".join(f"WHEN {column} <= {c!r} THEN {i}" for i, c in enumerate(cuts)) + f" ELSE {len(cuts)} END"

    def feature_stats(self, cuts: dict) -> list[str]:
        lines = []
        for name in FEATURES:
            if not cuts[name]:
                continue
            rows = self.learner.zetaris.run(
                f"SELECT {self.bucket(name, cuts[name])} AS b, COUNT(*), SUM(label) FROM ({self.alerts()}) a GROUP BY 1 ORDER BY 1", 20)
            edges = cuts[name]
            parts = []
            for b, count, positives in rows:
                b = int(b)
                label = f"<={edges[b]:g}" if b < len(edges) else f">{edges[-1]:g}"
                parts.append(f"{label}: {int(count):,} alerts, {int(positives or 0) / int(count):.1%} laundering")
            lines.append(f"{name} ({DESCRIPTIONS[name][:70]}): " + "; ".join(parts))
        return lines

    def pair_stats(self, a: str, b: str, cuts: dict) -> list[str]:
        rows = self.learner.zetaris.run(
            f"SELECT {self.bucket(a, cuts[a][1::2] or cuts[a])} AS x, {self.bucket(b, cuts[b][1::2] or cuts[b])} AS y, COUNT(*), SUM(label) "
            f"FROM ({self.alerts()}) t GROUP BY 1, 2 ORDER BY 1, 2", 50)
        ea, eb = cuts[a][1::2] or cuts[a], cuts[b][1::2] or cuts[b]
        name = lambda edges, i: f"<={edges[i]:g}" if i < len(edges) else f">{edges[-1]:g}"
        return [f"{a} {name(ea, int(x))} & {b} {name(eb, int(y))}: {int(c):,} alerts, {int(p or 0) / int(c):.1%} laundering"
                for x, y, c, p in rows if int(c) >= 50]


def summary(metrics: dict) -> str:
    return ("tiers: " + ", ".join(f"{t} {metrics[t]['alerts_per_day']:,}/day at {metrics[t]['window_precision']:.1%}" for t in TIERS)
            + f"; alerts/day to reach 60% of chains {metrics['alerts_per_day_for_60pct_chains']}, 90% {metrics['alerts_per_day_for_90pct_chains']}; "
            + "; ".join(f"top {b['alerts_per_day']:,}/day reaches {b['chain_recall']:.1%} of chains, {b.get('chain_precision', 0):.1%} of those alerts in a chain"
                        for b in metrics["budgets"]))


def development_score(metrics: dict) -> float:
    """Lower is better: minus (chain recall + chain precision) at the gated daily capacities."""
    return -sum(b["chain_recall"] + (b.get("chain_precision") or 0) for b in metrics["budgets"]
                if b["alerts_per_day"] in SETTINGS["gated_capacity_per_day"])


def better_or_equal(candidate: dict, baseline: dict, tolerance: float) -> tuple[bool, str]:
    """Primary: at each gated daily investigation capacity, chain recall and chain precision are no worse."""
    worse = []
    gated = set(SETTINGS["gated_capacity_per_day"])
    for c, b in zip(candidate.get("budgets", []), baseline.get("budgets", [])):
        if c["alerts_per_day"] not in gated:
            continue
        for key in ("chain_recall", "chain_precision"):
            if c.get(key) is None or (b.get(key) is not None and c[key] < b[key] * (1 - tolerance)):
                worse.append(f"{key} at {c['alerts_per_day']:,}/day: {c.get(key)} vs tier model {b.get(key)}")
    return (not worse, "; ".join(worse) or "at every gated daily capacity, chain recall and chain precision are no worse than the tier model")


def segment_stats(evaluator: Evaluator, policy: TierPolicy) -> list[str]:
    """Development alerts and laundering rate per segment (first matching by priority), plus unmatched."""
    ordered = sorted(policy.segments, key=lambda s: -s.priority)
    label = "CASE " + " ".join(f"WHEN {' AND '.join(atom(c) for c in s.conditions)} THEN {i}" for i, s in enumerate(ordered)) + " ELSE -1 END"
    rows = evaluator.learner.zetaris.run(f"SELECT {label} AS s, COUNT(*), SUM(label) FROM ({evaluator.alerts()}) a GROUP BY 1 ORDER BY 1", 20)
    out = []
    for index, count, positives in rows:
        index, count = int(index), int(count)
        name = f"{ordered[index].name} ({ordered[index].tier}, priority {ordered[index].priority})" if index >= 0 else f"unmatched ({policy.default_tier})"
        out.append(f"{name}: {count:,} development alerts, {int(positives or 0) / count:.1%} laundering")
    return out


def proposal_schema() -> dict:
    body = Proposal.model_json_schema()
    defs = body["$defs"]
    defs["Condition"]["properties"]["feature"] = {"type": "string", "pattern": "^[a-z0-9_]{1,40}$", "maxLength": 40}
    defs["Condition"]["properties"]["value"] = {"type": "number"}
    defs["Segment"]["properties"]["priority"] = {"type": "integer"}
    defs["Request"]["properties"]["features"] = {"type": "array", "items": {"type": "string", "pattern": "^[a-z0-9_]{1,40}$", "maxLength": 40},
                                                 "minItems": 2, "maxItems": 2}
    # Reasoning first, verdict-like fields last: the grammar must not dead-end after a closing value.
    defs["Segment"]["properties"] = {k: defs["Segment"]["properties"][k] for k in ("name", "reason", "conditions", "tier", "priority")}
    defs["Segment"]["required"] = list(defs["Segment"]["properties"])
    defs["TierPolicy"]["properties"] = {k: defs["TierPolicy"]["properties"][k] for k in ("rationale", "segments", "default_tier", "use_model_score")}
    defs["TierPolicy"]["required"] = list(defs["TierPolicy"]["properties"])
    return body


def active_tiering(store, version: str, cycle) -> dict:
    """The published tier policy for this rule version if any, else the tier model: expressions and base rates."""
    from .tiering import score_sql, tier_sql

    model_id, stored = cycle.tier_model(version)
    model = stored["model"]
    pointer = store.program_pointer(ALIAS)
    if pointer:
        body = store.read_artifact(pointer, "tier_policy_version")
        if body["rule_version"] == version:
            policy = TierPolicy.model_validate(body["policy"])
            tier, priority = compile_policy(policy, score_sql(model))
            rates = {t: body["development"][t]["window_precision"] for t in TIERS}
            return {"source": f"tier agent {body['label']}", "policy_id": pointer, "tier": tier, "priority": priority,
                    "rates": rates, "model_id": model_id}
    rates = {t: stored["metrics"]["development"][t]["window_precision"] for t in TIERS}
    return {"source": "tier model", "policy_id": None, "tier": tier_sql(model), "priority": score_sql(model), "rates": rates, "model_id": model_id}


def sample(learner: RuleLearner, program: RuleProgram, tiering: dict, split: str, per_cell: int) -> list[dict]:
    """Evaluation side: `per_cell` alerts per (tier, window label) in label-blind hash order, with their priority."""
    columns = ["bank", "account", "day", *FEATURES]
    tiered = (f"SELECT f.*, {tiering['tier']} AS tier, {tiering['priority']} AS priority, COALESCE(l.label, 0) AS label FROM "
              f"({learner.features(split, condition_sql(program))}) f LEFT JOIN {LABEL_TABLE} l "
              "ON l.bank = f.bank AND l.account = f.account AND l.day = f.day")
    rows = learner.zetaris.run(
        "SELECT " + ", ".join(columns) + ", tier, priority, label FROM (SELECT t.*, ROW_NUMBER() OVER (PARTITION BY tier, label "
        f"ORDER BY {HASH_ORDER}) AS r FROM (" + tiered + f") t) s WHERE r <= {per_cell} ORDER BY priority DESC", 6 * per_cell + 1)
    out = []
    for row in rows:
        record = dict(zip(columns + ["tier", "priority", "label"], row))
        out.append({"bank": str(record["bank"]), "account": str(record["account"]), "day": str(record["day"]),
                    "features": {n: None if record[n] is None else float(record[n]) for n in FEATURES},
                    "tier": record["tier"], "priority": float(record["priority"]), "label": int(record["label"])})
    return out


def ranked(learner: RuleLearner, program: RuleProgram, tiering: dict, split: str, per_tier: dict) -> list[dict]:
    """Label-free: the highest-priority alerts of each tier (per_tier = {"high": n, "mid": n, "low": n}), by priority."""
    columns = ["bank", "account", "day", *FEATURES]
    tiered = f"SELECT f.*, {tiering['tier']} AS tier, {tiering['priority']} AS priority FROM ({learner.features(split, condition_sql(program))}) f"
    quota = " OR ".join(f"(tier = '{tier}' AND r <= {int(n)})" for tier, n in per_tier.items())
    rows = learner.zetaris.run(
        "SELECT " + ", ".join(columns) + ", tier, priority FROM (SELECT t.*, ROW_NUMBER() OVER (PARTITION BY tier ORDER BY priority DESC) AS r "
        f"FROM ({tiered}) t) s WHERE {quota} ORDER BY priority DESC", sum(per_tier.values()) + 1)
    out = []
    for row in rows:
        record = dict(zip(columns + ["tier", "priority"], row))
        out.append({"bank": str(record["bank"]), "account": str(record["account"]), "day": str(record["day"]),
                    "features": {n: None if record[n] is None else float(record[n]) for n in FEATURES},
                    "tier": record["tier"], "priority": float(record["priority"])})
    return out


class TierAgent:
    def __init__(self, cycle, provider):
        self.cycle, self.store, self.learner, self.provider = cycle, cycle.store, cycle.learner, provider
        self.say = cycle.say

    def run(self, feedback: list[str] | None = None, start_from: dict | None = None, patterns: bool = False) -> dict:
        from .rule_program import load_program, program_of
        from .tiering import score_sql, tier_sql

        version = self.store.program_pointer("learned-current")
        program = program_of(load_program(self.store, version))
        model_id, stored = self.cycle.tier_model(version)
        model = stored["model"]
        evaluator = Evaluator(self.learner, program)
        reference = evaluator.evaluate("development", tier_sql(model), score_sql(model))
        self.say(json.dumps({"tier_model_development": summary(reference)}))
        cuts = evaluator.cuts()
        packet = {"brief": {
            "OBJECTIVE": ["At 2,000 and 5,000 investigated alerts a day, maximise the share of laundering chains reached and the share of those "
                          "alerts whose account is in a laundering chain; the tier model is the bar to beat."],
            "FEATURE STATISTICS (development alerts; laundering rate per bucket)": evaluator.feature_stats(cuts),
            "TIER MODEL REFERENCE (development)": [summary(reference),
                                                   "top features: " + ", ".join(f"{i['feature']} {i['importance']:.2f}" for i in model.get("importance", [])[:6])],
        }}
        found, library = [], []
        if patterns:
            from . import investigator_training

            rows, population = self.learner.sample("development", PROFILE["rule_learning"]["development_negative_ratio"])
            found = typologies(self.learner, program, rows, population)
            packet["brief"]["COMMON LAUNDERING PATTERNS (mined from development positives by sequential covering)"] = typology_lines(found)
            library = case_library_lines(investigator_training.load())
            if library:
                packet["brief"]["CASE LIBRARY (investigated development laundering cases)"] = library + [
                    "When a segment rests on a library case, cite it in the segment reason (e.g. 'case 3')."]
            self.say(json.dumps({"patterns": len(found), "case_library": len(library)}))
        if start_from:
            packet["brief"]["CURRENT POLICY (revise it)"] = [json.dumps(start_from["policy"])[:3000], f"its development effect: {summary(start_from['development'])}"]
        if feedback:
            packet["brief"]["INVESTIGATED DEVELOPMENT CASES WHERE THE TIER DISAGREED WITH THE OUTCOME"] = feedback[:30]
        schema, rounds, best = proposal_schema(), [], None
        for index in range(SETTINGS["rounds"]):
            proposal, errors = None, []
            for _ in range(2):
                try:
                    raw, _ = self.provider.generate(PROMPT, packet, schema)
                except ContextBudgetError as error:
                    errors.append(str(error)[:300])
                    break
                try:
                    if "_invalid_response" in raw:
                        raise ValueError(raw["_invalid_response"])
                    proposal = Proposal.model_validate(raw)
                    break
                except ValueError as error:
                    errors.append(str(error)[:300])
                    packet["repair"] = str(error)[:400]
            packet.pop("repair", None)
            if proposal is None:
                rounds.append({"round": index + 1, "errors": errors})
                continue
            tier, priority = compile_policy(proposal.policy, score_sql(model))
            metrics = evaluator.evaluate("development", tier, priority)
            segments = segment_stats(evaluator, proposal.policy)
            answers = [line for request in proposal.requests if all(f in cuts and cuts[f] for f in request.features)
                       for line in evaluator.pair_stats(request.features[0], request.features[1], cuts)][:40]
            # Development objective: chain recall plus chain precision at the gated capacities (higher is better).
            score = development_score(metrics)
            rounds.append({"round": index + 1, "proposal": proposal.model_dump(), "development": metrics, "segments": segments, "score": score})
            # Only a real grading policy can be chosen: several segments, and every tier populated on development.
            usable = len(proposal.policy.segments) >= 3 and all(metrics[t]["alerts_per_day"] > 0 for t in TIERS)
            rounds[-1]["eligible"] = usable
            if usable and (best is None or score < best["score"]):
                best = rounds[-1]
            self.say(json.dumps({"tier_agent_round": index + 1, "development": summary(metrics)}))
            # Only the two latest round results stay in the brief (the input is bounded; the best policy is kept here).
            packet["brief"].pop(f"ROUND {index - 1} RESULT", None)
            packet["brief"][f"ROUND {index + 1} RESULT"] = [
                f"your policy: {summary(metrics)}", f"tier model: {summary(reference)}", *segments,
                *(["pair statistics you asked for:"] + answers if answers else []),
            ]
        record = {"rule_version": version, "tier_model": model_id, "rounds": rounds, "created": time.time(),
                  "development_reference": reference, "trigger": "investigation_feedback" if feedback else "initial",
                  "feedback_cases": len(feedback or []), "parent": (start_from or {}).get("id"), "patterns": found,
                  "case_library": library}
        if best is None:
            record.update(decision="hold", reason="No valid policy from the agent")
            return self.publish(record, None)
        policy = TierPolicy.model_validate(best["proposal"]["policy"])
        tier, priority = compile_policy(policy, score_sql(model))
        validation = evaluator.evaluate("validation", tier, priority)
        baseline = evaluator.evaluate("validation", tier_sql(model), score_sql(model))
        better, comparison = better_or_equal(validation, baseline, SETTINGS["gate_tolerance"])
        self.say(json.dumps({"tier_agent_validation": summary(validation), "tier_model_validation": summary(baseline), "better_than_model": better}))
        record.update(policy=policy.model_dump(), development=best["development"], validation=validation, validation_reference=baseline,
                      best_round=best["round"], comparison_with_tier_model=comparison, better_than_tier_model=better)
        # The tier agent is the primary tiering; the tier model is its reference input. A new policy replaces the
        # current one when it scores better on development (validation is reported, never used to choose).
        current = self.store.program_pointer(ALIAS)
        current_score = None
        if current:
            body = self.store.read_artifact(current, "tier_policy_version")
            policy_now = body.get("policy") or {}
            eligible = len(policy_now.get("segments", [])) >= 3 and all(body["development"][t]["alerts_per_day"] > 0 for t in TIERS) \
                if body.get("development") else False
            if body.get("rule_version") == version and eligible:
                current_score = development_score(body["development"])
        replace = current_score is None or best["score"] <= current_score
        record.update(decision="publish" if replace else "keep_current",
                      reason="better development objective than the current policy" if replace else "current policy scores better on development")
        return self.publish(record, policy if replace else None)

    def publish(self, record: dict, policy) -> dict:
        existing = [body for _, body in self.store.artifacts("tier_policy_version")]
        record["label"] = f"T{len(existing) + 1}"
        record["status"] = "published" if policy is not None else "held"
        identifier = self.store.artifact("tier_policy_version", record)
        if policy is not None:
            self.store.set_program_pointer_any(ALIAS, identifier)
        record["id"] = identifier
        return record


def typologies(learner: RuleLearner, program: RuleProgram, rows: list[dict], population: dict, count: int = 12) -> list[dict]:
    """Common laundering patterns among layer-1 alerts, mined from the development sample by sequential covering.

    A shallow tree finds the purest laundering branches; their positives are removed and the tree is
    refitted, so each pass surfaces a different pattern. Precision and volume are re-weighted from the
    label-enriched sample to the natural development population.
    """
    import numpy as np
    from sklearn.tree import DecisionTreeClassifier

    from .rule_learning import atom_true

    def alerted(row: dict) -> bool:
        return any(all(atom_true(a, row) for a in program.rules if a.rule_id in g.rule_ids) for g in program.groups)

    pool = [r for r in rows if alerted(r)]
    features = list(FEATURES)
    labels_all = np.array([r["label"] for r in rows])
    weight_pos = population.get(1, 0) / max(1, int(labels_all.sum()))
    weight_neg = population.get(0, 0) / max(1, int(len(labels_all) - labels_all.sum()))
    start, end = day_range("development")
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    total_pos = sum(r["label"] for r in pool) * weight_pos
    found, remaining = [], list(pool)
    for _ in range(6):
        if len(found) >= count or sum(r["label"] for r in remaining) < 200:
            break
        x = RuleLearner.matrix(remaining, features)
        y = np.array([r["label"] for r in remaining])
        tree = DecisionTreeClassifier(max_depth=4, min_samples_leaf=150, class_weight="balanced", random_state=0).fit(x, y)
        structure, leaves = tree.tree_, tree.apply(x)
        paths: dict[int, list] = {}

        def walk(node: int, acc: list) -> None:
            if structure.children_left[node] == structure.children_right[node]:
                paths[node] = acc
                return
            name, threshold = features[structure.feature[node]], float(structure.threshold[node])
            walk(structure.children_left[node], acc + [(name, "le", threshold)])
            walk(structure.children_right[node], acc + [(name, "gt", threshold)])

        walk(0, [])
        scored = []
        for leaf, conditions in paths.items():
            members = leaves == leaf
            positives, negatives = int(y[members].sum()), int(members.sum() - y[members].sum())
            precision = positives * weight_pos / max(1e-9, positives * weight_pos + negatives * weight_neg)
            scored.append((precision, leaf, conditions, positives, negatives))
        scored.sort(key=lambda item: -item[0])
        picked = [s for s in scored if s[3] >= 100][:3]
        if not picked:
            break
        covered = np.zeros(len(remaining), dtype=bool)
        for precision, leaf, conditions, positives, negatives in picked:
            tight: dict = {}
            for name, op, threshold in conditions:
                tight[(name, op)] = min(tight.get((name, op), 1e18), threshold) if op == "le" else max(tight.get((name, op), -1e18), threshold)
            found.append({
                "conditions": [{"feature": n, "op": "le" if o == "le" else "ge", "value": round(t if o == "le" else t + 1e-6, 4)} for (n, o), t in sorted(tight.items())],
                "precision": round(precision, 3),
                "alerts_per_day": round((positives * weight_pos + negatives * weight_neg) / days),
                "share_of_laundering_alerts": round(positives * weight_pos / max(1e-9, total_pos), 3),
            })
            covered |= leaves == leaf
        remaining = [r for r, c in zip(remaining, covered) if not (c and r["label"])]
    return found[:count]


def typology_lines(found: list[dict]) -> list[str]:
    return [f"P{i}: " + " AND ".join(f"{c['feature']} {'>=' if c['op'] == 'ge' else '<='} {c['value']:g}" for c in p["conditions"])
            + f" | est. precision {p['precision']:.0%}, ~{p['alerts_per_day']:,} alerts/day, {p['share_of_laundering_alerts']:.0%} of laundering alerts"
            for i, p in enumerate(found, 1)]


def case_library_lines(config: dict | None, limit: int = 12) -> list[str]:
    """Investigated development laundering cases with the evidence the team found."""
    if not config:
        return []
    cases = [c for c in config.get("reference_cases", []) if c["outcome"] == "laundering"][:limit]
    return [f"case {n}, tier {c['tier']}: " + ", ".join(f"{k}={v:g}" for k, v in c["features"].items() if v not in (None, 0, 0.0))[:200]
            + (f" | found: {c['key_findings'][0][:120]}" if c.get("key_findings") else "") for n, c in enumerate(cases, 1)]
