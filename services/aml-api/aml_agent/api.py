"""Loopback business API. Internal operations require a local bearer token."""

from __future__ import annotations

import hmac
import subprocess
import time
import threading
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .contracts import CaseRequest, StepRequest, StrictModel
from .evaluation import validate_partition
from .profile import PROFILE
from .providers import ModelError, Nvidia
from .rule_program import load_program
from .settings import ROOT, Settings
from .store import Conflict, Store
from .tools import Tools
from .zetaris import DataError
from .workflow import BudgetError, Workflow
from .zetaris import Zetaris


class ClaimRequest(StrictModel):
    worker_id: str


class RenewRequest(ClaimRequest):
    fence: int


class LiveRequest(StrictModel):
    alert_id: str = Field(min_length=1, max_length=100)


class AggregateRequest(StrictModel):
    world_context: str = Field(default="", max_length=20000)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    store = Store(settings.database)
    workflow = Workflow(store, Nvidia(settings.role("investigator")), Tools(Zetaris(settings)),
                        senior=Nvidia(settings.role("senior_investigator")))
    app = FastAPI(title="AML investigation API", version="0.1.0")
    # Investigations run in parallel: one step per run at a time and at most
    # `api_step_slots` steps at once across runs.
    run_locks: dict[str, threading.Lock] = {}
    registry_lock = threading.Lock()
    slots = threading.BoundedSemaphore(PROFILE["rule_learning"]["api_step_slots"])

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        if not settings.internal_token or not hmac.compare_digest(
            authorization or "", "Bearer " + settings.internal_token
        ):
            raise HTTPException(401, "Valid local API token required")

    @contextmanager
    def per_run(run_id: str):
        with registry_lock:
            run_lock = run_locks.setdefault(run_id, threading.Lock())
        if not run_lock.acquire(blocking=False):
            raise HTTPException(409, "This run already has a step in progress")
        try:
            if not slots.acquire(timeout=600):
                raise HTTPException(409, "All investigator slots are busy")
            try:
                yield
            except Conflict as error:
                raise HTTPException(409, str(error)) from error
            except KeyError as error:
                raise HTTPException(404, "Run not found") from error
            finally:
                slots.release()
        finally:
            run_lock.release()

    # Read-only dashboard on the loopback listener: static page, exported cycle data
    # and label-free alert networks. No credentials are sent to the browser.
    dashboard_dir = ROOT / "apps/dashboard"

    @app.middleware("http")
    async def dashboard_no_cache(request, call_next):
        # The page, its scripts and the exported data change together; never serve a stale copy.
        response = await call_next(request)
        if request.url.path.startswith("/dashboard"):
            response.headers["Cache-Control"] = "no-store"
        return response
    if dashboard_dir.exists():
        app.mount("/dashboard/static", StaticFiles(directory=dashboard_dir), name="dashboard")

    @app.get("/dashboard")
    def dashboard_page():
        return FileResponse(dashboard_dir / "index.html")

    @app.get("/dashboard/data.json")
    def dashboard_data():
        from .dashboard import OUTPUT, SNAPSHOT

        path = OUTPUT / "data.json" if (OUTPUT / "data.json").exists() else SNAPSHOT
        if not path.exists():
            raise HTTPException(404, "Run: python -m aml_agent.cli dashboard")
        return FileResponse(path, media_type="application/json")

    # Live investigation from the dashboard: only stored layer-1 alerts of the development or validation
    # partition, at most LIVE_SLOTS at once; the token stays on the server (the browser holds no secret).
    live: dict[str, subprocess.Popen] = {}

    @app.get("/dashboard/api/live/alerts")
    def live_alerts():
        from .live import candidates

        return JSONResponse(candidates(store))

    def launch(alert_id: str, attempt: int = 1, retry_of: str | None = None) -> str:
        """Create a team case for an alert and start its worker; remembered per alert for the dashboard."""
        from .live import remember, request_for

        request = request_for(store, alert_id)
        run_id = create_case(request)["id"]
        if attempt > 1:
            store.annotate(run_id, "attempt", attempt)
            store.annotate(run_id, "retry_of", retry_of)
        remember(f"{request.account.bank}:{request.account.account}", request.start.date().isoformat(), run_id)
        log = (ROOT / ".runtime/logs" / f"worker-{run_id}.log").open("w")
        live[run_id] = subprocess.Popen(["node", "--experimental-transform-types", str(ROOT / "services/orchestrator/team.ts"), run_id],
                                        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        return run_id

    def supervise() -> None:
        """A live run whose worker ended without the senior investigator's decision is marked failed and retried."""
        from .live import MAX_RETRIES

        while True:
            time.sleep(15)
            for run_id, process in list(live.items()):
                if process.poll() is None:
                    continue
                live.pop(run_id, None)
                try:
                    state = store.get(run_id)
                    if (state.get("adjudication") or {}).get("decision"):
                        continue
                    if state["status"] in ("pending", "running"):  # the worker died mid-run
                        store.annotate(run_id, "status", "failed")
                        store.annotate(run_id, "error", state.get("error") or f"Worker exited (code {process.returncode}) without a decision")
                    attempt = state.get("attempt", 1)
                    if attempt <= MAX_RETRIES:
                        retry = launch(state["request"]["alert_id"], attempt + 1, run_id)
                        store.annotate(run_id, "retried_by", retry)
                except Exception:  # supervision must never stop
                    continue

    threading.Thread(target=supervise, daemon=True).start()

    @app.post("/dashboard/api/live/investigate")
    def live_investigate(body: LiveRequest):
        from .live import LIVE_SLOTS, NEEDS_SETUP, missing_setup

        missing = missing_setup(settings)
        if missing:
            raise HTTPException(503, {**NEEDS_SETUP, "missing": missing})
        running = [run_id for run_id, process in live.items() if process.poll() is None]
        if len(running) >= LIVE_SLOTS:
            raise HTTPException(429, f"{len(running)} live investigations are running; wait for one to finish")
        try:
            run_id = launch(body.alert_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        return {"run_id": run_id}

    @app.get("/dashboard/api/live/runs/{run_id}")
    def live_progress(run_id: str):
        from .alert_trace import run_detail
        from .live import progress

        try:
            result = progress(store, run_id)
            result["detail"] = run_detail(store, run_id)
        except KeyError as error:
            raise HTTPException(404, "Unknown run") from error
        process = live.get(run_id)
        result["worker_running"] = bool(process and process.poll() is None)
        # Ended without a decision and not yet handled by the supervisor: a retry is about to start.
        from .live import MAX_RETRIES

        result["retry_pending"] = bool(process and process.poll() is not None and result["outcome"] != "investigated"
                                       and result["attempt"] <= MAX_RETRIES)
        return JSONResponse(result)

    @app.get("/dashboard/api/live/for")
    def live_for(account: str, day: str):
        from .live import latest

        return {"run_id": latest(account, day)}

    alert_db = {}

    def alert_index_db():
        from . import alert_index

        if alert_db.get("db") is None:  # only an opened index is cached; it may be built while the API runs
            alert_db["db"] = alert_index.connect()
        return alert_db["db"]

    @app.get("/dashboard/api/alerts")
    def alerts_page(page: int = 0, q: str = "", day: str = "", tier: str = "", rule: str = "", status: str = ""):
        from . import alert_index
        from .dashboard import OUTPUT, SNAPSHOT

        db = alert_index_db()
        if db is None:
            raise HTTPException(404, "Run: python -m aml_agent.cli alert-index")
        data = OUTPUT / "data.json" if (OUTPUT / "data.json").exists() else SNAPSHOT
        from .live import statuses

        return alert_index.page(db, alert_index.investigated(data), {"page": page, "q": q, "day": day, "tier": tier, "rule": rule, "status": status},
                                statuses(store))

    @app.get("/dashboard/api/alerts/summary")
    def alerts_summary():
        from . import alert_index

        db = alert_index_db()
        if db is None:
            raise HTTPException(404, "Run: python -m aml_agent.cli alert-index")
        return alert_index.summary(db)

    @app.get("/healthz")
    def health():
        return {
            "status": "ok",
            "provider": "nvidia_direct",
            "model": settings.model,
            "model_key_configured": bool(settings.nvidia_key),
            "nightshift_used": False,
        }

    @app.post("/v1/cases", dependencies=[Depends(authorized)])
    def create_case(body: CaseRequest):
        if body.assignment or body.parent_run_id:
            raise HTTPException(422, "Specialist sub-runs are created only by the team coordinator")
        rule_context = None
        if body.rule_version:
            try:
                package = load_program(store, body.rule_version)
            except (KeyError, ValueError) as error:
                raise HTTPException(422, "Unknown or invalid frozen rule version") from error
            if package["status"] == "research_reference" and body.split != "development":
                raise HTTPException(422, "Research reference is limited to development replay")
            if package["status"] not in ("research_reference", "accepted_experiment"):
                raise HTTPException(422, "Rule version is not an executable research version")
            rule_context = {
                "version": body.rule_version,
                "program": package["program"],
                "program_sha256": package["program_sha256"],
                "status": package["status"],
                # Development base rate of each rule group: a population statistic, not a case label.
                "group_stats": package.get("group_stats"),
                "production_eligible": False,
            }
            if body.alert_id:
                try:
                    alert = store.read_artifact(body.alert_id, "rule_alert")
                except KeyError as error:
                    raise HTTPException(422, "Unknown rule alert") from error
                if (
                    alert["base_version"] != body.rule_version
                    or alert["account"] != body.account.model_dump()
                    or alert["start"] != body.start.isoformat()
                    or alert["as_of"] != body.as_of.isoformat()
                ):
                    raise HTTPException(422, "Alert/account/window/version mismatch")
                rule_context["alert"] = {
                    key: alert[key]
                    for key in ("triggered_rule_ids", "features", "alert_sha256")
                }
                # Layer-2 tier and its development base rate: population statistics, not labels.
                if alert.get("tier"):
                    rule_context["tier"] = {key: alert.get(key) for key in ("tier", "tier_score", "tier_base_rate", "tier_model", "tier_source", "priority")}
        elif body.alert_id:
            raise HTTPException(422, "Rule alert requires a frozen version")
        # Fixed partitions are checked server-side, rather than trusting the client split label.
        try:
            validate_partition(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return store.create(body.model_dump(mode="json"), rule_context)

    @app.get("/v1/runs/{run_id}", dependencies=[Depends(authorized)])
    def get_run(run_id: str):
        try:
            state = store.get(run_id)
            state["cancel_requested"] = store.cancelled(run_id)
            return state
        except KeyError as error:
            raise HTTPException(404, "Run not found") from error

    @app.get("/v1/runs/{run_id}/trace", dependencies=[Depends(authorized)])
    def get_trace(run_id: str):
        try:
            store.get(run_id)
        except KeyError as error:
            raise HTTPException(404, "Run not found") from error
        return {"run_id": run_id, "events": store.trace_events(run_id)}

    @app.post("/internal/jobs/{run_id}/claim", dependencies=[Depends(authorized)])
    def claim(run_id: str, body: ClaimRequest):
        with per_run(run_id):
            return store.claim(run_id, body.worker_id)

    @app.post("/internal/runs/{run_id}/step", dependencies=[Depends(authorized)])
    def step(run_id: str, body: StepRequest):
        with per_run(run_id):
            return workflow.step(run_id, body)

    @app.post("/internal/runs/{run_id}/team", dependencies=[Depends(authorized)])
    def team(run_id: str):
        with per_run(run_id):
            try:
                return workflow.team(run_id)
            except DataError as error:
                raise HTTPException(503, f"Team setup failed on data access: {error}") from error
            except (BudgetError, ModelError) as error:
                raise HTTPException(503, f"Team setup stopped: {error}") from error
            except Exception as error:
                # Record the failure on the run, so it does not stay "running" behind a 500.
                state = store.get(run_id)
                state["status"], state["error"] = "failed", f"Team setup failed: {type(error).__name__}: {str(error)[:300]}"
                store.annotate(run_id, "status", state["status"])
                store.annotate(run_id, "error", state["error"])
                raise HTTPException(500, state["error"]) from error

    @app.post("/internal/runs/{run_id}/adjudicate", dependencies=[Depends(authorized)])
    def adjudicate_case(run_id: str):
        from .adjudication import adjudicate

        with per_run(run_id):
            try:
                return adjudicate(store, workflow.tools, run_id, workflow=workflow)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            except DataError as error:
                raise HTTPException(503, f"Adjudication failed on data access: {error}") from error

    @app.post("/internal/runs/{run_id}/aggregate", dependencies=[Depends(authorized)])
    def aggregate(run_id: str, body: AggregateRequest):
        with per_run(run_id):
            return workflow.aggregate(run_id, body.world_context)

    @app.post("/internal/jobs/{run_id}/renew", dependencies=[Depends(authorized)])
    def renew(run_id: str, body: RenewRequest):
        try:
            store.renew(run_id, body.worker_id, body.fence)
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        return {"renewed": True}

    @app.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(authorized)])
    def cancel(run_id: str):
        try:
            store.request_cancel(run_id)
            return {"cancel_requested": True, "in_flight_request_cancelled": False}
        except KeyError as error:
            raise HTTPException(404, "Run not found") from error

    return app
