"""Every validation alert of the active rule version, graded by the primary policy, in a local SQLite index.

Built once from Zetaris, label-free, day shard by day shard (`cli alert-index`). A gzip copy is committed so the
dashboard can page through all alerts without data access. Hit rule atoms and the grading reason of one alert are
derived in the browser from its features and the rules / policy in data.json.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3

from .dashboard import OUTPUT, SNAPSHOT
from .feature_registry import FEATURES
from .store import RULES_POINTER

DB = OUTPUT / "alerts.sqlite"
SNAPSHOT_DB = SNAPSHOT.parent / "alerts.sqlite.gz"
TIER_RANK = {"high": 0, "mid": 1, "low": 2}
PAGE = 20
OPENED: dict = {}  # the read-only connection, once the index exists (it may be built while a server runs)


def build(cycle, log=print) -> dict:
    from .alert_trace import rule_hits
    from .rule_learning import alert_row, condition_sql, partition_days
    from .rule_program import load_program, program_of
    from .showcase import tier_of
    from .tier_agent import TierPolicy, compile_policy
    from .tiering import score_sql

    store, learner = cycle.store, cycle.learner
    version = store.program_pointer(RULES_POINTER)
    program = program_of(load_program(store, version))
    _, primary = store.primary_policy()
    if primary is None or primary["rule_version"] != version:
        raise ValueError("The primary grading policy belongs to another rule version; publish one for the active version first")
    _, stored = cycle.tier_model(version)
    tier_expr, priority_expr = compile_policy(TierPolicy.model_validate(primary["policy"]), score_sql(stored["model"]))
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = DB.with_suffix(".building")
    partial.unlink(missing_ok=True)
    db = sqlite3.connect(partial)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("CREATE TABLE alerts (account TEXT, day TEXT, tier TEXT, tier_rank INTEGER, tier_rule TEXT, priority REAL, rules TEXT, "
               + ", ".join(f"{name} REAL" for name in FEATURES) + ")")
    columns = ["bank", "account", "day", *FEATURES]
    total = 0
    for day in partition_days("validation"):
        sql = (f"SELECT {', '.join(columns)}, {tier_expr} AS tier, {priority_expr} AS priority "
               f"FROM ({learner.features('validation', condition_sql(program), day)}) f")
        batch = []
        for row in learner.zetaris.run(sql, 200_000):
            alert, tier, priority = alert_row(columns, row), row[-2], float(row[-1])
            features = alert["features"]
            hits = [h["group"] for h in rule_hits(program, features) if h["hit"]]
            batch.append((f"{alert['bank']}:{alert['account']}", alert["day"], tier, TIER_RANK.get(tier, 3),
                          tier_of(primary["policy"], features).get("name"), priority, "," + ",".join(hits) + ",",
                          *[features[n] for n in FEATURES]))
        db.executemany(f"INSERT INTO alerts VALUES ({', '.join('?' * (7 + len(FEATURES)))})", batch)
        db.commit()
        total += len(batch)
        log(json.dumps({"day": day, "alerts": len(batch), "total": total}))
    db.execute("CREATE INDEX by_rank ON alerts (tier_rank, priority DESC)")
    db.execute("CREATE INDEX by_account ON alerts (account, day)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("rule_version", version), ("policy", primary["label"]), ("alerts", str(total))])
    db.commit()
    db.execute("VACUUM")
    db.close()
    partial.replace(DB)
    OPENED.pop("db", None)  # servers reopen the rebuilt file
    return {"alerts": total, "rule_version": version, "policy": primary["label"]}


def snapshot() -> int:
    """Commit-ready gzip copy of the index."""
    SNAPSHOT_DB.parent.mkdir(parents=True, exist_ok=True)
    with DB.open("rb") as source, gzip.open(SNAPSHOT_DB, "wb", compresslevel=9) as target:
        shutil.copyfileobj(source, target)
    return SNAPSHOT_DB.stat().st_size


def connect() -> sqlite3.Connection | None:
    """The local index (read-only, shared), unpacked from the committed snapshot on first use; None when there is none."""
    if OPENED.get("db") is not None:
        return OPENED["db"]
    if not DB.exists() and SNAPSHOT_DB.exists():
        OUTPUT.mkdir(parents=True, exist_ok=True)
        partial = DB.with_suffix(".unpacking")
        with gzip.open(SNAPSHOT_DB, "rb") as source, partial.open("wb") as target:
            shutil.copyfileobj(source, target)
        partial.replace(DB)
    if not DB.exists():
        return None
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    OPENED["db"] = db
    return db


def investigated(data_path) -> set[tuple[str, str]]:
    """Account-days with a finished investigation, from the exported dashboard data."""
    if not data_path.exists():
        return set()
    trace = json.loads(data_path.read_text()).get("trace") or {}
    return {(c["account"], c["day"]) for c in trace.get("cases", [])}


def page(db: sqlite3.Connection, done: set, query: dict, live: dict | None = None) -> dict:
    """One page of alerts, highest risk first; filters: q (account), day, tier, rule, status (investigated / running / failed / pending).

    `live` maps (account, day) to the status of its latest live investigation (from the full API).
    """
    where, args = [], []
    if query.get("q"):
        where.append("account LIKE ?"); args.append(f"%{query['q']}%")
    if query.get("day"):
        where.append("day = ?"); args.append(query["day"])
    if query.get("tier") in TIER_RANK:
        where.append("tier = ?"); args.append(query["tier"])
    if query.get("rule"):
        where.append("rules LIKE ?"); args.append(f"%,{query['rule']},%")
    live = live or {}
    done = set(done) | {k for k, v in live.items() if v["status"] == "investigated"}
    running = {k for k, v in live.items() if v["status"] == "running"} - done
    failed = {k for k, v in live.items() if v["status"] == "failed"} - done
    def among(pairs: set) -> tuple[str, list]:
        return ("(account || '|' || day) IN (" + ",".join("?" * len(pairs)) + ")" if pairs else "0"), [f"{a}|{d}" for a, d in sorted(pairs)]

    status = query.get("status")
    if status in ("investigated", "running", "failed"):
        clause, values = among({"investigated": done, "running": running, "failed": failed}[status])
        where.append(clause); args += values
    elif status == "pending":
        clause, values = among(done | running | failed)
        where.append(f"NOT {clause}"); args += values
    sql_where = " WHERE " + " AND ".join(where) if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM alerts{sql_where}", args).fetchone()[0]
    number = max(0, int(query.get("page") or 0))
    # Within a tier, investigated alerts come first so their recorded cases are easy to reach.
    first, first_args = among(done)
    rows = db.execute(f"SELECT * FROM alerts{sql_where} ORDER BY tier_rank, {first} DESC, priority DESC LIMIT ? OFFSET ?",
                      args + first_args + [PAGE, number * PAGE]).fetchall()
    return {
        "total": total, "page": number, "pages": max(1, -(-total // PAGE)), "size": PAGE,
        "rows": [{"account": r["account"], "day": r["day"], "tier": r["tier"], "tier_rule": r["tier_rule"], "priority": r["priority"],
                  "rules": [g for g in r["rules"].split(",") if g], "features": {n: r[n] for n in FEATURES},
                  "status": "investigated" if (r["account"], r["day"]) in done else "running" if (r["account"], r["day"]) in running
                  else "failed" if (r["account"], r["day"]) in failed else "pending",
                  "error": (live.get((r["account"], r["day"])) or {}).get("error") if (r["account"], r["day"]) in failed else None}
                 for r in rows],
    }


def summary(db: sqlite3.Connection) -> dict:
    """Totals for the filters and headline: alerts per tier and per day, rule groups, index metadata."""
    meta = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}
    tiers = {r["tier"]: r["n"] for r in db.execute("SELECT tier, COUNT(*) AS n FROM alerts GROUP BY tier")}
    days = [r["day"] for r in db.execute("SELECT DISTINCT day FROM alerts ORDER BY day")]
    rules = sorted({g for r in db.execute("SELECT DISTINCT rules FROM alerts") for g in r["rules"].split(",") if g})
    return {"meta": meta, "tiers": tiers, "days": days, "rules": rules}
