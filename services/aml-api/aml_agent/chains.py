"""Laundering chains: connected components of labelled laundering transactions (evaluation side).

Chain-level recall is the share of chains in which at least one account-day is alerted:
catching one account of a chain lets the investigators follow the rest. Chains are
built in Zetaris by label propagation over the partition's laundering edges; every
intermediate result is a parquet shard under the feature-store root. Labels are used
only here and by the rule learner, never by investigators.
"""

from __future__ import annotations

import json
import time

from .feature_store import ROOT_PATH, FeatureStore
from .zetaris import IN_SCOPE, timestamp

EDGES = ("agent_eval_chain_edges", ROOT_PATH + "/eval_chain_edges_v1")
LABELS = ("agent_eval_chain_label", ROOT_PATH + "/eval_chain_label_v1")
MEMBERS = ("agent_eval_chain_member", ROOT_PATH + "/eval_chain_member_v1")


def ref(side: str) -> str:
    return f"CONCAT({side}_bank, ':', {side}_account)"


class Chains:
    def __init__(self, feature_store: FeatureStore | None = None, log=print):
        self.fs = feature_store or FeatureStore()
        self.log = log
        self.totals: dict[str, int] = {}

    def register(self, table: str, path: str) -> None:
        self.fs.register_table(table, path)

    def build(self, split: str, start: str, end: str, max_rounds: int = 40) -> dict:
        """Edges of the partition, then min-label propagation until no account changes."""
        started = time.monotonic()
        self.fs.run(
            f"INSERT OVERWRITE DIRECTORY '{EDGES[1]}/split={split}' USING parquet SELECT * FROM ("
            f"SELECT CAST(transaction_id AS STRING) AS transaction_id, {ref('from')} AS src, {ref('to')} AS dst, "
            "CAST(transaction_time AS DATE) AS day, payment_format, "
            f"CASE WHEN {IN_SCOPE.format(p='')} THEN 1 ELSE 0 END AS in_scope "
            "FROM IBM_AML.hi_large_transactions WHERE is_laundering = 1 "
            f"AND transaction_time >= {timestamp(start + 'T00:00:00')} AND transaction_time < {timestamp(end + 'T00:00:00')}) shard"
        )
        self.register(*EDGES)
        edges = f"(SELECT src, dst FROM IBM_AML.{EDGES[0]} WHERE split = '{split}')"
        # Both directions plus self rows from one literal side table: Spark resolves each relation once.
        directed = (
            f"SELECT CASE WHEN s.k IN ('f', 'a') THEN e.src ELSE e.dst END AS a, "
            f"CASE WHEN s.k IN ('f', 'b') THEN e.dst ELSE e.src END AS b FROM {edges} e "
            "CROSS JOIN (SELECT 'f' AS k UNION ALL SELECT 'r' AS k UNION ALL SELECT 'a' AS k UNION ALL SELECT 'b' AS k) s"
        )
        self.fs.run(
            f"INSERT OVERWRITE DIRECTORY '{LABELS[1]}/split={split}/round=0' USING parquet SELECT * FROM ("
            f"SELECT DISTINCT a AS account, a AS chain FROM ({directed}) d) shard"
        )
        self.register(*LABELS)
        rounds = 0
        for rounds in range(1, max_rounds + 1):
            previous = f"(SELECT account, chain FROM IBM_AML.{LABELS[0]} WHERE split = '{split}' AND round = {rounds - 1})"
            self.fs.run(
                f"INSERT OVERWRITE DIRECTORY '{LABELS[1]}/split={split}/round={rounds}' USING parquet SELECT * FROM ("
                f"SELECT d.a AS account, MIN(l.chain) AS chain FROM ({directed}) d JOIN {previous} l ON l.account = d.b GROUP BY d.a) shard"
            )
            self.fs.run(f"REFRESH TABLE IBM_AML.{LABELS[0]}")
            changed = int(self.fs.run(
                f"SELECT COUNT(*) FROM (SELECT account, chain FROM IBM_AML.{LABELS[0]} WHERE split = '{split}' AND round = {rounds}) n "
                f"JOIN (SELECT account, chain FROM IBM_AML.{LABELS[0]} WHERE split = '{split}' AND round = {rounds - 1}) o "
                "ON o.account = n.account WHERE o.chain <> n.chain", 2)[0][0])
            self.log(json.dumps({"chain_round": rounds, "changed_accounts": changed}))
            if changed == 0:
                break
        # Members: every (chain, account, day) touched by a laundering transaction of the chain.
        labels = f"(SELECT account, chain FROM IBM_AML.{LABELS[0]} WHERE split = '{split}' AND round = {rounds})"
        side = "CROSS JOIN (SELECT 'src' AS k UNION ALL SELECT 'dst' AS k) s"
        self.fs.run(
            f"INSERT OVERWRITE DIRECTORY '{MEMBERS[1]}/split={split}' USING parquet SELECT * FROM ("
            "SELECT DISTINCT l.chain, x.account, x.day, x.in_scope FROM ("
            f"SELECT CASE WHEN s.k = 'src' THEN e.src ELSE e.dst END AS account, e.day, e.in_scope "
            f"FROM IBM_AML.{EDGES[0]} e {side} WHERE e.split = '{split}') x JOIN {labels} l ON l.account = x.account) shard"
        )
        self.register(*MEMBERS)
        return {"split": split, "rounds": rounds, "seconds": round(time.monotonic() - started, 1), **self.summary(split)}

    def members(self, split: str) -> str:
        return f"(SELECT chain, account, day, in_scope FROM IBM_AML.{MEMBERS[0]} WHERE split = '{split}')"

    def scoped_members(self, split: str) -> str:
        """(chain, account) of the in-scope chains: those with at least one ACH / Wire transfer."""
        return f"(SELECT DISTINCT chain, account FROM {self.members(split)} x WHERE in_scope = 1)"

    def member_accounts(self, split: str) -> str:
        """Every account of any laundering chain of the partition."""
        return f"(SELECT DISTINCT account FROM {self.members(split)} x)"

    def total(self, split: str) -> int:
        """Number of in-scope chains, cached per split."""
        if split not in self.totals:
            self.totals[split] = int(self.fs.run(f"SELECT COUNT(DISTINCT chain) FROM {self.scoped_members(split)} m", 2)[0][0])
        return self.totals[split]

    def summary(self, split: str) -> dict:
        row = self.fs.run(
            "SELECT COUNT(*), SUM(CASE WHEN scoped > 0 THEN 1 ELSE 0 END), SUM(CASE WHEN n = 2 THEN 1 ELSE 0 END), MAX(n), "
            "PERCENTILE_APPROX(n, 0.5), PERCENTILE_APPROX(n, 0.9) FROM ("
            f"SELECT chain, COUNT(DISTINCT account) AS n, SUM(in_scope) AS scoped FROM {self.members(split)} m GROUP BY chain) c", 2
        )[0]
        return {"chains": int(row[0]), "chains_in_scope": int(row[1] or 0), "two_account_chains": int(row[2] or 0),
                "largest_chain_accounts": int(row[3] or 0), "median_chain_accounts": float(row[4] or 0), "p90_chain_accounts": float(row[5] or 0)}

    def recall(self, split: str, flagged_sql: str) -> dict:
        """Chain recall of a flagged account-day relation (columns bank, account, day).

        A chain is recalled when any of its accounts is flagged on any day of the
        partition: one alerted member is enough for investigators to follow the chain.
        """
        row = self.fs.run(
            "SELECT COUNT(*), SUM(hit), SUM(CASE WHEN scoped > 0 THEN 1 ELSE 0 END), SUM(CASE WHEN scoped > 0 THEN hit ELSE 0 END) FROM ("
            "SELECT m.chain, MAX(CASE WHEN f.account IS NULL THEN 0 ELSE 1 END) AS hit, MAX(m.in_scope) AS scoped "
            f"FROM {self.members(split)} m LEFT JOIN (SELECT DISTINCT CONCAT(bank, ':', account) AS account FROM ({flagged_sql}) q) f "
            "ON f.account = m.account GROUP BY m.chain) c", 2
        )[0]
        chains, hit, scoped, scoped_hit = (int(v or 0) for v in row)
        # In scope: chains with at least one ACH/Wire transfer, the only formats the feature store observes.
        return {"chains": scoped, "chains_hit": scoped_hit, "chain_recall": scoped_hit / scoped if scoped else None,
                "chains_all": chains, "chain_recall_all": hit / chains if chains else None}

    def accounts(self, split: str, flagged_sql: str) -> dict:
        """Account-level view of a flagged relation (columns bank, account, day):

        full-chain account recall  share of member accounts of recalled chains that are themselves flagged
        chain precision            share of flagged accounts that belong to any laundering chain of the partition
        """
        members = self.scoped_members(split)
        flagged = f"(SELECT DISTINCT CONCAT(bank, ':', account) AS account FROM ({flagged_sql}) q)"
        row = self.fs.run(
            "SELECT SUM(CASE WHEN recalled = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN recalled = 1 THEN hit ELSE 0 END) FROM ("
            "SELECT m.chain, m.account, CASE WHEN f.account IS NULL THEN 0 ELSE 1 END AS hit, "
            "MAX(CASE WHEN f.account IS NULL THEN 0 ELSE 1 END) OVER (PARTITION BY m.chain) AS recalled "
            f"FROM {members} m LEFT JOIN {flagged} f ON f.account = m.account) z", 2)[0]
        precision = self.fs.run(
            f"SELECT COUNT(*), SUM(CASE WHEN c.account IS NULL THEN 0 ELSE 1 END) FROM {flagged} f "
            f"LEFT JOIN {self.member_accounts(split)} c ON c.account = f.account", 2)[0]
        recalled_members, flagged_members = int(row[0] or 0), int(row[1] or 0)
        flagged_accounts, in_chain = int(precision[0] or 0), int(precision[1] or 0)
        return {"recalled_chain_accounts": recalled_members, "recalled_chain_accounts_flagged": flagged_members,
                "full_chain_account_recall": flagged_members / recalled_members if recalled_members else None,
                "flagged_accounts": flagged_accounts, "flagged_accounts_in_chain": in_chain,
                "chain_precision": in_chain / flagged_accounts if flagged_accounts else None}

    def coverage(self, split: str, leafed_sql: str) -> tuple[dict[int, set], int]:
        """In-scope chains reachable from each leaf of a leaf-assigned account-day relation."""
        pairs = self.fs.run(
            f"SELECT DISTINCT m.chain, q.leaf FROM {self.scoped_members(split)} m JOIN (SELECT DISTINCT CONCAT(bank, ':', account) AS account, leaf "
            f"FROM ({leafed_sql}) z) q ON q.account = m.account", 2_000_000
        )
        cover: dict[int, set] = {}
        for chain, leaf in pairs:
            cover.setdefault(int(leaf), set()).add(str(chain))
        return cover, self.total(split)
