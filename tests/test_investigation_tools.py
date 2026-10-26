from types import SimpleNamespace

import pytest
from aml_agent.candidates import candidate_transactions
from aml_agent.contracts import Action, StepRequest
from aml_agent.store import Store
from aml_agent.tools import Tools
from aml_agent.workflow import Workflow, planner_schema
from aml_agent.zetaris import DataError, Zetaris
from test_workflow import FakeProvider, case


class ScriptedSource(Zetaris):
    """Real Zetaris query builders with scripted source responses, one per call."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def execute(self, sql, limit):
        self.calls.append((sql, limit))
        return self.responses.pop(0) if self.responses else []


def tx(identifier, time, source, target, amount="100.00", currency="Euro"):
    return [identifier, time, *source.split(":"), *target.split(":"), amount, currency, amount, currency]


def ledger_row(identifier, time, source, target, amount="100.00", currency="Euro"):
    keys = ["transaction_id", "transaction_time", "from_bank", "from_account", "to_bank",
            "to_account", "amount_received", "receiving_currency", "amount_paid", "payment_currency"]
    return dict(zip(keys, tx(identifier, time, source, target, amount, currency)))


def act(tool, **arguments):
    return Action(tool=tool, arguments=arguments, expected_observation="test", question_id=1)


def test_profile_is_one_scan_with_counterparty_distribution(tmp_path):
    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    source = ScriptedSource(
        [
            ["incoming", "001", "0002", "Euro", "3", "2", "30.00", "2022-08-04 01:00:00.0",
             "2022-08-04 05:00:00.0", "1", "000:0001", "2", "20.00"],
            ["incoming", "001", "0002", "Euro", "3", "2", "30.00", "2022-08-04 01:00:00.0",
             "2022-08-04 05:00:00.0", "2", "000:0009", "1", "10.00"],
        ]
    )
    result = Tools(source).execute(act("summarize_account", account_refs=["001:0002"]), state, lambda: None)
    sql = source.calls[0][0]
    # One profile scan plus one feature-store lookup for the profiled account.
    assert len(source.calls) == 2 and "is_laundering" not in sql
    assert "IBM_AML.agent_day_aggregates" in source.calls[1][0] and "agent_eval" not in source.calls[1][0]
    assert "CROSS JOIN" in sql and "ROW_NUMBER()" in sql and sql.count("aml.transactions") == 1
    group = result["accounts"]["001:0002"]["incoming"][0]
    assert group["count"] == "3" and group["counterparties"] == "2"
    assert [c["account_ref"] for c in group["top_counterparties"]] == ["000:0001", "000:0009"]
    assert result["accounts"]["001:0002"]["outgoing"] == []
    assert result["registered_account_refs"] == ["000:0001", "000:0009"]
    source.responses = [[["incoming", "999", "x", "Euro", "1", "1", "1", "2022-08-04 01:00:00.0",
                          "2022-08-04 01:00:00.0", "1", "a:b", "1", "1"]]]
    with pytest.raises(DataError):
        source.profile([case().account], "2022-08-04", "2022-08-05", lambda: None)


def test_flow_iterates_hops_in_zetaris_with_amount_continuity(tmp_path):
    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    source = ScriptedSource(
        [[0, *tx(1, "2022-08-04 01:00:00.0", "001:0002", "002:0003", "100.00")]],
        [[0, *tx(2, "2022-08-04 03:00:00.0", "002:0003", "003:0004", "97.00")]],
        [[0, *tx(3, "2022-08-04 04:00:00.0", "003:0004", "001:0002", "95.00")]],
    )
    result = Tools(source).execute(
        act("check_structure", account_ref="001:0002", direction="downstream", depth=3),
        state,
        lambda: None,
    )
    # Three hops, then the payment-context and endpoint-feature enrichment queries.
    assert len(source.calls) == 5
    context_sql = source.calls[3][0]
    assert "a.transaction_time < f.at" in context_sql and "is_laundering" not in context_sql
    hop = source.calls[1][0]
    assert hop.startswith("WITH frontier AS (SELECT * FROM VALUES (0, '002', '0003'")
    assert "t.amount_paid >= f.amount * 0.05" in hop and "is_laundering" not in hop
    route = result["routes"][0]
    assert route["transaction_ids"] == ["1", "2", "3"]
    assert route["gap_hours"] == [2.0, 1.0] and route["amount_retained"] == "0.9500"
    assert route["returns_to_origin"] and result["returns_to_origin"]
    assert {row["transaction_id"] for row in result["rows"]} == {"1", "2", "3"}
    assert "003:0004" in result["registered_account_refs"]


def test_flow_rejects_broken_linkage_and_reports_upstream_chronologically():
    broken = ScriptedSource(
        [[0, *tx(1, "2022-08-04 01:00:00.0", "001:0002", "002:0003")]],
        [[0, *tx(2, "2022-08-04 03:00:00.0", "999:0003", "003:0004")]],
    )
    with pytest.raises(DataError):
        broken.flow(case().account, "downstream", "2022-08-04", "2022-08-05", 24, 2, lambda: None)
    upstream = ScriptedSource(
        [[0, *tx(9, "2022-08-04 05:00:00.0", "000:0001", "001:0002")]],
        [[0, *tx(8, "2022-08-04 02:00:00.0", "003:0004", "000:0001", "120.00")]],
    )
    result = upstream.flow(case().account, "upstream", "2022-08-04", "2022-08-05", 24, 2, lambda: None)
    assert result["routes"][0]["transaction_ids"] == ["8", "9"]
    assert result["routes"][0]["accounts"] == ["003:0004", "000:0001", "001:0002"]


def test_provably_empty_direction_is_short_circuited_without_sql(tmp_path):
    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    state["evidence"]["ev_profile"] = {
        "accounts": {"001:0002": {"incoming": [{"currency": "Euro"}], "outgoing": []}},
        "complete": True,
        "window": {"start": "2022-08-04T00:00:00", "end": "2022-08-05T00:00:00"},
    }

    def no_sql():
        raise AssertionError("A provably empty scope must not dispatch SQL")

    result = Tools(ScriptedSource()).execute(
        act("check_structure", account_ref="001:0002", direction="downstream"), state, no_sql
    )
    assert result["status"] == "empty_by_prior_evidence" and result["basis_evidence_ref"] == "ev_profile"
    # Partial evidence never proves absence.
    state["evidence"]["ev_profile"]["complete"] = False
    source = ScriptedSource([])
    Tools(source).execute(
        act("check_structure", account_ref="001:0002", direction="downstream"), state, lambda: None
    )
    assert len(source.calls) == 1


def test_alert_prefetch_registers_support_before_first_planner_call(tmp_path):
    class AlertSource(ScriptedSource):
        def transactions(self, account, start, end, direction, reserve, cap, other=None):
            reserve()
            self.calls.append(("detail", start, end))
            rows = [
                ledger_row(str(index), f"2022-08-04T0{index}:00:00", f"000:000{index}", "001:0002", "10")
                for index in range(1, 4)
            ]
            return {"rows": rows, "complete": True}

    seen = []

    class Recording(FakeProvider):
        def generate(self, system, packet, schema):
            seen.append(packet)
            return super().generate(system, packet, schema)

    store = Store(tmp_path / "runs.sqlite")
    rule = {"rule_id": "IBM-REF-FANIN", "feature_id": "fan_in_24h", "operator": "ge", "threshold": 3}
    state = store.create(
        case().model_dump(mode="json"),
        {
            "program": {"rules": [rule]},
            "alert": {"triggered_rule_ids": ["IBM-REF-FANIN"], "features": {"fan_in_24h": 3}},
        },
    )
    lease = store.claim(state["id"], "worker")
    source = AlertSource()
    state = Workflow(store, Recording(), Tools(source)).step(
        state["id"],
        StepRequest(expected_state_version=0, action_id="a", worker_id="worker", fence=lease["fence"]),
    )
    assert source.calls[0] == ("detail", "2022-08-03T00:00:00", "2022-08-05T00:00:00")
    assert state["observations"][0]["tool"] == "get_alert_context"
    evidence = state["evidence"][state["observations"][0]["evidence_ref"]]
    assert evidence["rule_support"][0]["distinct_counterparties_observed"] == 3
    # Supporting counterparties become queryable accounts, not a dead end.
    assert set(evidence["registered_account_refs"]) == {"000:0001", "000:0002", "000:0003"}
    first = seen[0]
    brief = first["brief"]
    assert len(brief["TRANSACTIONS (id | time | from > to | paid | context)"]) == 3
    # Candidates are listed with their observed signals only: no host score.
    candidates = brief["CANDIDATES (structural signals; judge each in finish)"]
    assert len(candidates) == 3 and candidates[0].endswith("supports_rule")
    assert first["candidate_ids"] and any("Rule IBM-REF-FANIN" in line for line in brief["LATEST OBSERVATIONS"])


def test_candidates_come_from_routes_and_every_one_must_be_judged(tmp_path):
    from aml_agent.contracts import DECISIONS

    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    rows = [
        ledger_row("1", "2022-08-04T01:00:00", "000:0001", "001:0002", "100.00"),
        ledger_row("2", "2022-08-04T03:00:00", "001:0002", "002:0003", "98.00"),
        ledger_row("3", "2022-08-04T09:00:00", "004:0005", "001:0002", "5.00"),
    ]
    state["evidence"]["ev_flow"] = {
        "rows": rows,
        "complete": True,
        "routes": [
            {"transaction_ids": ["1", "2"], "accounts": ["000:0001", "001:0002", "002:0003"],
             "gap_hours": [2.0], "amount_retained": "0.9800", "returns_to_origin": False}
        ],
        "convergence_accounts": [],
    }
    state.update(plan=["Where did funds go?", "What is unknown?"], plan_version=1)
    candidates = candidate_transactions(state)
    assert [c["transaction_id"] for c in candidates] == ["1", "2"]
    assert set(candidates[0]["signals"]) == {"on_route", "amount_retained_high", "fast_hop"}
    finish = planner_schema(state)["$defs"]["FinishDecision"]
    assert "candidate_decisions" in finish["required"]
    assert finish["properties"]["candidate_decisions"]["minItems"] == 2
    decision = DECISIONS.validate_python(
        {
            "schema_version": "planner/1.0", "decision": "finish", "based_on_state_version": 0,
            "plan_version": 1, "brief_reason": "Route kept 98% within 2h", "evidence_refs": ["ev_flow"],
            "conclusion": "suspicious", "stop_reason": "Questions answered",
            "suspect_transaction_ids": ["1"],
            "candidate_decisions": [
                {"transaction_id": "1", "decision": "confirm", "reason": "Fast pass-through", "evidence_refs": ["ev_flow"]},
                {"transaction_id": "2", "decision": "confirm", "reason": "Onward leg", "evidence_refs": ["ev_flow"]},
            ],
        }
    )
    with pytest.raises(ValueError, match="confirmed candidate"):
        Workflow.validate_decision(decision, state, 0)
    decision.suspect_transaction_ids = ["1", "2"]
    Workflow.validate_decision(decision, state, 0)
    decision.candidate_decisions = decision.candidate_decisions[:1]
    with pytest.raises(ValueError, match="every candidate"):
        Workflow.validate_decision(decision, state, 0)


def test_replan_reason_is_recorded_not_used_as_a_gate(tmp_path):
    from aml_agent.contracts import DECISIONS

    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    state.update(plan=["Q one", "Q two"], plan_version=1)
    state["evidence"]["ev_full"] = {
        "rows": [ledger_row("1", "2022-08-04T01:00:00", "000:0001", "001:0002")],
        "complete": True,
    }
    state["observations"] = [
        {"tool": "query_account_transactions", "arguments": {}, "evidence_ref": "ev_full"}
    ]
    decision = DECISIONS.validate_python(
        {
            "schema_version": "planner/1.0", "decision": "replan", "reason": "other",
            "based_on_state_version": 0, "plan_version": 2, "previous_plan_version": 1,
            "brief_reason": "change", "evidence_refs": [], "plan_patch": ["Q new"],
            "next_action": {"tool": "summarize_account", "arguments": {"account_refs": ["001:0002"]},
                            "expected_observation": "x", "question_id": 1},
        }
    )
    Workflow.validate_decision(decision, state, 0)
    # Only the anti-loop guard remains: a second replan needs a new observation.
    state["last_replan_observation_count"] = 1
    with pytest.raises(ValueError, match="new observation"):
        Workflow.validate_decision(decision, state, 0)

def test_packet_exposes_questions_frontier_and_eight_tools(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    state["accounts"]["000:0001"] = {"bank": "000", "account": "0001"}
    state["questions"] = [{"id": 1, "text": "Where from?", "status": "open", "evidence_refs": [], "note": ""}]
    packet = Workflow(store, FakeProvider(), Tools(SimpleNamespace())).packet(state, "older fact A [ev_1]\nolder fact B [ev_2]")
    brief = packet["brief"]
    assert packet["question_status"] == [{"id": 1, "status": "open"}]
    assert brief["QUESTIONS"] == ["Q1 [open] Where from?"]
    assert brief["ACCOUNTS"] == ["001:0002 [not inspected]", "000:0001 [not inspected]"]
    # H-MEM recall arrives as plain fact lines, not nested JSON.
    assert brief["CASE MEMORY"] == ["older fact A [ev_1]", "older fact B [ev_2]"]
    tools = [e["properties"]["tool"]["const"] for e in planner_schema(state)["$defs"]["Action"]["oneOf"]]
    assert tools == [
        "get_alert_context", "assess_risk", "summarize_account", "query_account_transactions",
        "compare_account_baseline", "query_between_accounts", "check_structure",
    ]


def test_packet_shows_transactions_as_text_lines_within_target(tmp_path):
    from aml_agent.profile import PLANNER
    from aml_agent.providers import input_upper_bound
    from aml_agent.workflow import PLANNER_PROMPT

    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    rows = [
        ledger_row(str(1000 + i), f"2022-08-04T{i % 24:02}:00:00", f"0{i % 9}:8{i:08X}", "001:0002", "1234.56", "US Dollar")
        for i in range(68)
    ]
    state["evidence"]["ev_large"] = {"rows": rows, "complete": True,
                                     "transaction_context": {"1000": {"payer_prior_in_gap_h": 2.0, "payer_prior_sources_48h": 3,
                                                                      "payer_prior_other_currency": True, "reverse_edge_prior_48h": False}}}
    state["observations"] = [
        {"tool": "query_account_transactions",
         "arguments": {"account_ref": "001:0002", "direction": "incoming", "window_ref": "current"},
         "evidence_ref": "ev_large", "digest": ["query_account_transactions 001:0002 incoming: 68 rows, complete=True [ev_large]"]}
    ]
    packet = Workflow.fit_packet(Workflow(store, FakeProvider(), Tools(SimpleNamespace())).packet(state))
    lines = packet["brief"]["TRANSACTIONS (id | time | from > to | paid | context)"]
    assert len(lines) == 31 and lines[-1].startswith("... 38 more stored")
    assert "payer received 2.0h earlier from 3 source(s); payer had received another currency" in lines[0]
    assert packet["display_reductions"] == []
    # Far below the hard bound: the brief is lines of text, not nested JSON.
    assert input_upper_bound(PLANNER_PROMPT, packet) < PLANNER["input_hard_tokens_per_request"] // 2


def test_context_trimming_drops_memory_and_rows_but_keeps_the_contract(tmp_path):
    from aml_agent.profile import PLANNER
    from aml_agent.providers import input_upper_bound
    from aml_agent.workflow import PLANNER_PROMPT

    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    packet = Workflow(None, None, None).packet(state, "\n".join("m" * 2500 for _ in range(8)))
    result = Workflow.fit_packet(packet)
    assert input_upper_bound(PLANNER_PROMPT, result) <= PLANNER["input_hard_tokens_per_request"]
    assert result["display_reductions"][0].startswith("CASE MEMORY")
    assert result["decision_contract"]["based_on_state_version"] == state["state_version"]
    assert result["brief"]["CASE"] and result["brief"]["BUDGET"]


def test_transient_source_failure_is_retried_once_within_budget():
    from aml_agent.zetaris import TransientDataError

    class Flaky(Zetaris):
        attempts = 0

        def execute(self, sql, limit):
            self.attempts += 1
            if self.attempts == 1:
                raise TransientDataError("Zetaris HTTP 503")
            return []

    import aml_agent.zetaris as zetaris_module

    zetaris_module.RETRY_WAIT_SECONDS = 0
    reservations = []
    source = Flaky(SimpleNamespace())
    assert source.run("SELECT 1 FROM aml.transactions", 1, lambda: reservations.append(1)) == []
    assert source.attempts == 2 and len(reservations) == 2


def test_flow_follows_currency_changing_hops_like_published_cycles():
    source = ScriptedSource(
        [[0, *tx(1, "2022-08-04 01:00:00.0", "001:0002", "002:0003", "14546.79", "US Dollar")]],
        [[0, *tx(2, "2022-08-04 04:00:00.0", "002:0003", "003:0004", "97428.60", "Yuan")]],
        [[0, *tx(3, "2022-08-04 20:00:00.0", "003:0004", "001:0002", "14546.79", "US Dollar")]],
    )
    result = source.flow(case().account, "downstream", "2022-08-04", "2022-08-05", 24, 3, lambda: None)
    hop = source.calls[1][0]
    # Amount continuity is only enforced within one currency; no FX rates exist.
    assert "t.payment_currency <> f.currency OR" in hop and "ORDER BY t.transaction_time" in hop
    route = result["routes"][0]
    assert route["currencies"] == ["US Dollar", "Yuan", "US Dollar"]
    assert route["currency_changes"] == 2 and route["returns_to_origin"]
    assert route["amount_retained"] == "1.0000"


def test_gathered_continuation_keeps_every_feeding_payment():
    source = ScriptedSource(
        [
            [0, *tx(1, "2022-08-04 00:25:00.0", "001:0002", "002:0003", "60.00")],
            [0, *tx(2, "2022-08-04 06:25:00.0", "001:0002", "002:0003", "50.00")],
        ],
        [
            [0, *tx(3, "2022-08-04 15:31:00.0", "002:0003", "001:0002", "110.00")],
            [1, *tx(3, "2022-08-04 15:31:00.0", "002:0003", "001:0002", "110.00")],
        ],
    )
    result = source.flow(case().account, "downstream", "2022-08-04", "2022-08-05", 24, 2, lambda: None)
    routes = sorted(route["transaction_ids"] for route in result["routes"])
    # Both inflows feed the combined return; neither is dropped by de-duplication.
    assert routes == [["1", "3"], ["2", "3"]]
    assert all(route["returns_to_origin"] for route in result["routes"])
    assert len(result["rows"]) == 3


def test_unregistered_account_is_a_repairable_decision_error(tmp_path):
    from aml_agent.contracts import DECISIONS

    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    state.update(plan=["Where did funds go?"], plan_version=1)
    decision = DECISIONS.validate_python(
        {"schema_version": "planner/1.0", "decision": "action", "based_on_state_version": 0, "plan_version": 1,
         "brief_reason": "follow branch", "evidence_refs": [],
         "next_action": {"tool": "summarize_account", "arguments": {"account_refs": ["999:9999"]},
                         "expected_observation": "x", "question_id": 1}}
    )
    # Bounded formats let the model name any account; membership is checked here,
    # before dispatch, with the valid choices in the repair message.
    with pytest.raises(ValueError, match="Unregistered account 999:9999; use one of: 001:0002"):
        Workflow.validate_decision(decision, state, 0)


def test_risk_drivers_compare_against_the_population_median():
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier

    from aml_agent.rule_learning import RuleLearner
    from aml_agent.tiering import explain, export_trees

    features = ["fan_in_48h", "history_active_days_7d"]
    rows = [{"fan_in_48h": float(i % 5), "history_active_days_7d": float(7 - i % 8), "label": int(i % 5 >= 3)} for i in range(200)]
    x = RuleLearner.matrix(rows, features)
    model = export_trees(GradientBoostingClassifier(n_estimators=5, max_depth=2, random_state=0).fit(x, np.array([r["label"] for r in rows])), features, x)
    model["cutoffs"] = {"high": 0.5, "mid": -0.5}
    calibration = {"levels": [1, 5, 10, 25, 50, 75, 90, 95, 99],
                   "quantiles": {"fan_in_48h": [0, 0, 0, 1, 1, 2, 3, 4, 4], "history_active_days_7d": [0, 0, 1, 3, 5, 7, 7, 7, 7]}}
    result = explain(model, {"fan_in_48h": 4.0, "history_active_days_7d": 5.0}, calibration)
    # Only features away from the median can drive the score; fan-in 4 raises it.
    assert [d["feature"] for d in result["drivers"]] == ["fan_in_48h"] and result["drivers"][0]["score_effect"] > 0
    assert result["tier"] in ("high", "mid", "low")


def test_adjudication_schema_puts_verdict_last_and_bounds_accounts():
    from aml_agent.adjudication import Adjudication, schema

    from aml_agent.adjudication import final_level

    body = schema(["1:A", "2:B"])
    assert list(body["properties"])[-1] == "adjustment"
    assert body["properties"]["account_risks"]["maxItems"] == 2
    decision = Adjudication.model_validate({"rationale": "x", "key_evidence": [], "account_risks": [], "adjustment": "raise"})
    # The agent moves the calibrated prior one step at most; the ends are clamped.
    assert final_level("low", decision.adjustment) == "medium" and final_level("high", "raise") == "high"


def test_store_annotation_keeps_the_case_file_and_bumps_the_version(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    store.annotate(state["id"], "adjudication", {"decision": "report"})
    after = store.get(state["id"])
    assert after["adjudication"]["decision"] == "report" and after["state_version"] == state["state_version"] + 1
    assert after["request"] == state["request"]
