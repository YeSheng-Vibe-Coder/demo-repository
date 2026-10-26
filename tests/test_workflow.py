import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from aml_agent.contracts import Account, CaseRequest, FinishDecision, StepRequest
from aml_agent.evaluation import validate_partition
from aml_agent.store import Conflict, Store
from aml_agent.tools import Tools
from aml_agent.zetaris import DataError, Zetaris
from aml_agent.workflow import Workflow, planner_schema


def case():
    return CaseRequest(
        account=Account(bank="001", account="0002"),
        start=datetime(2022, 8, 4),
        as_of=datetime(2022, 8, 5),
        split="development",
    )


class FakeProvider:
    settings = SimpleNamespace(model="nvidia/nemotron-3.5-lightning-30b-a3b")

    def generate(self, system, packet, schema):
        assert "is_laundering" not in str(packet)
        if packet.get("review_contract"):
            return {
                "verdict": "pass",
                "findings": [],
                "rationale": "Narrative matches supplied canonical facts",
            }, {"prompt_tokens": 100, "completion_tokens": 100}
        base = {
            "schema_version": "planner/1.0",
            "based_on_state_version": packet["state_version"],
            "plan_version": 1,
            "brief_reason": "Inspect observed activity",
            "evidence_refs": [],
        }
        if packet["plan"]:
            base.update(
                decision="finish",
                conclusion="not_supported",
                stop_reason="No supporting evidence",
                evidence_refs=packet["all_evidence_refs"],
            )
        else:
            base.update(
                decision="action",
                initial_plan=["Does activity converge?", "Does it rapidly disperse?"],
                next_action={
                    "tool": "summarize_account",
                    "arguments": {"account_refs": ["001:0002"], "window_ref": "current"},
                    "expected_observation": "Source counts",
                    "question_id": 1,
                },
            )
        return base, {"prompt_tokens": 100, "completion_tokens": 100}


class FakeZetaris:
    calls = 0

    def summary(self, account, start, end, direction, reserve):
        reserve()
        self.calls += 1
        return {"groups": [], "complete": True}

    def account_features(self, accounts_days, as_of, reserve):
        return {}

    def transaction_context(self, rows, reserve):
        return {}

    def profile(self, accounts, start, end, reserve):
        reserve()
        self.calls += 1
        return {
            "accounts": {a.reference: {"incoming": [], "outgoing": []} for a in accounts},
            "complete": True,
            "window": {"start": start, "end": end},
        }


def test_evidence_loop_is_durable_and_idempotent(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    sql = FakeZetaris()
    workflow = Workflow(store, FakeProvider(), Tools(sql))
    request = StepRequest(
        expected_state_version=0,
        action_id="first",
        worker_id="worker",
        fence=lease["fence"],
    )
    first = workflow.step(state["id"], request)
    duplicate = workflow.step(state["id"], request)
    assert first == duplicate
    assert sql.calls == 1
    assert first["budget"]["sql_requests"] == 1
    final = workflow.step(
        state["id"],
        StepRequest(
            expected_state_version=first["state_version"],
            action_id="second",
            worker_id="worker",
            fence=lease["fence"],
        ),
    )
    assert final["status"] == "completed"
    assert final["budget"]["model_requests"] == 2
    assert final["budget"]["output_tokens"] == 200


def test_expired_worker_cannot_commit(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    with store.connect() as db:
        db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (state["id"],))
    new = store.claim(state["id"], "replacement")
    assert new["fence"] > lease["fence"]
    with pytest.raises(Conflict):
        store.save(state, 0, "worker", lease["fence"])


def test_unknown_reference_and_stale_decision_rejected():
    decision = FinishDecision(
        schema_version="planner/1.0",
        decision="finish",
        based_on_state_version=4,
        plan_version=1,
        brief_reason="Complete",
        evidence_refs=["invented"],
        conclusion="suspicious",
        stop_reason="finished",
        initial_plan=["one", "two"],
    )
    state = {"evidence": {}, "plan": [], "plan_version": 0, "budget": {"replans": 0}}
    with pytest.raises(ValueError, match="Unknown evidence"):
        Workflow.validate_decision(decision, state, 4)
    with pytest.raises(ValueError, match="stale"):
        Workflow.validate_decision(decision, state, 5)


def test_partitions_and_held_out_test():
    validate_partition(case())
    with pytest.raises(ValueError):
        validate_partition(case().model_copy(update={"start": datetime(2022, 8, 3)}))
    with pytest.raises(ValueError, match="locked"):
        CaseRequest(
            account=Account(bank="1", account="2"),
            start=datetime(2022, 8, 15),
            as_of=datetime(2022, 8, 16),
            split="test",
        )


def test_flow_does_not_infer_order_for_same_minute():
    parent = {
        "transaction_time": "2022-08-04T01:00:00",
        "to_bank": "1",
        "to_account": "b",
        "receiving_currency": "USD",
    }
    child = {
        "transaction_time": "2022-08-04T01:00:00",
        "from_bank": "1",
        "from_account": "b",
        "payment_currency": "USD",
    }
    # Same-minute timestamps cannot establish that funds moved onward.
    with pytest.raises(DataError):
        Zetaris.check_link(parent, child, "downstream", 24)

def test_budget_exhaustion_stops_before_provider_dispatch(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    from aml_agent.profile import PLANNER

    limit = PLANNER["max_model_http_requests_per_case_including_repairs"]
    state["budget"]["model_requests"] = limit
    store.save(state, 0, "worker", lease["fence"])
    provider = FakeProvider()
    provider.generate = lambda *args: pytest.fail(
        "Provider must not be called after budget exhaustion"
    )
    result = Workflow(store, provider, Tools(FakeZetaris())).step(
        state["id"],
        StepRequest(
            expected_state_version=1,
            action_id="budget-check",
            worker_id="worker",
            fence=lease["fence"],
        ),
    )
    assert result["status"] == "budget_exhausted"
    assert result["budget"]["model_requests"] == limit


def test_cancellation_stops_before_provider_dispatch(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    store.request_cancel(state["id"])
    provider = FakeProvider()
    provider.generate = lambda *args: pytest.fail(
        "Cancelled case cannot dispatch a model call"
    )
    result = Workflow(store, provider, Tools(FakeZetaris())).step(
        state["id"],
        StepRequest(
            expected_state_version=0,
            action_id="cancel-check",
            worker_id="worker",
            fence=lease["fence"],
        ),
    )
    assert result["status"] == "cancelled"
    assert result["budget"]["model_requests"] == 0


def test_provider_schema_excludes_illegal_state_transitions(tmp_path):
    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    schema = planner_schema(state)
    assert "initial_plan" in schema["$defs"]["ActionDecision"]["required"]
    assert schema["$defs"]["ActionDecision"]["properties"]["plan_version"]["const"] == 1
    assert not any("ReplanDecision" in entry["$ref"] for entry in schema["oneOf"])
    state.update(plan=["question one", "question two"], plan_version=1)
    schema = planner_schema(state)
    assert "initial_plan" not in schema["$defs"]["ActionDecision"]["properties"]
    tools = [
        entry["properties"]["tool"]["const"]
        for entry in schema["$defs"]["Action"]["oneOf"]
    ]
    assert "propose_rule_change" not in tools  # No frozen base version yet.


def test_last_affordable_call_or_chunk_allows_only_finish(tmp_path):
    from aml_agent.profile import MARKOVIAN, PLANNER

    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    state.update(plan=["question one", "question two"], plan_version=1)
    state["budget"]["model_requests"] = 3
    assert len(planner_schema(state)["oneOf"]) == 3
    state["budget"]["model_requests"] = PLANNER[
        "max_model_http_requests_per_case_including_repairs"
    ]
    refs = [entry["$ref"] for entry in planner_schema(state)["oneOf"]]
    assert refs == ["#/$defs/FinishDecision"]
    state["budget"]["model_requests"] = 3
    state["decisions"] = [{}] * (MARKOVIAN["maxChunks"] - 1)
    refs = [entry["$ref"] for entry in planner_schema(state)["oneOf"]]
    assert refs == ["#/$defs/FinishDecision"]


def test_wall_clock_starts_at_first_step_not_case_creation(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    with store.connect() as db:
        stale = {**state, "created": state["created"] - 3600}
        db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(stale), state["id"]))
    lease = store.claim(state["id"], "worker")
    result = Workflow(store, FakeProvider(), Tools(FakeZetaris())).step(
        state["id"],
        StepRequest(
            expected_state_version=0,
            action_id="late-start",
            worker_id="worker",
            fence=lease["fence"],
        ),
    )
    assert result["status"] == "running"
    assert result["started"] - result["created"] >= 3600


def test_duplicate_detail_query_pages_stored_rows_instead_of_stopping(tmp_path):
    """Regression for the 20261022 cycle: a 68-row fan-in re-queried until loop stop."""

    rows = [
        {
            "transaction_id": str(index),
            "transaction_time": f"2022-08-04T{index % 24:02}:00:00",
            "from_bank": "000",
            "from_account": f"{index:04}",
            "to_bank": "001",
            "to_account": "0002",
            "amount_received": "10",
            "receiving_currency": "Euro",
            "amount_paid": "10",
            "payment_currency": "Euro",
            "payment_format": "ACH",
        }
        for index in range(68)
    ]

    class DetailSource:
        calls = 0

        def transaction_context(self, rows, reserve):
            return {}

        def account_features(self, accounts_days, as_of, reserve):
            return {}

        def transactions(self, account, start, end, direction, reserve, cap, other):
            reserve()
            self.calls += 1
            assert cap == 200
            return {"rows": rows, "complete": True, "status": "complete"}

    class RepeatDetail(FakeProvider):
        def generate(self, system, packet, schema):
            decision = {
                "schema_version": "planner/1.0",
                "decision": "action",
                "based_on_state_version": packet["state_version"],
                "plan_version": 1,
                "brief_reason": "Need the remaining fan-in rows",
                "evidence_refs": [],
                "next_action": {
                    "tool": "query_account_transactions",
                    "arguments": {"account_ref": "001:0002", "direction": "incoming"},
                    "expected_observation": "All contributing transfers",
                    "question_id": 1,
                },
            }
            if not packet["plan"]:
                decision["initial_plan"] = ["Who sends funds?", "Where do they go?"]
            return decision, {"prompt_tokens": 100, "completion_tokens": 100}

    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    source = DetailSource()
    workflow = Workflow(store, RepeatDetail(), Tools(source))
    for index in range(3):
        state = workflow.step(
            state["id"],
            StepRequest(
                expected_state_version=state["state_version"],
                action_id=str(index),
                worker_id="worker",
                fence=lease["fence"],
            ),
        )
    assert source.calls == 1
    assert state["status"] == "running"
    pages = [state["evidence"][o["evidence_ref"]] for o in state["observations"][1:]]
    assert [page["page_offset"] for page in pages] == [30, 60]
    assert len(pages[1]["rows"]) == 8


def test_planner_grammar_is_bounded_below_output_reservation(tmp_path):
    from aml_agent.profile import PLANNER
    from aml_agent.workflow import output_bound

    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    state["request"]["rule_version"] = "frozen-rule"
    rows = [
        {
            "transaction_id": str(10**8 + index),
            "transaction_time": "2022-08-04T01:00:00",
            "from_bank": "0011",
            "from_account": f"8{index:08X}",
            "to_bank": "001",
            "to_account": "0002",
            "amount_received": "1",
            "receiving_currency": "Euro",
            "amount_paid": "1",
            "payment_currency": "Euro",
        }
        for index in range(200)
    ]
    for index in range(12):
        state["evidence"][f"ev_{index:032x}"] = {"rows": rows if index == 0 else []}
    for index in range(20):
        state["accounts"][f"0011:8{index:08X}"] = {"bank": "0011", "account": f"8{index:08X}"}
    state.update(plan=["question one", "question two"], plan_version=1)
    # Twelve structural candidates make the finish grammar its largest.
    state["evidence"][f"ev_{0:032x}"]["routes"] = [
        {
            "transaction_ids": [str(10**8 + 2 * i), str(10**8 + 2 * i + 1)],
            "accounts": [f"0011:8{2 * i:08X}", "001:0002", f"0011:8{2 * i + 1:08X}"],
            "gap_hours": [1.0],
            "amount_retained": "0.99",
            "returns_to_origin": True,
        }
        for i in range(12)
    ]
    from aml_agent.candidates import MAX_CANDIDATES, candidate_transactions

    assert len(candidate_transactions(state)) == MAX_CANDIDATES
    # Two characters per token is a pessimistic tokenizer assumption.
    limit = 2 * PLANNER["output_hard_tokens_per_request_including_reasoning"]
    assert output_bound(planner_schema(state)) <= limit
    # The NVIDIA grammar rejects these keywords outright (HTTP 400).
    assert not {"uniqueItems", "patternProperties"} & set(json.dumps(planner_schema(state)).replace('"', " ").split())
    open_schema = {"oneOf": [{"type": "object", "properties": {}}]}
    with pytest.raises(ValueError):
        output_bound(open_schema)


def test_replan_must_carry_an_action():
    from aml_agent.contracts import DECISIONS

    with pytest.raises(Exception):
        DECISIONS.validate_python(
            {
                "schema_version": "planner/1.0",
                "decision": "replan",
                "based_on_state_version": 3,
                "plan_version": 2,
                "previous_plan_version": 1,
                "brief_reason": "New branch",
                "evidence_refs": [],
                "plan_patch": ["Follow the downstream branch"],
            }
        )


def test_transient_model_failure_is_retried_once_and_budgeted(tmp_path, monkeypatch):
    from aml_agent.providers import TransientModelError

    monkeypatch.setattr("aml_agent.workflow.time.sleep", lambda seconds: None)

    class FlakyProvider(FakeProvider):
        failures = 1

        def generate(self, system, packet, schema):
            if self.failures:
                self.failures -= 1
                raise TransientModelError("NVIDIA HTTP 503")
            return super().generate(system, packet, schema)

    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    state = Workflow(store, FlakyProvider(), Tools(FakeZetaris())).step(
        state["id"],
        StepRequest(expected_state_version=0, action_id="a", worker_id="worker", fence=lease["fence"]),
    )
    assert state["status"] == "running" and state["observations"]
    assert state["budget"]["model_requests"] == 2
    assert any(e["stage"] == "model_retry" for e in store.trace_events(state["id"]))


def test_one_invalid_step_does_not_end_the_case_but_two_consecutive_do(tmp_path):
    class Invalid(FakeProvider):
        def generate(self, system, packet, schema):
            return {"decision": "action"}, {"prompt_tokens": 10, "completion_tokens": 10}

    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    workflow = Workflow(store, Invalid(), Tools(FakeZetaris()))
    request = lambda i, s: StepRequest(expected_state_version=s["state_version"], action_id=str(i), worker_id="worker", fence=lease["fence"])
    state = workflow.step(state["id"], request(0, state))
    assert state["status"] == "running" and state["budget"]["model_requests"] == 2
    assert any(e["stage"] == "step_without_action" for e in store.trace_events(state["id"]))
    state = workflow.step(state["id"], request(1, state))
    assert state["status"] == "model_unavailable"


def test_grammar_never_contains_zero_capacity_arrays(tmp_path):
    def arrays(node, found):
        if isinstance(node, dict):
            if node.get("type") == "array" and node.get("maxItems") == 0:
                found.append(node)
            for value in node.values():
                arrays(value, found)
        elif isinstance(node, list):
            for value in node:
                arrays(value, found)
        return found

    # A fresh case (no evidence, no transactions, no candidates) is the worst case:
    # an opened empty array leaves only "]" or whitespace, and the model loops.
    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    schema = planner_schema(state)
    assert arrays(schema, []) == []
    finish = schema["$defs"]["FinishDecision"]
    assert "candidate_decisions" not in finish["properties"] and "chain" not in finish["properties"]
    assert "suspect_transaction_ids" not in finish["required"]


def test_aggregator_finishes_a_team_case_despite_its_own_budget_reservation(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    parent = store.create(case().model_dump(mode="json"))
    child = store.create({**case().model_dump(mode="json"),
                          "assignment": {"role": "flow", "question": "Where did funds go?", "accounts": ["001:0002"],
                                         "tools": ["check_structure"], "max_actions": 2},
                          "parent_run_id": parent["id"]})
    child.update(status="completed", conclusion={"conclusion": "not_supported", "supported_findings": ["no onward flow"]})
    store.seed(child)
    parent.update(plan=["Where did funds go?"], plan_version=1,
                  team={"children": [child["id"]], "assignments": [{"role": "flow", "question": "Where did funds go?"}]})
    store.seed(parent)
    # FakeProvider echoes packet["state_version"]; reserving the call advances the
    # stored version, and the decision must still validate against the packet's.
    state = Workflow(store, FakeProvider(), Tools(FakeZetaris())).aggregate(parent["id"], "case graph: 1 account")
    assert state["status"] == "completed" and state["conclusion"]["decision"] == "finish"
    assert not state.get("contract_errors")
