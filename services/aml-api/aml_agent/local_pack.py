"""Local data pack: the live-investigation slice of IBM HI-Large for a teammate's own Zetaris.

A live investigation reads only `aml.transactions` (label-free) and the three feature tables
`IBM_AML.agent_day_{aggregates,transit,history}`. The pack carries exactly those for the validation
period plus 30 days of history (what baselines and new-counterparty checks look back over), and the
state the investigation team needs (active rule version, primary grading policy, calibration,
investigator config). It is installed into a local Zetaris platform and queried through the same
Zetaris REST SQL as the VM, so no code path changes.

  build    (owner, with VM access)  raw export + .runtime state -> aml-local-pack.tar, with expected counts
  install  (teammate)                register the tables in their Zetaris, merge the state, verify the counts

Layout of the pack (the `ibm/` folder is copied into the Zetaris customer-data volume at shared/ibm):

  ibm/aml_pack/transactions/transactions.parquet   in-scope transactions, no label column
  ibm/aml_agent/day_*_v1/day=YYYY-MM-DD/           feature shards, as stored on the VM
  runtime/state-seed.sqlite                         rule versions, grading policies, tier models, pointers
  runtime/calibration/development.json, runtime/investigator-config.json
  MANIFEST.json                                     window, counts and file list
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import time
from datetime import date, timedelta
from pathlib import Path

from .feature_store import ROOT_PATH, SHARDS, FeatureStore
from .settings import ROOT, Settings
from .zetaris import SCOPE, DataError, timestamp

START, END = "2022-08-05", "2022-09-26"
RAW = ROOT / ".runtime/local-data/raw"
OUTPUT = ROOT / ".runtime/local-data"
NAME = "aml-local-pack"
TRANSACTIONS_PATH = ROOT_PATH.replace("/aml_agent", "/aml_pack") + "/transactions"
FEATURE_SHARDS = ("aggregates", "transit", "history")
SEED_KINDS = ("rule_program_version", "tier_policy_version", "risk_tier_model")
SEED_POINTERS = ("learned-current", "tier-policy-current")
RUNTIME_FILES = ("calibration/development.json", "investigator-config.json")


def checks() -> dict[str, str]:
    """Count queries whose answers must match between the VM and a local install."""
    window = SCOPE.format(p="", start=timestamp(START + "T00:00:00"), end=timestamp(END + "T00:00:00"))
    out = {"transactions": f"SELECT COUNT(*), MIN(transaction_id), MAX(transaction_id) FROM aml.transactions WHERE {window}"}
    for part in FEATURE_SHARDS:
        table = SHARDS[part][0]
        out[table] = f"SELECT COUNT(*) FROM IBM_AML.{table} WHERE day >= DATE '{START}' AND day < DATE '{END}'"
    return out


def days() -> list[str]:
    day, end, out = date.fromisoformat(START), date.fromisoformat(END), []
    while day < end:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def build(settings: Settings | None = None, log=print) -> Path:
    """Assemble the pack from the VM export in .runtime/local-data/raw and this machine's state."""
    settings = settings or Settings.load()
    source = RAW / "transactions.parquet"
    if not source.exists():
        raise FileNotFoundError("Run scripts/export_local_pack.sh first: it exports the raw slice from the VM")
    folder = OUTPUT / NAME
    if folder.exists():
        shutil.rmtree(folder)
    target = folder / "ibm/aml_pack/transactions"
    target.mkdir(parents=True)
    shutil.copy2(source, target / "transactions.parquet")
    shards = {}
    for part in FEATURE_SHARDS:
        name = SHARDS[part][1].rsplit("/", 1)[1]
        present = sorted(p.name for p in (RAW / name).glob("day=*"))
        shutil.copytree(RAW / name, folder / "ibm/aml_agent" / name)
        shards[name] = present
    runtime = folder / "runtime"
    (runtime / "calibration").mkdir(parents=True)
    for file in RUNTIME_FILES:
        shutil.copy2(ROOT / ".runtime" / file, runtime / file)
    seed(settings.database, runtime / "state-seed.sqlite")
    # Expected answers, from the source Zetaris: the install must reproduce them.
    store = FeatureStore(settings)
    expected = {name: [str(v) for v in store.run(sql)[0]] for name, sql in checks().items()}
    manifest = {
        "name": NAME, "created": time.time(), "window": [START, END], "days": days(), "shards": shards,
        "transactions_path": TRANSACTIONS_PATH, "feature_root": ROOT_PATH, "expected": expected,
        "label_free": True, "source": "IBM HI-Large v8 (CDLA-Sharing-1.0), in-scope ACH/Wire transfers without self transfers",
    }
    (folder / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    archive = OUTPUT / f"{NAME}.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(folder, arcname=NAME)
    log(json.dumps({"archive": str(archive.relative_to(ROOT)), "bytes": archive.stat().st_size, "expected": expected}))
    return archive


def seed(database: Path, target: Path) -> None:
    """Only what a live investigation reads: rule versions, grading policies and tier models, plus the two pointers."""
    from .store import Store

    Store(target)
    with sqlite3.connect(f"file:{target}", uri=True) as db:
        db.execute("ATTACH DATABASE ? AS source", (f"file:{database}?mode=ro",))
        db.execute(f"INSERT INTO artifacts SELECT * FROM source.artifacts WHERE kind IN ({','.join('?' * len(SEED_KINDS))})", SEED_KINDS)
        db.execute(f"INSERT INTO program_registry SELECT * FROM source.program_registry WHERE name IN ({','.join('?' * len(SEED_POINTERS))})",
                   SEED_POINTERS)
        db.commit()
        db.execute("DETACH DATABASE source")
    with sqlite3.connect(target) as db:
        db.execute("VACUUM")


def install(pack: Path, settings: Settings | None = None, log=print) -> dict:
    """Register the copied pack in this Zetaris, merge its state into .runtime, and verify the counts."""
    settings = settings or Settings.load()
    if not settings.zetaris_key:
        raise ValueError("Set ZETARIS_API_KEY (and ZETARIS_REST_URL / ZETARIS_ORG_ID) in .env.local first")
    manifest = json.loads((pack / "MANIFEST.json").read_text())
    store = FeatureStore(settings)
    for database, about in (("AML", "AML agent: label-free transactions"), ("IBM_AML", "AML agent: feature store")):
        try:
            store.run(f'CREATE LIGHTNING DATABASE {database} DESCRIBE BY "{about}" OPTIONS ()')
        except DataError:
            pass  # already registered
    tables = [("transactions", "AML", manifest["transactions_path"])] + [
        (SHARDS[part][0], "IBM_AML", SHARDS[part][1]) for part in FEATURE_SHARDS
    ]
    for table, database, path in tables:
        try:
            store.run(f'CREATE LIGHTNING FILESTORE TABLE {table} FROM {database} FORMAT PARQUET OPTIONS (PATH "{path}", mergeSchema "false")')
        except DataError:
            store.run(f"REFRESH TABLE {database}.{table}")
        log(json.dumps({"registered": f"{database}.{table}"}))
    merged = merge_state(pack / "runtime", settings.database)
    found = {name: [str(v) for v in store.run(sql)[0]] for name, sql in checks().items()}
    mismatched = {k: {"expected": v, "found": found[k]} for k, v in manifest["expected"].items() if found.get(k) != v}
    return {"tables": [f"{d}.{t}" for t, d, _ in tables], "state": merged, "counts": found, "verified": not mismatched,
            "mismatched": mismatched}


def merge_state(runtime: Path, database: Path) -> dict:
    """Add the seed's artifacts and pointers to the local state database; copy the config files when absent."""
    from .store import Store

    Store(database)
    with sqlite3.connect(f"file:{database}", uri=True) as db:
        db.execute("ATTACH DATABASE ? AS seed", (f"file:{runtime / 'state-seed.sqlite'}?mode=ro",))
        added = db.execute("INSERT OR IGNORE INTO artifacts SELECT * FROM seed.artifacts").rowcount
        db.execute("INSERT OR REPLACE INTO program_registry SELECT * FROM seed.program_registry")
        db.commit()
        db.execute("DETACH DATABASE seed")
    copied = []
    for file in RUNTIME_FILES:
        target = ROOT / ".runtime" / file
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(runtime / file, target)
            copied.append(file)
    return {"artifacts_added": added, "pointers": list(SEED_POINTERS), "files_copied": copied}
