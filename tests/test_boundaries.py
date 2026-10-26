import httpx
import pytest
from aml_agent.api import create_app
from aml_agent.contracts import Account
from aml_agent.providers import ModelError, Nvidia
from aml_agent.settings import Settings
from aml_agent.zetaris import DataError, Zetaris
from fastapi.testclient import TestClient


def settings(tmp_path):
    return Settings(
        "test-nvidia-key",
        "test-data-key",
        "http://localhost:8888/api/v1.0",
        "1",
        "local-test-token",
        tmp_path / "db.sqlite",
    )


def test_api_requires_authentication_and_validates_partition(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    body = {
        "account": {"bank": "01", "account": "0002"},
        "start": "2022-08-04T00:00:00",
        "as_of": "2022-08-05T00:00:00",
        "split": "development",
    }
    assert client.post("/v1/cases", json=body).status_code == 401
    headers = {"Authorization": "Bearer local-test-token"}
    response = client.post("/v1/cases", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["accounts"]["01:0002"]["account"] == "0002"
    body["split"] = "validation"
    assert client.post("/v1/cases", json=body, headers=headers).status_code == 422


def test_capped_results_are_partial_and_all_probe_rows_are_validated(tmp_path):
    data = Zetaris(settings(tmp_path))
    row = [
        1,
        "2022-08-04T01:00:00",
        "1",
        "a",
        "2",
        "b",
        "10",
        "USD",
        "10",
        "USD",
        "ACH",
    ]
    data.execute = lambda sql, limit: [row, [*row[:1], "2022-08-05T00:00:00", *row[2:]]]
    with pytest.raises(DataError, match="Out-of-window"):
        data.transactions(
            Account(bank="1", account="a"),
            "2022-08-04T00:00:00",
            "2022-08-05T00:00:00",
            "outgoing",
            lambda: None,
            cap=1,
        )
    data.execute = lambda sql, limit: [row, [2, *row[1:]]]
    result = data.transactions(
        Account(bank="1", account="a"),
        "2022-08-04T00:00:00",
        "2022-08-05T00:00:00",
        "outgoing",
        lambda: None,
        cap=1,
    )
    assert result["complete"] is False
    assert len(result["rows"]) == 1


def test_provider_does_not_accept_model_substitution_or_retry(tmp_path, monkeypatch):
    calls = []

    def complete(self, body):
        calls.append(body)
        return {"model": "other-model", "choices": [], "usage": {}}

    monkeypatch.setattr(Nvidia, "complete", complete)
    with pytest.raises(ModelError, match="unexpected model"):
        Nvidia(settings(tmp_path)).generate("system", {}, {})
    assert len(calls) == 1
    assert calls[0]["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert calls[0]["stream"] is True and calls[0]["stream_options"] == {"include_usage": True}
    from aml_agent.profile import PLANNER

    assert calls[0]["max_tokens"] == PLANNER["output_hard_tokens_per_request_including_reasoning"]


def test_provider_usage_missing_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Nvidia,
        "complete",
        lambda self, body: {
            "model": settings(tmp_path).model,
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {},
        },
    )
    with pytest.raises(ModelError, match="Missing reliable usage"):
        Nvidia(settings(tmp_path)).generate("system", {}, {})


def test_whitespace_runaway_stream_is_cut_and_keeps_the_reservation(tmp_path, monkeypatch):
    import json as jsonlib
    from contextlib import contextmanager

    from aml_agent.profile import PLANNER

    model = settings(tmp_path).model
    lines = [f"data: {jsonlib.dumps({'model': model, 'choices': [{'delta': {'content': '{\"a\": '}}]})}"]
    lines += [f"data: {jsonlib.dumps({'model': model, 'choices': [{'delta': {'content': '    '}}]})}"] * 40
    consumed = []

    class Stream:
        status_code, headers = 200, {}

        def iter_lines(self):
            for line in lines:
                consumed.append(line)
                yield line

    @contextmanager
    def stream(*args, **kwargs):
        yield Stream()

    monkeypatch.setattr(httpx, "stream", stream)
    raw, usage = Nvidia(settings(tmp_path)).generate("system", {}, {})
    assert "whitespace runaway" in raw["_invalid_response"] and "tail=whitespace" in raw["_invalid_response"]
    # Stopped after ~80 whitespace characters instead of reading to max_tokens.
    assert len(consumed) < len(lines)
    # Charged at provable upper bounds, never below the received characters.
    assert len('{"a": ') + 80 <= usage["completion_tokens"] < PLANNER["output_hard_tokens_per_request_including_reasoning"]
    assert usage["usage_source"] == "upper_bound_aborted_stream"


def test_parallel_investigators_cannot_step_one_run_concurrently(tmp_path, monkeypatch):
    import threading

    from aml_agent import workflow as workflow_module

    client = TestClient(create_app(settings(tmp_path)))
    headers = {"Authorization": "Bearer local-test-token"}
    gate, calls = threading.Event(), []

    def slow_step(self, run_id, body):
        calls.append(run_id)
        gate.wait(2)
        return {}

    monkeypatch.setattr(workflow_module.Workflow, "step", slow_step)
    body = {"expected_state_version": 0, "action_id": "a", "worker_id": "w", "fence": 1}
    results = []

    def post(run_id):
        results.append(client.post(f"/internal/runs/{run_id}/step", json=body, headers=headers).status_code)

    first = threading.Thread(target=post, args=("r1",))
    first.start()
    while not calls:
        pass
    # A second step on the same run is refused while the first is in flight ...
    assert client.post("/internal/runs/r1/step", json=body, headers=headers).status_code == 409
    # ... while another run proceeds in parallel.
    other = threading.Thread(target=post, args=("r2",))
    other.start()
    while len(calls) < 2:
        pass
    gate.set()
    first.join()
    other.join()
    assert sorted(results) == [200, 200]


def test_committed_schemas_match_the_contracts():
    import json

    from aml_agent.cli import exported_schemas
    from aml_agent.settings import ROOT

    committed = {p.name.removesuffix(".schema.json"): json.loads(p.read_text()) for p in (ROOT / "schemas").glob("*.schema.json")}
    assert committed == exported_schemas(), "Run: python -m aml_agent.cli export-schemas"


def test_team_lease_survives_a_model_call_longer_than_the_lease(tmp_path, monkeypatch):
    import time as clock

    from aml_agent.store import Store
    from aml_agent.workflow import Workflow

    store = Store(tmp_path / "runs.sqlite3")
    run = store.create({"account": {"bank": "1", "account": "A"}, "start": "2022-08-05T00:00:00", "as_of": "2022-08-06T00:00:00"})
    flow = Workflow.__new__(Workflow)
    flow.store = store
    state, save = flow.lease(run["id"])
    real = clock.time
    monkeypatch.setattr("aml_agent.store.time.time", lambda: real() + 600)  # the lease has expired
    state["status"] = "running"
    save()
    assert store.get(run["id"])["status"] == "running"


def test_local_pack_seed_carries_only_live_investigation_state(tmp_path, monkeypatch):
    from aml_agent import local_pack
    from aml_agent.store import Store

    source = Store(tmp_path / "source.sqlite")
    policy = source.artifact("tier_policy_version", {"label": "T3·v7"})
    source.artifact("rule_alert", {"account": "private"})
    source.artifact("investigation_grades", {"labels": "evaluation only"})
    source.set_program_pointer_any("tier-policy-current", policy)
    runtime = tmp_path / "pack/runtime"
    (runtime / "calibration").mkdir(parents=True)
    for file in local_pack.RUNTIME_FILES:
        (runtime / file).write_text("{}")
    local_pack.seed(source.path, runtime / "state-seed.sqlite")

    monkeypatch.setattr(local_pack, "ROOT", tmp_path / "teammate")
    merged = local_pack.merge_state(runtime, tmp_path / "teammate/.runtime/aml.sqlite")
    target = Store(tmp_path / "teammate/.runtime/aml.sqlite")
    assert merged["artifacts_added"] == 1 and sorted(merged["files_copied"]) == sorted(local_pack.RUNTIME_FILES)
    assert target.program_pointer("tier-policy-current") == policy
    assert target.artifacts("rule_alert") == [] and target.artifacts("investigation_grades") == []
    # Installing twice changes nothing.
    assert local_pack.merge_state(runtime, target.path)["artifacts_added"] == 0
