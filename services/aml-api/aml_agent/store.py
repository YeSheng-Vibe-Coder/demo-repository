"""Single-writer SQLite run ledger, lease fencing and durable idempotency."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .profile import PROFILE, PROFILE_SHA


class Conflict(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, version INTEGER NOT NULL,
                    lease_owner TEXT, lease_until REAL DEFAULT 0, fence INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS actions(
                    run_id TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, PRIMARY KEY(run_id,id));
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cancellations(run_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS trace_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    span_id TEXT NOT NULL, parent_span_id TEXT,
                    stage TEXT NOT NULL, at REAL NOT NULL, attributes TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS trace_by_run ON trace_events(run_id,sequence);
                CREATE TABLE IF NOT EXISTS program_registry(
                    name TEXT PRIMARY KEY, current_version TEXT NOT NULL,
                    previous_version TEXT, updated REAL NOT NULL);
            """)
        path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def create(self, request: dict, rule_context: dict | None = None) -> dict:
        run_id = str(uuid.uuid4())
        state = {
            "id": run_id,
            "request": request,
            "status": "pending",
            "state_version": 0,
            "rule_context": rule_context,
            "config_version": PROFILE["config_version"],
            "config_sha256": PROFILE_SHA,
            "created": time.time(),
            "plan_version": 0,
            "plan": [],
            "decisions": [],
            "evidence": {},
            "observations": [],
            "proposals": [],
            "accounts": {
                request["account"]["bank"]
                + ":"
                + request["account"]["account"]: request["account"]
            },
            "account_depths": {
                request["account"]["bank"] + ":" + request["account"]["account"]: 0
            },
            "budget": {
                "model_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "sql_requests": 0,
                "actions": 0,
                "replans": 0,
            },
        }
        with self.transaction() as db:
            db.execute(
                "INSERT INTO runs(id,state,version) VALUES(?,?,0)",
                (run_id, json.dumps(state)),
            )
        return state

    def seed(self, state: dict) -> None:
        """Initialise a freshly created, never-leased run (e.g. a specialist sub-run)."""
        with self.transaction() as db:
            result = db.execute(
                "UPDATE runs SET state=? WHERE id=? AND version=0 AND lease_owner IS NULL",
                (json.dumps(state), state["id"]),
            )
            if result.rowcount != 1:
                raise Conflict("Only an unclaimed version-0 run can be seeded")

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row["state"])

    def save(self, state: dict, expected: int, owner: str, fence: int) -> None:
        next_state = {**state, "state_version": expected + 1}
        with self.transaction() as db:
            result = db.execute(
                "UPDATE runs SET state=?,version=? WHERE id=? AND version=? AND lease_owner=? AND fence=? AND lease_until>?",
                (
                    json.dumps(next_state),
                    expected + 1,
                    state["id"],
                    expected,
                    owner,
                    fence,
                    time.time(),
                ),
            )
            if result.rowcount != 1:
                raise Conflict("State version or lease fencing conflict")
        state.update(next_state)

    def set_program_pointer_any(self, name: str, artifact_id: str) -> None:
        """Point an alias at any artifact (tier policies), keeping the previous target for rollback."""
        with self.transaction() as db:
            row = db.execute("SELECT current_version FROM program_registry WHERE name=?", (name,)).fetchone()
            db.execute(
                "INSERT INTO program_registry VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET current_version=excluded.current_version,previous_version=excluded.previous_version,updated=excluded.updated",
                (name, artifact_id, row[0] if row else None, time.time()),
            )

    def annotate(self, run_id: str, key: str, value) -> None:
        """Attach a post-investigation record (e.g. adjudication) to a finished run's state."""
        with self.transaction() as db:
            row = db.execute("SELECT state, version FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            state = json.loads(row["state"])
            state[key] = value
            state["state_version"] = row["version"] + 1
            db.execute("UPDATE runs SET state=?, version=? WHERE id=? AND version=?",
                       (json.dumps(state), row["version"] + 1, run_id, row["version"]))

    def claim(self, run_id: str, worker: str) -> dict:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["lease_until"] > time.time() and row["lease_owner"] != worker:
                raise Conflict("Run already leased")
            fence = (
                row["fence"]
                if row["lease_owner"] == worker and row["lease_until"] > time.time()
                else row["fence"] + 1
            )
            db.execute(
                "UPDATE runs SET lease_owner=?,lease_until=?,fence=? WHERE id=?",
                (worker, time.time() + 420, fence, run_id),
            )
            return {"fence": fence, "lease_seconds": 420}

    def renew(self, run_id: str, worker: str, fence: int) -> None:
        with self.transaction() as db:
            result = db.execute(
                "UPDATE runs SET lease_until=? WHERE id=? AND lease_owner=? AND fence=? AND lease_until>?",
                (time.time() + 420, run_id, worker, fence, time.time()),
            )
            if result.rowcount != 1:
                raise Conflict("Lease expired or fenced")

    def begin_action(self, run_id: str, action_id: str) -> dict | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM actions WHERE run_id=? AND id=?", (run_id, action_id)
            ).fetchone()
            if row:
                if row["status"] != "completed":
                    raise Conflict(
                        "Interrupted action requires reconciliation; refusing duplicate dispatch"
                    )
                return json.loads(row["result"])
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,NULL)", (run_id, action_id, "started")
            )
        return None

    def complete_action(self, run_id: str, action_id: str, result: dict) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE actions SET status=?,result=? WHERE run_id=? AND id=?",
                ("completed", json.dumps(result), run_id, action_id),
            )

    def artifact(self, kind: str, body: dict) -> str:
        identifier = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?)",
                (identifier, kind, json.dumps(body)),
            )
        return identifier

    def artifacts(self, kind: str) -> list[tuple[str, dict]]:
        """All artifacts of one kind, oldest first."""
        with self.connect() as db:
            rows = db.execute("SELECT id, body FROM artifacts WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
        return [(row["id"], json.loads(row["body"])) for row in rows]

    def read_artifact(self, identifier: str, kind: str | None = None) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT kind,body FROM artifacts WHERE id=?", (identifier,)
            ).fetchone()
        if row is None or kind is not None and row["kind"] != kind:
            raise KeyError(identifier)
        return json.loads(row["body"])

    def request_cancel(self, run_id: str) -> None:
        self.get(run_id)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO cancellations VALUES(?)", (run_id,))

    def trace(
        self,
        run_id: str,
        stage: str,
        *,
        span_id: str,
        parent_span_id: str | None = None,
        **attributes,
    ) -> None:
        forbidden = {
            "prompt",
            "messages",
            "api_key",
            "token",
            "authorization",
            "rows",
            "arguments",
            "reasoning_content",
        }
        if forbidden.intersection(attributes):
            raise ValueError("Sensitive raw content is prohibited in tracing")
        with self.transaction() as db:
            db.execute(
                "INSERT INTO trace_events(run_id,span_id,parent_span_id,stage,at,attributes) VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    span_id,
                    parent_span_id,
                    stage,
                    time.time(),
                    json.dumps(attributes),
                ),
            )

    def trace_events(self, run_id: str) -> list[dict]:
        self.get(run_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM trace_events WHERE run_id=? ORDER BY sequence", (run_id,)
            ).fetchall()
        return [
            {**dict(row), "attributes": json.loads(row["attributes"])} for row in rows
        ]

    def cancelled(self, run_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM cancellations WHERE run_id=?", (run_id,)
                ).fetchone()
                is not None
            )

    def program_pointer(self, name: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT current_version FROM program_registry WHERE name=?", (name,)
            ).fetchone()
        return row[0] if row else None

    def set_program_pointer(
        self, name: str, version: str, *, expected: str | None
    ) -> None:
        self.read_artifact(version, "rule_program_version")
        with self.transaction() as db:
            row = db.execute(
                "SELECT current_version FROM program_registry WHERE name=?", (name,)
            ).fetchone()
            actual = row[0] if row else None
            if actual != expected:
                raise Conflict(
                    "Active rule changed; evaluation must be repeated against actual parent"
                )
            db.execute(
                "INSERT INTO program_registry VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET current_version=excluded.current_version,previous_version=excluded.previous_version,updated=excluded.updated",
                (name, version, actual, time.time()),
            )

    def rollback_program(self, name: str, expected: str) -> str:
        with self.transaction() as db:
            row = db.execute(
                "SELECT current_version,previous_version FROM program_registry WHERE name=?",
                (name,),
            ).fetchone()
            if not row or row[0] != expected or not row[1]:
                raise Conflict("Rollback target/current rule mismatch")
            db.execute(
                "UPDATE program_registry SET current_version=?,previous_version=?,updated=? WHERE name=?",
                (row[1], row[0], time.time(), name),
            )
            return row[1]
