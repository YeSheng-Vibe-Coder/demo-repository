from copy import deepcopy

import pytest
from aml_agent.contracts import FinishDecision, TransferClaim
from aml_agent.evidence import EvidenceConflict, transaction_ledger, verify_claims
from aml_agent.store import Store
from test_workflow import FakeProvider, case


def evidence_state(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    row = {
        "transaction_id": "t1",
        "from_bank": "000",
        "from_account": "0001",
        "to_bank": "001",
        "to_account": "0002",
        "transaction_time": "2022-08-04T10:00:00",
        "amount_received": "50",
        "receiving_currency": "Euro",
        "amount_paid": "50",
        "payment_currency": "Euro",
    }
    state["evidence"] = {
        "ev_a": {"rows": [row], "complete": True},
        "ev_b": {"rows": [deepcopy(row)], "complete": True},
    }
    state["observations"] = [
        {
            "tool": "query_account_transactions",
            "arguments": {
                "account_ref": "001:0002",
                "window_ref": "current",
                "direction": "both",
            },
            "evidence_ref": ref,
        }
        for ref in state["evidence"]
    ]
    return store, state


def proposal(**overrides):
    return FinishDecision(
        decision="finish",
        schema_version="planner/1.0",
        based_on_state_version=0,
        plan_version=1,
        brief_reason="Observed flow only",
        evidence_refs=["ev_a"],
        conclusion="insufficient_evidence",
        stop_reason="Network scope remains unknown",
        **overrides,
    )


def test_same_id_is_one_directed_edge_and_true_reverse_is_distinct(tmp_path):
    _, state = evidence_state(tmp_path)
    ledger = transaction_ledger(state)
    assert ledger["unique_count"] == 1
    assert (
        ledger["observed_incoming_count"] == 1
        and ledger["observed_outgoing_count"] == 0
    )
    assert set(ledger["transactions"][0]["evidence_refs"]) == {"ev_a", "ev_b"}
    reverse = deepcopy(state["evidence"]["ev_a"]["rows"][0])
    reverse.update(
        transaction_id="t2",
        from_bank="001",
        from_account="0002",
        to_bank="000",
        to_account="0001",
        transaction_time="2022-08-04T11:00:00",
    )
    state["evidence"]["ev_c"] = {"rows": [reverse]}
    ledger = transaction_ledger(state)
    assert ledger["unique_count"] == 2 and ledger["observed_outgoing_count"] == 1


def test_conflicting_same_id_is_not_silently_deduplicated(tmp_path):
    _, state = evidence_state(tmp_path)
    state["evidence"]["ev_b"]["rows"][0]["to_account"] = "9999"
    with pytest.raises(EvidenceConflict):
        transaction_ledger(state)


def test_wrong_direction_and_repeated_claims_rejected(tmp_path):
    _, state = evidence_state(tmp_path)
    claim = TransferClaim(
        transaction_id="t1",
        from_account_ref="001:0002",
        to_account_ref="000:0001",
        evidence_refs=["ev_a"],
    )
    assert (
        verify_claims(proposal(transaction_claims=[claim]), transaction_ledger(state))[
            0
        ]["code"]
        == "direction_error"
    )
    correct = claim.model_copy(
        update={"from_account_ref": "000:0001", "to_account_ref": "001:0002"}
    )
    assert (
        verify_claims(
            proposal(transaction_claims=[correct, correct]), transaction_ledger(state)
        )[0]["code"]
        == "duplicate_transaction"
    )


def test_case_finish_does_not_call_llm_evaluator(tmp_path):
    from aml_agent.contracts import StepRequest
    from aml_agent.tools import Tools
    from aml_agent.workflow import Workflow
    from test_workflow import FakeZetaris

    store = Store(tmp_path / "runs.sqlite")
    state = store.create(case().model_dump(mode="json"))
    lease = store.claim(state["id"], "worker")

    class NoCaseJudgeProvider(FakeProvider):
        def generate(self, system, packet, schema):
            assert not packet.get("review_contract"), (
                "Case loop must not invoke a business LLM evaluator"
            )
            return super().generate(system, packet, schema)

    workflow = Workflow(store, NoCaseJudgeProvider(), Tools(FakeZetaris()))
    for index in range(2):
        state = workflow.step(
            state["id"],
            StepRequest(
                expected_state_version=state["state_version"],
                action_id=str(index),
                worker_id="worker",
                fence=lease["fence"],
            ),
        )
    assert state["status"] == "completed" and state["conclusion"]
    assert len(state["model_calls"]) == 2
    assert state["validation"]["label_access"] is False
    assert state["validation"]["validation_kind"] == "schema_reference_scope_only"
