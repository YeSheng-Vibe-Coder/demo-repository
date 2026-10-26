import pytest
from aml_agent.feature_registry import FEATURES, POLICY
from aml_agent.feature_store import FeatureStore, features_relation
from aml_agent.rule_learning import RuleLearner, atom_true, condition_sql, days_clause, score_sql
from aml_agent.rule_program import RuleAtom, RuleGroup, RuleProgram, compile_program, reference_program
from aml_agent.zetaris import DataError


def rows():
    """Positives: many sources and a burst; negatives: quiet accounts."""
    data = []
    for index in range(40):
        data.append({"bank": "1", "account": f"P{index}", "day": "2022-08-05", "label": 1,
                     **{name: 0.0 for name in FEATURES}, "fan_in_48h": 4.0 + index % 3, "activity_change_24h": 6.0})
    for index in range(160):
        data.append({"bank": "1", "account": f"N{index}", "day": "2022-08-05", "label": 0,
                     **{name: 0.0 for name in FEATURES}, "fan_in_48h": float(index % 3), "activity_change_24h": 1.0,
                     "transit_gap_hours": None})
    return data


def test_tree_becomes_a_compilable_program_with_population_weighted_groups():
    learner = RuleLearner(store=None, provider=None, feature_store=object())
    features = ["fan_in_48h", "activity_change_24h", "transit_gap_hours"]
    tree = learner.fit(rows(), features, 8, 5, 1)
    positive_leaf = int(tree.apply(learner.matrix(rows()[:1], features))[0])
    structure = tree.tree_
    volumes = {n: 1 for n in range(structure.node_count) if structure.children_left[n] == structure.children_right[n]}
    roots = learner.group_roots(tree, [positive_leaf], volumes)
    program, stats = learner.to_program(tree, features, roots, rows(), {1: 400, 0: 160000})
    compile_program(program)
    # Every atom carries the feature's verified policy paragraph.
    assert all(set(rule.source_refs) == set(POLICY[rule.feature_id]) for rule in program.rules)
    # 40 sampled positives stand for 400 and 0 negatives for 160000: precision stays 1.0.
    assert stats[0]["estimated_population_precision"] == 1.0
    # Count thresholds become exact integer bounds (x > 2.5 -> x >= 3).
    assert all(float(r.threshold).is_integer() for r in program.rules if r.feature_id == "fan_in_48h" and r.operator == "ge")
    positive = rows()[0]
    assert any(all(atom_true(r, positive) for r in program.rules if r.rule_id in g.rule_ids) for g in program.groups)


def test_leaves_are_chosen_for_chain_coverage_per_alert_until_the_target():
    volumes = {1: 10, 2: 1000, 3: 50, 4: 5}
    cover = {1: {"a", "b"}, 2: {"a", "b", "c", "d"}, 3: {"c"}, 4: set()}
    # Leaf 1 is cheapest per chain, then 3; the big leaf 2 is needed only for chain d.
    assert RuleLearner.select_leaves(volumes, cover, 4, 0.75) == [1, 3]
    assert RuleLearner.select_leaves(volumes, cover, 4, 1.0) == [1, 3, 2]
    # A target that cannot be reached stops when no leaf adds a chain.
    assert RuleLearner.select_leaves(volumes, cover, 5, 1.0) == [1, 3, 2]


def test_sibling_leaves_merge_into_one_rule_group():
    learner = RuleLearner(store=None, provider=None, feature_store=object())
    features = ["fan_in_48h", "activity_change_24h", "transit_gap_hours"]
    tree = learner.fit(rows(), features, 8, 5, 1)
    structure = tree.tree_
    leaves = [n for n in range(structure.node_count) if structure.children_left[n] == structure.children_right[n]]
    # Choosing every leaf collapses to the root: one group, not one per leaf.
    assert learner.group_roots(tree, leaves, {leaf: 1 for leaf in leaves}) == [0]


def test_gate_puts_chain_recall_before_alert_volume():
    learner = RuleLearner(store=None, provider=None, feature_store=object())
    base = {"chains": 5000, "chain_recall": 0.98, "flagged_account_days": 1000, "window_precision": 0.02}
    assert learner.gate({**base, "chain_recall": 0.95, "flagged_account_days": 10}, base)[0] == "hold"
    assert learner.gate({**base, "flagged_account_days": 900, "window_precision": 0.03}, base)[0] == "accept_for_experiment"
    assert learner.gate({**base, "flagged_account_days": 1100}, base)[0] == "hold"
    assert learner.gate({**base, "flagged_account_days": 5000}, {**base, "chain_recall": 0.5})[0] == "accept_for_experiment"


def test_version_diff_reports_added_removed_and_threshold_changes():
    from aml_agent.rule_versions import diff

    def program(groups):
        rules, out = [], []
        for gid, conds in groups.items():
            ids = []
            for feature, op, threshold in conds:
                rid = f"IBM-{gid}-{feature}-{op}"
                rules.append({"rule_id": rid, "feature_id": feature, "operator": op, "threshold": threshold})
                ids.append(rid)
            out.append({"group_id": gid, "rule_ids": ids})
        return {"rules": rules, "groups": out}

    old = program({"a": [("fan_in_48h", "ge", 3), ("activity_change_24h", "ge", 1)], "b": [("currency_count_24h", "ge", 2)]})
    new = program({"x": [("fan_in_48h", "ge", 4), ("activity_change_24h", "ge", 1)], "y": [("transit_gap_hours", "le", 2)]})
    change = diff(old, new)
    assert change["changed"][0]["thresholds"] == [{"condition": "fan_in_48h ≥", "from": 3, "to": 4}]
    assert [g["group"] for g in change["added"]] == ["y"] and [g["group"] for g in change["removed"]] == ["b"]


def test_sql_and_python_rule_semantics_agree_on_missing_and_boundaries():
    program = RuleProgram(
        rules=[
            RuleAtom(rule_id="IBM-A", feature_id="transit_gap_hours", operator="le", threshold=24, source_refs=["P03"]),
            RuleAtom(rule_id="IBM-B", feature_id="fan_in_48h", operator="ge", threshold=3, source_refs=["P02"]),
        ],
        groups=[RuleGroup(group_id="g", operator="all", rule_ids=["IBM-A", "IBM-B"])],
        group_operator="any",
    )
    sql = condition_sql(program)
    # A missing transit gap means no pair within 48h: encoded as 49h in SQL and Python.
    assert "COALESCE(transit_gap_hours, 49.0) <= 24" in sql and "COALESCE(fan_in_48h, 0) >= 3" in sql
    assert not atom_true(program.rules[0], {"transit_gap_hours": None})
    assert atom_true(program.rules[0], {"transit_gap_hours": 24.0})
    assert atom_true(program.rules[1], {"fan_in_48h": 3.0}) and not atom_true(program.rules[1], {"fan_in_48h": 2.0})
    assert "THEN 0.3" in score_sql(program, {"g": 0.3}) and score_sql(reference_program(), None) == "0.0"


def test_queries_stay_inside_partitions_and_never_touch_labels_for_features():
    assert days_clause("development") == "g.day >= DATE '2022-08-04' AND g.day < DATE '2022-09-03'"
    relation = features_relation(days_clause("validation"))
    assert "is_laundering" not in relation and "agent_eval" not in relation
    for sql in (FeatureStore.aggregate_sql("2022-08-04"), FeatureStore.transit_sql("2022-08-04")):
        assert "is_laundering" not in sql
        # Nothing at or after the window cutoff (day + 24h) is read.
        assert "transaction_time < TIMESTAMP '2022-08-05 00:00:00'" in sql
    assert "is_laundering" in FeatureStore.label_sql("2022-08-04")


def test_feature_store_writes_only_its_own_shards():
    store = FeatureStore.__new__(FeatureStore)
    with pytest.raises(DataError):
        store.run("INSERT OVERWRITE DIRECTORY 'file:///home/zetaris/other' USING parquet SELECT 1")
    with pytest.raises(DataError):
        store.run("DROP TABLE IBM_AML.hi_large_transactions")


def test_learned_features_are_registered_with_policy_provenance():
    program = RuleProgram(
        rules=[RuleAtom(rule_id="IBM-X", feature_id="round_trip_48h", operator="ge", threshold=1, source_refs=["P02"])],
        groups=[RuleGroup(group_id="g", operator="all", rule_ids=["IBM-X"])],
        group_operator="any",
    )
    with pytest.raises(ValueError, match="P03"):
        compile_program(program)


def test_stored_tier_model_scores_like_scikit_learn_and_compiles_to_sql():
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    from aml_agent.tiering import export_trees, score_rows, score_sql, tier_sql

    data = rows()
    features = ["fan_in_48h", "activity_change_24h", "transit_gap_hours"]
    x = RuleLearner.matrix(data, features)
    y = np.array([r["label"] for r in data])
    model = GradientBoostingClassifier(n_estimators=5, max_depth=2, random_state=0).fit(x, y)
    stored = export_trees(model, features, x)
    # Missing values take the same defaults in the Python and the SQL form (49h for a missing transit gap).
    assert np.allclose(score_rows(stored, data), model.decision_function(x))
    assert score_rows(stored, [{**data[0], "transit_gap_hours": None}])[0] == score_rows(stored, [{**data[0], "transit_gap_hours": 49.0}])[0]
    assert "COALESCE(CAST(" in score_sql(stored)
    assert tier_sql({**stored, "cutoffs": {"high": 1.0, "mid": 0.0}}).startswith("CASE WHEN")


def test_verdict_threshold_keeps_case_recall_with_fewest_false_reports(monkeypatch):
    from aml_agent import investigator_training as training

    monkeypatch.setitem(training.SETTINGS, "case_recall_target", 0.9)
    cases = ([{"tier": "high", "window_label": 1, "suspicion": s} for s in ("high",) * 9 + ("medium",)]
             + [{"tier": "high", "window_label": 0, "suspicion": s} for s in ("low",) * 6 + ("medium",) * 3 + ("high",)])
    chosen = training.thresholds(cases)["high"]["chosen"]
    # 'high' keeps 9/10 laundering cases (meets 90%) and reports only 1/10 normal cases.
    assert chosen["report_at_or_above"] == "high" and chosen["normal_cases_reported"] == 1


def test_reference_cases_never_include_the_case_under_investigation():
    from aml_agent import investigator_training as training

    base = {name: 0.0 for name in FEATURES}
    config = {"reference_cases": [
        {"account": "1:A", "day": "2022-08-05", "tier": "high", "outcome": "laundering", "features": base},
        {"account": "1:B", "day": "2022-08-05", "tier": "high", "outcome": "laundering", "features": base},
        {"account": "1:C", "day": "2022-08-06", "tier": "high", "outcome": "normal", "features": base},
    ]}
    picked = training.similar(config, "high", base, ("1:A", "2022-08-05"), None)
    assert all(c["account"] != "1:A" for c in picked) and {c["outcome"] for c in picked} == {"laundering", "normal"}


def test_tier_policy_compiles_highest_priority_segment_first_and_rejects_unknown_features():
    from aml_agent.tier_agent import TierPolicy, compile_policy

    policy = TierPolicy.model_validate({"rationale": "x", "default_tier": "low", "use_model_score": False, "segments": [
        {"name": "a", "reason": "r", "conditions": [{"feature": "fan_in_48h", "op": "ge", "value": 3}], "tier": "mid", "priority": 40},
        {"name": "b", "reason": "r", "conditions": [{"feature": "history_active_days_7d", "op": "le", "value": 0}], "tier": "high", "priority": 150},
    ]})
    tier, priority = compile_policy(policy, None)
    assert tier.index("history_active_days_7d") < tier.index("fan_in_48h")  # priority 150 is clamped to 100 and checked first
    assert policy.segments[1].priority == 100 and "ELSE 1000" in priority
    with pytest.raises(ValueError, match="Unregistered feature"):
        TierPolicy.model_validate({"rationale": "x", "default_tier": "low", "use_model_score": True, "segments": [
            {"name": "a", "reason": "r", "conditions": [{"feature": "label", "op": "ge", "value": 1}], "tier": "high", "priority": 1}]})


def test_tier_gate_checks_chain_recall_and_chain_precision_at_each_capacity():
    from aml_agent.tier_agent import better_or_equal

    base = {"budgets": [{"alerts_per_day": 2000, "chain_recall": 0.38, "chain_precision": 0.50},
                        {"alerts_per_day": 5000, "chain_recall": 0.62, "chain_precision": 0.40},
                        {"alerts_per_day": 10000, "chain_recall": 0.82, "chain_precision": 0.30}]}
    better = {"budgets": [{**b, "chain_recall": b["chain_recall"] + 0.01} for b in base["budgets"]]}
    assert better_or_equal(better, base, 0.02)[0]
    # Worse precision at 2,000/day fails even if recall is higher; the 10,000/day point is not gated.
    worse = {"budgets": [{**base["budgets"][0], "chain_recall": 0.45, "chain_precision": 0.40}, base["budgets"][1],
                         {**base["budgets"][2], "chain_recall": 0.1}]}
    ok, reason = better_or_equal(worse, base, 0.02)
    assert not ok and "chain_precision at 2,000/day" in reason
