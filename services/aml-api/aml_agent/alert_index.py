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
from datetime import date, timedelta

from .dashboard import OUTPUT, SNAPSHOT
from .feature_registry import FEATURES

DB = OUTPUT / "alerts.sqlite"
SNAPSHOT_DB = SNAPSHOT.parent / "alerts.sqlite.gz"
TIER_RANK = {"high": 0, "mid": 1, "low": 2}
PAGE = 20


def build(cycle, log=print) -> dict:
    from .alert_trace import rule_hits
    from .rule_learning import condition_sql, day_range
    from .rule_program import load_program, program_of
    from .showcase import tier_of
    from .tier_agent import ALIAS, TierPolicy, compile_policy
    from .tiering import score_sql

    store, learner = cycle.store, cycle.learner
    version = store.program_pointer("learned-current")
    program = program_of(load_program(store, version))
    primary = store.read_artifact(store.program_pointer(ALIAS), "tier_policy_version")
    if primary["rule_version"] != version:
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
    start, end = (date.fromisoformat(d) for d in day_range("validation"))
    total, day = 0, start
    while day < end:
        sql = (f"SELECT {', '.join(columns)}, {tier_expr} AS tier, {priority_expr} AS priority "
               f"FROM ({learner.features('validation', condition_sql(program), day.isoformat())}) f")
        rows = learner.zetaris.run(sql, 200_000)
        batch = []
        for row in rows:
            record = dict(zip(columns + ["tier", "priority"], row))
            features = {n: None if record[n] is None else float(record[n]) for n in FEATURES}
            grading = tier_of(primary["policy"], features)
            hits = [h["group"] for h in rule_hits(program, features) if h["hit"]]
            batch.append((f"{record['bank']}:{record['account']}", str(record["day"]), record["tier"], TIER_RANK.get(record["tier"], 3),
                          grading.get("name"), float(record["priority"]), "," + ",".join(hits) + ",", *[features[n] for n in FEATURES]))
        db.executemany(f"INSERT INTO alerts VALUES ({', '.join('?' * (7 + len(FEATURES)))})", batch)
        db.commit()
        total += len(batch)
        log(json.dumps({"day": day.isoformat(), "alerts": len(batch), "total": total}))
        day += timedelta(days=1)
    db.execute("CREATE INDEX by_rank ON alerts (tier_rank, priority DESC)")
    db.execute("CREATE INDEX by_account ON alerts (account, day)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("rule_version", version), ("policy", primary["label"]), ("alerts", str(total))])
    db.commit()
    db.execute("VACUUM")
    db.close()
    partial.replace(DB)
    return {"alerts": total, "rule_version": version, "policy": primary["label"]}


def snapshot() -> int:
    """Commit-ready gzip copy of the index."""
    SNAPSHOT_DB.parent.mkdir(parents=True, exist_ok=True)
    with DB.open("rb") as source, gzip.open(SNAPSHOT_DB, "wb", compresslevel=9) as target:
        shutil.copyfileobj(source, target)
    return SNAPSHOT_DB.stat().st_size


def connect() -> sqlite3.Connection | None:
    """The local index, unpacked from the committed snapshot on first use."""
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
    keys = sorted(done)

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
    first = "(account || '|' || day) IN (" + ",".join("?" * len(keys)) + ")" if keys else "0"
    rows = db.execute(f"SELECT * FROM alerts{sql_where} ORDER BY tier_rank, {first} DESC, priority DESC LIMIT ? OFFSET ?",
                      args + [f"{a}|{d}" for a, d in keys] + [PAGE, number * PAGE]).fetchall()
    return {
        "total": total, "page": number, "pages": max(1, -(-total // PAGE)), "size": PAGE,
        "rows": [{"account": r["account"], "day": r["day"], "tier": r["tier"], "tier_rule": r["tier_rule"], "priority": r["priority"],
                  "rules": [g for g in r["rules"].split(",") if g], "features": {n: r[n] for n in FEATURES},
                  "investigated": (r["account"], r["day"]) in done,
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
