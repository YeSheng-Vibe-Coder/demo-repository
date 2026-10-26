import pytest
from aml_agent.api import create_app
from aml_agent.contracts import DECISIONS, CaseRequest
from aml_agent.evaluation import TransactionLabelReader, grade_samples, has_model_finish
from aml_agent.rule_learning import atom_true, condition_sql
from aml_agent.rule_program import RuleAtom, RuleGroup, RuleProgram, compile_program, load_program
from aml_agent.settings import Settings
from aml_agent.store import Store
from aml_agent.workflow import Workflow, planner_schema
from fastapi.testclient import TestClient


def program(threshold):
    return RuleProgram(
        rules=[RuleAtom(rule_id="IBM-T", feature_id="fan_in_24h", operator="ge", threshold=threshold, source_refs=["P02"])],
        groups=[RuleGroup(group_id="g", operator="any", rule_ids=["IBM-T"])],
        group_operator="any",
    )


def register(store, rule, status="research_reference"):
    return store.artifact("rule_program_version", {**compile_program(rule), "status": status, "parent_version": None})


def sample_state():
    row = {
        "transaction_id": "101", "from_bank": "B", "from_account": "1", "to_bank": "A", "to_account": "2",
        "transaction_time": "2022-08-04T12:00:00", "amount_received": "5", "receiving_currency": "EUR",
        "amount_paid": "5", "payment_currency": "EUR",
    }
    return {
        "request": {"account": {"bank": "A", "account": "2"}},
        "evidence": {"ev": {"rows": [row, {**row, "transaction_id": "102"}]}},
        "conclusion": {"suspect_transaction_ids": ["101"]},
    }


def test_weighted_groups_compile_and_evaluate_like_their_sql():
    candidate = RuleProgram(
        rules=[
            RuleAtom(rule_id="IBM-T", feature_id="fan_in_24h", operator="ge", threshold=3, source_refs=["P02"]),
            RuleAtom(rule_id="IBM-X", feature_id="currency_count_24h", operator="ge", threshold=2, source_refs=["P03"]),
        ],
        groups=[RuleGroup(group_id="w", operator="weighted", rule_ids=["IBM-T", "IBM-X"], weights=[1, 2], cutoff=2)],
        group_operator="all",
    )
    compile_program(candidate)
    assert "CASE WHEN (COALESCE(currency_count_24h, 0) >= 2.0) THEN 2.0" in condition_sql(candidate)
    assert atom_true(candidate.rules[1], {"currency_count_24h": 2.0})
    with pytest.raises(ValueError):
        RuleGroup(group_id="g", operator="weighted", rule_ids=["IBM-T"], weights=[1], cutoff=2)


def test_unregistered_features_sources_and_disabled_rules_are_rejected():
    candidate = program(3)
    candidate.rules[0].feature_id = "unimplemented_raw_feature"
    with pytest.raises(ValueError, match="extractor"):
        compile_program(candidate)
    candidate = program(3)
    candidate.rules[0].source_refs = ["invented"]
    with pytest.raises(ValueError, match="policy"):
        compile_program(candidate)
    orphan = program(3).model_copy(update={"rules": [*program(3).rules, RuleAtom(
        rule_id="IBM-ORPHAN", feature_id="fan_in_48h", operator="ge", threshold=1, source_refs=["P02"])]})
    with pytest.raises(ValueError, match="Disabled rules"):
        compile_program(orphan)


def test_frozen_versions_are_hash_checked(tmp_path):
    store = Store(tmp_path / "rules.sqlite")
    version = register(store, program(3))
    assert load_program(store, version)["status"] == "research_reference"
    body = store.read_artifact(version, "rule_program_version")
    tampered = store.artifact("rule_program_version", {**body, "program": program(9).model_dump(mode="json")})
    with pytest.raises(ValueError, match="hash mismatch"):
        load_program(store, tampered)


def test_exact_transaction_labels_are_not_account_labels_or_all_retrieved():
    state = sample_state()
    checked = grade_samples(state, {"101": 1, "102": 0})
    assert checked["selected_suspicious_transactions"] == 1 and checked["retrieved_unique_transactions"] == 2
    assert checked["tp"] == 1 and checked["fp"] == 0 and checked["global_transaction_recall"] is None
    assert "label" not in str(state)
    state["conclusion"]["suspect_transaction_ids"] = []
    assert grade_samples(state, {"101": 1, "102": 0})["precision"] is None
    state["conclusion"]["suspect_transaction_ids"] = ["999"]
    with pytest.raises(ValueError):
        grade_samples(state, {"101": 1, "102": 0})


def test_label_lookup_rejects_missing_and_uncontrolled_ids():
    class Source:
        def run(self, sql, limit):
            assert sql.startswith("SELECT transaction_id, is_laundering FROM IBM_AML.hi_large_transactions")
            return [[101, 1]]

    reader = TransactionLabelReader(Source())
    assert reader.read(["101"]) == {"101": 1} and reader.calls == 1
    with pytest.raises(ValueError):
        reader.read(["101 OR 1=1"])
    with pytest.raises(ValueError, match="Missing"):
        reader.read(["101", "102"])


def test_rule_alert_binds_the_case_and_carries_no_labels(tmp_path):
    settings = Settings("", "", "http://unused", "1", "local-test", tmp_path / "api.sqlite")
    store = Store(settings.database)
    version = register(store, program(2))
    alert = {"base_version": version, "account": {"bank": "001", "account": "A001"}, "start": "2022-08-05T00:00:00",
             "as_of": "2022-08-06T00:00:00", "triggered_rule_ids": ["IBM-T"], "features": {"fan_in_24h": 4.0},
             "alert_sha256": "x"}
    alert_id = store.artifact("rule_alert", alert)
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer local-test"}
    request = CaseRequest(account=alert["account"], start=alert["start"], as_of=alert["as_of"], split="development",
                          rule_version=version, alert_id=alert_id).model_dump(mode="json")
    response = client.post("/v1/cases", json=request, headers=headers)
    assert response.status_code == 200
    assert response.json()["rule_context"]["alert"]["triggered_rule_ids"] == ["IBM-T"]
    assert "label" not in str(response.json()["rule_context"])
    wrong = {**request, "account": {"bank": "001", "account": "wrong"}}
    assert client.post("/v1/cases", json=wrong, headers=headers).status_code == 422
    # Specialist sub-runs cannot be created through the public API.
    assignment = {"role": "x", "question": "q", "accounts": ["001:A001"], "tools": ["check_structure"], "max_actions": 2}
    assert client.post("/v1/cases", json={**request, "assignment": assignment}, headers=headers).status_code == 422


def test_execution_guard_stop_is_not_a_completed_case_for_rule_learning():
    state = sample_state()
    state["status"] = "insufficient_evidence"
    state["conclusion"] = {"kind": "harness_stop", "conclusion": "insufficient_evidence"}
    assert not has_model_finish(state)
    state["conclusion"] = {"decision": "finish", "conclusion": "insufficient_evidence", "suspect_transaction_ids": []}
    assert has_model_finish(state)


def test_suspicious_output_grammar_is_bounded_by_actual_evidence(tmp_path):
    store = Store(tmp_path / "schema.sqlite")
    state = store.create(CaseRequest(account={"bank": "A", "account": "2"}, start="2022-08-04T00:00:00",
                                     as_of="2022-08-05T00:00:00", split="development").model_dump(mode="json"))
    state["evidence"] = sample_state()["evidence"]
    finish = planner_schema(state)["$defs"]["FinishDecision"]["properties"]
    assert finish["suspect_transaction_ids"]["maxItems"] == 2
    # Bounded formats, not enums (an enum dead-ends the grammar); the host checks membership.
    assert finish["suspect_transaction_ids"]["items"]["pattern"] == "^[0-9]{1,20}$"
    state.update(plan=["q"], plan_version=1)
    decision = DECISIONS.validate_python(
        {"schema_version": "planner/1.0", "decision": "finish", "based_on_state_version": state["state_version"],
         "plan_version": 1, "brief_reason": "x", "evidence_refs": [], "conclusion": "suspicious",
         "stop_reason": "done", "suspect_transaction_ids": ["999"]}
    )
    with pytest.raises(ValueError, match="observed transaction IDs"):
        Workflow.validate_decision(decision, state, state["state_version"])
    # Naming suspects while concluding not_supported is inconsistent and rejected.
    with pytest.raises(ValueError, match="conclusion must be suspicious"):
        Workflow.validate_decision(decision.model_copy(update={"conclusion": "not_supported"}), state, state["state_version"])


def test_versions_frozen_before_2_0_still_load_and_stay_tamper_evident(tmp_path):
    from aml_agent.harness import content_hash

    store = Store(tmp_path / "legacy.sqlite")
    legacy = {**program(3).model_dump(mode="json"), "derived_features": []}
    version = store.artifact("rule_program_version", {"program": legacy, "program_sha256": content_hash(legacy),
                                                      "status": "accepted_experiment"})
    assert load_program(store, version)["status"] == "accepted_experiment"
    altered = {**legacy, "groups": [{**legacy["groups"][0], "operator": "all"}]}
    tampered = store.artifact("rule_program_version", {"program": altered, "program_sha256": content_hash(legacy),
                                                       "status": "accepted_experiment"})
    with pytest.raises(ValueError, match="hash mismatch"):
        load_program(store, tampered)
