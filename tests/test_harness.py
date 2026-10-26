from copy import deepcopy

import pytest
from aml_agent.contracts import Action, StepRequest
from aml_agent.harness import ExecutionHarness, action_fingerprint
from aml_agent.store import Store
from aml_agent.tools import Tools
from aml_agent.workflow import Workflow
from test_workflow import FakeProvider, FakeZetaris, case


def action(window="current"):
    return Action(
        tool="query_account_transactions",
        arguments={
            "account_ref": "001:0002",
            "window_ref": window,
            "direction": "incoming",
        },
        expected_observation="Counts",
    )


def test_duplicate_scope_blocked_but_new_window_allowed(tmp_path):
    state = Store(tmp_path / "runs.sqlite").create(case().model_dump(mode="json"))
    guard = ExecutionHarness()
    fingerprint = action_fingerprint(action(), state)
    state["observations"].append(
        {"action_fingerprint": fingerprint, "evidence_ref": "ev_prior"}
    )
    verdict = guard.check(action(), state)
    assert not verdict.allowed and verdict.evidence_ref == "ev_prior"
    assert guard.check(action("history"), state).allowed
    reordered = action().model_copy(
        update={"arguments": dict(reversed(list(action().arguments.items())))}
    )
    assert action_fingerprint(reordered, state) == fingerprint
    guard.blocked(state, verdict)
    assert not state["harness"].get("force_finish")
    guard.blocked(state, verdict)
    assert state["harness"]["force_finish"]


def test_distinct_empty_queries_are_new_negative_evidence():
    guard, state = ExecutionHarness(), {}
    assert guard.observe(state, {"rows": []}, "scope_a")["new_observation"]
    assert guard.observe(state, {"rows": []}, "scope_b")["new_observation"]
    assert not guard.observe(state, {"rows": []}, "scope_b")["new_observation"]
    guard.observe(state, {"rows": []}, "scope_b")
    assert state["harness"]["force_finish"]


class RepeatingProvider(FakeProvider):
    def generate(self, system, packet, schema):
        if packet["decision_contract"]["finish_required"]:
            return super().generate(system, packet, schema)
        first_packet = deepcopy(packet)
        first_packet["plan"] = []
        response, usage = super().generate(system, first_packet, schema)
        if packet["plan"]:
            response.pop("initial_plan")
        return response, usage


def test_repeated_planner_action_forces_finish_without_second_sql(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")
    sql = FakeZetaris()
    workflow = Workflow(store, RepeatingProvider(), Tools(sql))
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
    # Non-row duplicates are rejected in-step with explicit feedback; the repair
    # repeated it, so the next step may only finish. The case still ends with the
    # planner's own judgement, usable for rule learning.
    assert "summarize_account with these exact arguments" in state["contract_errors"][0]["error"]
    assert state["status"] == "completed"
    assert state["conclusion"]["decision"] == "finish"
    assert sql.calls == 1
    events = store.trace_events(state["id"])
    assert sum(event["stage"] == "guard_blocked" for event in events) == 2
    assert sum(event["stage"] == "sql_dispatched" for event in events) == 1
    assert sum(event["stage"] == "finish_forced" for event in events) == 1
    with pytest.raises(ValueError):
        store.trace(state["id"], "unsafe", span_id="x", api_key="secret")


def test_progress_clears_stale_block_feedback():
    guard, state = ExecutionHarness(), {"observations": [], "evidence": {}}
    state["harness"] = {"feedback": {"code": "duplicate_completed_action"}}
    guard.observe(state, {"rows": []}, "new_scope")
    assert "feedback" not in state["harness"]
