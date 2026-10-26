"""Zetaris-materialised feature store: day-sharded account-day features and labels.

All computation runs inside Zetaris on the VM; only small results (sampled rows,
alerts, confusion counts) leave it. Shards are parquet directories written by
INSERT OVERWRITE DIRECTORY and registered as Lightning filestore tables:

  IBM_AML.agent_account_day              label-free registered features per account-day
  IBM_AML.agent_eval_account_day_label   evaluation-only window labels (never agent-readable)

A window is (account, day D) with cutoff D + 24h; features read [D - 24h, D + 24h).
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta

import httpx

from .settings import Settings
from .zetaris import SCOPE, DataError, timestamp

ROOT_PATH = "file:///home/zetaris/customer-data/shared/ibm/aml_agent"
LABEL_TABLE = "IBM_AML.agent_eval_account_day_label"
SHARDS = {
    # name -> (Lightning table, parquet root)
    "aggregates": ("agent_day_aggregates", ROOT_PATH + "/day_aggregates_v1"),
    "transit": ("agent_day_transit", ROOT_PATH + "/day_transit_v1"),
    "labels": ("agent_eval_account_day_label", ROOT_PATH + "/eval_account_day_label_v1"),
    "history": ("agent_day_history", ROOT_PATH + "/day_history_v1"),
}
HISTORY_DAYS = 7
AGGREGATE_COLUMNS = [
    "fan_in_24h", "fan_in_48h", "fan_out_24h", "fan_out_48h", "in_count_24h", "out_count_24h",
    "in_banks_24h", "out_banks_24h", "pass_through_ratio_24h", "currency_count_24h", "activity_change_24h",
]
TRANSIT_DEFAULTS = {
    "transit_gap_hours": "NULL",
    "transit_pairs_24h": "0",
    "round_trip_48h": "0",
    "onward_currency_switch_48h": "0",
}
HISTORY_COLUMNS = ["new_counterparty_share_24h", "new_counterparties_24h", "history_active_days_7d", "amount_vs_history_24h"]


def features_relation(where: str = "TRUE") -> str:
    """Composite Zetaris query: aggregate shard LEFT JOIN transit and history shards per account-day."""
    transit = ", ".join(
        f"COALESCE(tr.{name}, {default}) AS {name}" if default != "NULL" else f"tr.{name} AS {name}"
        for name, default in TRANSIT_DEFAULTS.items()
    )
    return (
        "SELECT g.bank, g.account, g.day, "
        + ", ".join(f"g.{name}" for name in AGGREGATE_COLUMNS)
        + f", {transit}, "
        + ", ".join(f"COALESCE(h.{name}, 0) AS {name}" for name in HISTORY_COLUMNS)
        + " FROM IBM_AML.agent_day_aggregates g "
        "LEFT JOIN IBM_AML.agent_day_transit tr ON tr.bank = g.bank AND tr.account = g.account AND tr.day = g.day "
        "LEFT JOIN IBM_AML.agent_day_history h ON h.bank = g.bank AND h.account = g.account AND h.day = g.day "
        f"WHERE {where}"
    )


class FeatureStore:
    """Write path for the data work package; agent tools never call this."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()

    def run(self, sql: str, limit: int = 1000) -> list[list]:
        allowed = (
            sql.startswith(f"INSERT OVERWRITE DIRECTORY '{ROOT_PATH}/")
            or sql.startswith("CREATE LIGHTNING FILESTORE TABLE agent_")
            or sql.startswith("REFRESH TABLE IBM_AML.agent_")
            # Local data pack install (local_pack.py): its two databases and the label-free transactions table.
            or sql.startswith(('CREATE LIGHTNING DATABASE AML DESCRIBE', 'CREATE LIGHTNING DATABASE IBM_AML DESCRIBE'))
            or sql.startswith(("CREATE LIGHTNING FILESTORE TABLE transactions FROM AML ", "REFRESH TABLE AML.transactions"))
            or sql.startswith(("SELECT ", "WITH "))
        )
        if not allowed or ";" in sql:
            raise DataError("Feature store accepts only its own shard writes and reads")
        # Read-only statements and idempotent shard overwrites may be re-sent after a
        # dropped connection or a 5xx; the SSH tunnel can blip during long runs.
        for attempt in range(4):
            try:
                response = httpx.post(
                    self.settings.zetaris_url + "/sql-editor/sqls/run",
                    headers={
                        "Authorization": "Bearer " + self.settings.zetaris_key,
                        "X-Org-ID": self.settings.org_id,
                        "X-Request-ID": str(uuid.uuid4()),
                    },
                    json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
                    timeout=1800,
                )
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
                if attempt == 3:
                    raise
                time.sleep(20)
                continue
            if response.status_code < 500 or attempt == 3:
                break
            time.sleep(20)
        if response.status_code != 200:
            raise DataError(f"Zetaris HTTP {response.status_code}: {response.text[:200]}")
        body = response.json(parse_float=str)
        if not isinstance(body, dict) or body.get("error") or body.get("success") is False:
            raise DataError("Zetaris reported a feature-store error")
        return body.get("data") or []

    @staticmethod
    def window(day: str) -> tuple[str, str, str]:
        start = datetime.fromisoformat(day)
        prior, end = start - timedelta(hours=24), start + timedelta(hours=24)
        return tuple(timestamp(x.isoformat()) for x in (prior, start, end))

    @classmethod
    def aggregate_sql(cls, day: str) -> str:
        """Every account active on `day`: counts, counterparties, banks, ratio, activity change.

        A strictly linear CTE chain: Spark fails to resolve a view-backed relation
        that is referenced twice, so each stage feeds exactly one consumer.
        """
        prior_ts, start_ts, end_ts = cls.window(day)

        def pick(incoming: str, outgoing: str) -> str:
            return f"CASE WHEN s.side = 'incoming' THEN {incoming} ELSE {outgoing} END"

        current = f"t.transaction_time >= {start_ts}"
        counterparty = pick(
            "CONCAT(t.from_bank,':',t.from_account)", "CONCAT(t.to_bank,':',t.to_account)"
        )

        def by_side(column: str, side: str, function: str = "SUM") -> str:
            return f"{function}(CASE WHEN side = '{side}' THEN {column} ELSE 0 END)"

        return (
            "WITH t AS (SELECT transaction_id, transaction_time, from_bank, from_account, to_bank, to_account, "
            "amount_received, receiving_currency, amount_paid, payment_currency FROM aml.transactions "
            f"WHERE {SCOPE.format(p='', start=prior_ts, end=end_ts)}), "
            "side_currency AS (SELECT "
            f"{pick('t.to_bank', 't.from_bank')} AS bank, {pick('t.to_account', 't.from_account')} AS account, "
            f"s.side, {pick('t.receiving_currency', 't.payment_currency')} AS currency, "
            f"SUM(CASE WHEN {current} THEN 1 ELSE 0 END) AS n24, SUM(CASE WHEN {current} THEN 0 ELSE 1 END) AS nprior, "
            f"COUNT(DISTINCT CASE WHEN {current} THEN {counterparty} END) AS cp24, COUNT(DISTINCT {counterparty}) AS cp48, "
            f"COUNT(DISTINCT CASE WHEN {current} THEN {pick('t.from_bank', 't.to_bank')} END) AS banks24, "
            f"SUM(CASE WHEN {current} THEN {pick('t.amount_received', 't.amount_paid')} ELSE 0 END) AS amount24 "
            "FROM t CROSS JOIN (SELECT 'incoming' AS side UNION ALL SELECT 'outgoing' AS side) s GROUP BY 1, 2, 3, 4), "
            "currency_level AS (SELECT bank, account, currency, "
            f"{by_side('n24', 'incoming')} AS n_in, {by_side('n24', 'outgoing')} AS n_out, SUM(nprior) AS n_prior, "
            f"{by_side('cp24', 'incoming', 'MAX')} AS cp24_in, {by_side('cp48', 'incoming', 'MAX')} AS cp48_in, "
            f"{by_side('cp24', 'outgoing', 'MAX')} AS cp24_out, {by_side('cp48', 'outgoing', 'MAX')} AS cp48_out, "
            f"{by_side('banks24', 'incoming', 'MAX')} AS banks_in, {by_side('banks24', 'outgoing', 'MAX')} AS banks_out, "
            f"LEAST(COALESCE({by_side('amount24', 'outgoing')} / NULLIF({by_side('amount24', 'incoming')}, 0), 0), 10) AS ratio "
            "FROM side_currency GROUP BY 1, 2, 3) "
            f"SELECT bank, account, DATE '{day}' AS day, "
            "MAX(cp24_in) AS fan_in_24h, MAX(cp48_in) AS fan_in_48h, MAX(cp24_out) AS fan_out_24h, MAX(cp48_out) AS fan_out_48h, "
            "SUM(n_in) AS in_count_24h, SUM(n_out) AS out_count_24h, MAX(banks_in) AS in_banks_24h, MAX(banks_out) AS out_banks_24h, "
            "CAST(MAX(ratio) AS DOUBLE) AS pass_through_ratio_24h, "
            "COUNT(DISTINCT CASE WHEN n_in + n_out > 0 THEN currency END) AS currency_count_24h, "
            "CAST(SUM(n_in + n_out) / (SUM(n_prior) + 1.0) AS DOUBLE) AS activity_change_24h "
            "FROM currency_level GROUP BY 1, 2 HAVING SUM(n_in + n_out) > 0"
        )

    @classmethod
    def transit_sql(cls, day: str) -> str:
        """Receive-then-send pairs per receiving account; outgoing leg on `day`, any currency."""
        prior_ts, start_ts, end_ts = cls.window(day)
        gap = "(UNIX_TIMESTAMP(b.transaction_time) - UNIX_TIMESTAMP(a.transaction_time)) / 3600.0"
        return (
            "WITH t AS (SELECT transaction_id, transaction_time, from_bank, from_account, to_bank, to_account, "
            # Base table with an explicit label-free column list: Spark cannot write a
            # self-join over the view. is_laundering is never selected here.
            "receiving_currency, payment_currency FROM IBM_AML.hi_large_transactions "
            f"WHERE {SCOPE.format(p='', start=prior_ts, end=end_ts)}) "
            "SELECT a.to_bank AS bank, a.to_account AS account, "
            f"DATE '{day}' AS day, CAST(MIN({gap}) AS DOUBLE) AS transit_gap_hours, "
            f"SUM(CASE WHEN {gap} <= 24 THEN 1 ELSE 0 END) AS transit_pairs_24h, "
            "COUNT(DISTINCT CASE WHEN b.to_bank = a.from_bank AND b.to_account = a.from_account THEN b.transaction_id END) AS round_trip_48h, "
            "COUNT(DISTINCT CASE WHEN b.payment_currency <> a.receiving_currency THEN b.transaction_id END) AS onward_currency_switch_48h "
            "FROM t a JOIN t b ON b.from_bank = a.to_bank AND b.from_account = a.to_account "
            "AND b.transaction_time > a.transaction_time AND b.transaction_id <> a.transaction_id "
            f"AND b.transaction_time >= {start_ts} AND b.transaction_time <= a.transaction_time + INTERVAL 48 HOURS "
            "GROUP BY 1, 2"
        )

    @classmethod
    def history_sql(cls, day: str) -> str:
        """The account's last 24h against its own prior 7 days: new counterparties, dormancy, outsized amounts.

        History is [D - 7d, D) and the current window [D, D + 24h); both end before the cutoff.
        Base table with an explicit label-free column list (Spark cannot self-join the view).
        """
        start = datetime.fromisoformat(day)
        lookback, current, end = (timestamp(x.isoformat()) for x in (start - timedelta(days=HISTORY_DAYS), start, start + timedelta(hours=24)))
        side = "CROSS JOIN (SELECT 'out' AS k UNION ALL SELECT 'in' AS k) x"
        counterparty = "CASE WHEN x.k = 'out' THEN CONCAT(to_bank, ':', to_account) ELSE CONCAT(from_bank, ':', from_account) END"

        def pick(outgoing: str, incoming: str) -> str:
            return f"CASE WHEN x.k = 'out' THEN {outgoing} ELSE {incoming} END"

        return (
            "WITH t AS (SELECT transaction_time, from_bank, from_account, to_bank, to_account, amount_paid, payment_currency "
            f"FROM IBM_AML.hi_large_transactions WHERE {SCOPE.format(p='', start=lookback, end=end)}), "
            f"s AS (SELECT {pick('from_bank', 'to_bank')} AS bank, {pick('from_account', 'to_account')} AS account, "
            f"{counterparty} AS cp, "
            f"transaction_time AS ts, amount_paid AS amount, payment_currency AS currency FROM t {side}), "
            f"pairs AS (SELECT DISTINCT bank, account, cp FROM s WHERE ts < {current}), "
            f"active AS (SELECT bank, account, COUNT(DISTINCT CAST(ts AS DATE)) AS days FROM s WHERE ts < {current} GROUP BY bank, account), "
            f"peak AS (SELECT bank, account, currency, MAX(amount) AS amount FROM s WHERE ts < {current} GROUP BY bank, account, currency), "
            f"now AS (SELECT bank, account, cp, currency, amount FROM s WHERE ts >= {current}), "
            "fresh AS (SELECT n.bank, n.account, COUNT(DISTINCT n.cp) AS cps, COUNT(DISTINCT CASE WHEN p.cp IS NULL THEN n.cp END) AS new_cps "
            "FROM now n LEFT JOIN pairs p ON p.bank = n.bank AND p.account = n.account AND p.cp = n.cp GROUP BY n.bank, n.account), "
            "ratio AS (SELECT n.bank, n.account, MAX(LEAST(n.amount / (COALESCE(k.amount, 0) + 1), 1000)) AS ratio "
            "FROM now n LEFT JOIN peak k ON k.bank = n.bank AND k.account = n.account AND k.currency = n.currency GROUP BY n.bank, n.account) "
            f"SELECT f.bank, f.account, DATE '{day}' AS day, CAST(f.new_cps / f.cps AS DOUBLE) AS new_counterparty_share_24h, "
            "f.new_cps AS new_counterparties_24h, COALESCE(a.days, 0) AS history_active_days_7d, CAST(r.ratio AS DOUBLE) AS amount_vs_history_24h "
            "FROM fresh f LEFT JOIN active a ON a.bank = f.bank AND a.account = f.account "
            "LEFT JOIN ratio r ON r.bank = f.bank AND r.account = f.account"
        )

    @staticmethod
    def label_sql(day: str) -> str:
        start = datetime.fromisoformat(day)
        end = start + timedelta(hours=24)
        scope = SCOPE.format(p="", start=timestamp(start.isoformat()), end=timestamp(end.isoformat()))
        # Literal side table: Spark cannot resolve a UNION that reads one table twice.
        return (
            f"SELECT CASE WHEN s.side = 'to' THEN to_bank ELSE from_bank END AS bank, "
            "CASE WHEN s.side = 'to' THEN to_account ELSE from_account END AS account, "
            f"DATE '{day}' AS day, MAX(is_laundering) AS label "
            "FROM IBM_AML.hi_large_transactions CROSS JOIN (SELECT 'to' AS side UNION ALL SELECT 'from' AS side) s "
            f"WHERE {scope} GROUP BY 1, 2"
        )

    def write_day(self, day: str, parts=("aggregates", "transit", "labels")) -> dict:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("Expected a YYYY-MM-DD day")
        statements = {
            "aggregates": self.aggregate_sql,
            "transit": self.transit_sql,
            "labels": self.label_sql,
            "history": self.history_sql,
        }
        timings = {"day": day}
        for part in parts:
            started = time.monotonic()
            # The outer SELECT avoids a Spark analyzer failure on INSERT over a view-backed CTE.
            self.run(
                f"INSERT OVERWRITE DIRECTORY '{SHARDS[part][1]}/day={day}' USING parquet "
                f"SELECT * FROM ({statements[part](day)}) shard"
            )
            timings[part + "_seconds"] = round(time.monotonic() - started, 1)
        return timings

    def register(self) -> None:
        """(Re)register the day-partitioned shard sets as Lightning tables."""
        for table, path in SHARDS.values():
            self.register_table(table, path)

    def register_table(self, table: str, path: str, database: str = "IBM_AML") -> None:
        """A parquet directory as a Lightning filestore table; refreshed when it already exists."""
        try:
            self.run(f'CREATE LIGHTNING FILESTORE TABLE {table} FROM {database} FORMAT PARQUET OPTIONS (PATH "{path}", mergeSchema "false")')
        except DataError:
            self.run(f"REFRESH TABLE {database}.{table}")
