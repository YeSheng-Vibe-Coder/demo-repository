"""Fixed SQL against the label-free view, with bounded results and strict scope."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

import httpx

from .contracts import Account, endpoint_ref
from .settings import Settings


RETRY_WAIT_SECONDS = 20
# Label-free sources an investigator may read: the transaction view and the
# Zetaris feature-store shards. Labels live only in evaluation tables.
AGENT_SOURCES = ("aml.transactions", "ibm_aml.agent_day_aggregates", "ibm_aml.agent_day_transit", "ibm_aml.agent_day_history")
CONTEXT_FEATURES = [
    "fan_in_48h", "fan_out_24h", "in_banks_24h", "out_banks_24h", "currency_count_24h",
    "transit_gap_hours", "round_trip_48h", "onward_currency_switch_48h", "activity_change_24h",
    "new_counterparty_share_24h", "history_active_days_7d", "amount_vs_history_24h",
]


class DataError(RuntimeError):
    pass


class TransientDataError(DataError):
    """Connection failure, 5xx or 429: the query never produced a result."""


# In-scope transfers: ACH / Wire between two different accounts, the only ones the feature store observes.
IN_SCOPE = "{p}payment_format IN ('ACH','Wire') AND ({p}from_bank <> {p}to_bank OR {p}from_account <> {p}to_account)"
SCOPE = "{p}transaction_time >= {start} AND {p}transaction_time < {end} AND " + IN_SCOPE

PATH_COLUMNS = [
    "transaction_id",
    "transaction_time",
    "from_bank",
    "from_account",
    "to_bank",
    "to_account",
    "amount_received",
    "receiving_currency",
    "amount_paid",
    "payment_currency",
]


def moment(value) -> datetime:
    """Parse a dataset_naive source timestamp; a trailing Z is not a timezone claim here."""
    return datetime.fromisoformat(str(value).removesuffix("Z"))


def timestamp(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo:
        raise ValueError("Expected dataset_naive timestamp")
    return "TIMESTAMP '" + dt.isoformat(sep=" ", timespec="seconds") + "'"


class Zetaris:
    def __init__(self, settings: Settings):
        self.settings = settings

    def execute(self, sql: str, limit: int) -> list[list]:
        lowered = sql.lower()
        if (
            ";" in sql
            or not sql.startswith(("SELECT ", "WITH "))
            or not any(source in lowered for source in AGENT_SOURCES)
            or "is_laundering" in lowered
            or "agent_eval" in lowered
            or "hi_large_transactions" in lowered
        ):
            raise DataError("Only fixed label-free SELECT statements are allowed")
        try:
            response = httpx.post(
                self.settings.zetaris_url + "/sql-editor/sqls/run",
                headers={
                    "Authorization": "Bearer " + self.settings.zetaris_key,
                    "X-Org-ID": self.settings.org_id,
                    "X-Request-ID": str(uuid.uuid4()),
                },
                json={
                    "queryId": str(uuid.uuid4()),
                    "sql": sql,
                    "source": "SqlEditor",
                    "limit": limit,
                },
                timeout=180,
                follow_redirects=False,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientDataError(f"Zetaris HTTP {response.status_code}")
            if response.status_code != 200:
                raise DataError(f"Zetaris HTTP {response.status_code}")
            data = response.json(parse_float=str)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as error:
            raise TransientDataError("Zetaris connection failure") from error
        except (httpx.HTTPError, ValueError) as error:
            raise DataError(
                "Zetaris transport/response failure; no empty-evidence substitution"
            ) from error
        rows = data.get("data") if isinstance(data, dict) else None
        if isinstance(data, dict) and (
            data.get("error") or data.get("errors") or data.get("success") is False
        ):
            raise DataError("Zetaris reported a query error")
        if (
            not isinstance(rows, list)
            or len(rows) > limit
            or any(not isinstance(row, list) for row in rows)
        ):
            raise DataError("Invalid Zetaris result envelope")
        return rows

    def run(self, sql: str, limit: int, reserve: Callable[[], None]) -> list[list]:
        """Reserve budget per physical request; retry once only if no result was produced.

        Client timeouts are not retried: the source query may still be running.
        """
        reserve()
        try:
            return self.execute(sql, limit)
        except TransientDataError:
            time.sleep(RETRY_WAIT_SECONDS)  # Lets a dropped tunnel reconnect first.
            reserve()
            return self.execute(sql, limit)

    @staticmethod
    def predicate(account: Account, direction: str) -> str:
        if direction not in ("incoming", "outgoing", "both"):
            raise ValueError("Invalid direction")
        sides = (
            ["to", "from"]
            if direction == "both"
            else ["to" if direction == "incoming" else "from"]
        )
        return (
            "("
            + " OR ".join(
                f"({side}_bank='{account.bank}' AND {side}_account='{account.account}')"
                for side in sides
            )
            + ")"
        )

    def transactions(
        self,
        account: Account,
        start: str,
        end: str,
        direction: str,
        reserve: Callable[[], None],
        cap: int = 200,
        other: Account | None = None,
    ) -> dict:
        if type(cap) is not int or not 1 <= cap <= 200:
            raise ValueError("cap must be 1..200")
        predicate = self.predicate(account, direction)
        if other:
            predicate += " AND " + self.predicate(other, "both")
        keys = PATH_COLUMNS + ["payment_format"]
        sql = (
            f"SELECT {', '.join(keys)} FROM aml.transactions WHERE {predicate} "
            f"AND {SCOPE.format(p='', start=timestamp(start), end=timestamp(end))} "
            f"ORDER BY transaction_time, transaction_id LIMIT {cap + 1}"
        )
        raw = self.run(sql, cap + 1, reserve)
        rows = []
        for row in raw:
            if len(row) != len(keys):
                raise DataError("Invalid transaction columns")
            record = dict(zip(keys, row))
            time_value = moment(record["transaction_time"])
            if (
                not datetime.fromisoformat(start)
                <= time_value
                < datetime.fromisoformat(end)
            ):
                raise DataError("Out-of-window transaction")
            endpoints = [
                str(record["from_bank"]) + ":" + str(record["from_account"]),
                str(record["to_bank"]) + ":" + str(record["to_account"]),
            ]
            if account.reference not in endpoints or (
                other and other.reference not in endpoints
            ):
                raise DataError("Out-of-account transaction")
            if (
                direction == "incoming"
                and endpoints[1] != account.reference
                or direction == "outgoing"
                and endpoints[0] != account.reference
            ):
                raise DataError("Direction mismatch")
            record["transaction_time"] = time_value.isoformat()
            rows.append(record)
        return {
            "rows": rows[:cap],
            "complete": len(raw) <= cap,
            "status": "complete" if len(raw) <= cap else "partial",
            "window": {"start": start, "end": end},
            "scope": "ACH/Wire; self transfers excluded",
            "ordering": "transaction_time,transaction_id",
            "cap": cap,
        }

    def profile(
        self,
        accounts: list[Account],
        start: str,
        end: str,
        reserve: Callable[[], None],
        top: int = 5,
    ) -> dict:
        """Scale, frequency and counterparty distribution for 1-5 accounts in one scan.

        Totals are exact source aggregates per account, direction and currency;
        only the counterparty list is limited to the top entries by count.
        """
        if not 1 <= len(accounts) <= 5 or not 1 <= top <= 5:
            raise ValueError("Profile covers one to five accounts")

        def pick(incoming: str, outgoing: str) -> str:
            return f"CASE WHEN side='incoming' THEN {incoming} ELSE {outgoing} END"

        # A literal direction table lets Zetaris scan the view once; Spark cannot
        # resolve a UNION that references the same view twice.
        incoming = " OR ".join(self.predicate(a, "incoming") for a in accounts)
        outgoing = " OR ".join(self.predicate(a, "outgoing") for a in accounts)
        counterparty = pick(
            "CONCAT(from_bank,':',from_account)", "CONCAT(to_bank,':',to_account)"
        )
        partition = "PARTITION BY side, bank, account, currency"
        sql = (
            "WITH pairs AS (SELECT side, "
            f"{pick('to_bank', 'from_bank')} AS bank, {pick('to_account', 'from_account')} AS account, "
            f"{pick('receiving_currency', 'payment_currency')} AS currency, {counterparty} AS counterparty, "
            f"COUNT(*) AS n, SUM({pick('amount_received', 'amount_paid')}) AS amount, "
            "MIN(transaction_time) AS first_time, MAX(transaction_time) AS last_time "
            "FROM aml.transactions CROSS JOIN (SELECT 'incoming' AS side UNION ALL SELECT 'outgoing' AS side) sides "
            f"WHERE ((side='incoming' AND ({incoming})) OR (side='outgoing' AND ({outgoing}))) "
            f"AND {SCOPE.format(p='', start=timestamp(start), end=timestamp(end))} "
            "GROUP BY 1, 2, 3, 4, 5), ranked AS (SELECT *, "
            f"ROW_NUMBER() OVER ({partition} ORDER BY n DESC, amount DESC, counterparty) AS rank, "
            f"COUNT(*) OVER ({partition}) AS counterparties, SUM(n) OVER ({partition}) AS total_n, "
            f"SUM(amount) OVER ({partition}) AS total_amount, MIN(first_time) OVER ({partition}) AS window_first, "
            f"MAX(last_time) OVER ({partition}) AS window_last FROM pairs) "
            "SELECT side, bank, account, currency, total_n, counterparties, total_amount, window_first, "
            f"window_last, rank, counterparty, n, amount FROM ranked WHERE rank <= {top} "
            "ORDER BY bank, account, side, currency, rank LIMIT 201"
        )
        rows = self.run(sql, 201, reserve)
        requested = {a.reference for a in accounts}
        result = {ref: {"incoming": [], "outgoing": []} for ref in sorted(requested)}
        window = (datetime.fromisoformat(start), datetime.fromisoformat(end))
        for row in rows[:200]:
            if len(row) != 13 or row[0] not in ("incoming", "outgoing"):
                raise DataError("Invalid profile columns")
            reference = f"{row[1]}:{row[2]}"
            if reference not in requested:
                raise DataError("Out-of-scope profile account")
            if not window[0] <= moment(row[7]) <= moment(row[8]) < window[1]:
                raise DataError("Out-of-window profile aggregate")
            groups = result[reference][row[0]]
            if not groups or groups[-1]["currency"] != row[3]:
                groups.append(
                    {
                        "currency": row[3],
                        "count": row[4],
                        "counterparties": row[5],
                        "amount": row[6],
                        "first": moment(row[7]).isoformat(),
                        "last": moment(row[8]).isoformat(),
                        "top_counterparties": [],
                    }
                )
            groups[-1]["top_counterparties"].append(
                {"account_ref": row[10], "count": row[11], "amount": row[12]}
            )
        return {
            "accounts": result,
            "complete": len(rows) <= 200,
            "window": {"start": start, "end": end},
            "scope": "ACH/Wire; self transfers excluded; currencies kept separate; empty direction means none in this complete window only; top_counterparties ranked by count, not risk",
        }

    def flow(
        self,
        account: Account,
        mode: str,
        start: str,
        end: str,
        gap_hours: int,
        depth: int,
        reserve: Callable[[], None],
        seeds: int = 10,
        beam: int = 3,
        max_nodes: int = 60,
    ) -> dict:
        """Iterative multi-hop fund tracing computed by Zetaris, one SQL per hop.

        downstream follows payments out of the account; upstream follows the funds
        that paid into it backwards; through starts from incoming payments and
        follows what the account sent on. Each hop is strictly later (earlier
        upstream) within gap_hours and inside [start, end). Hops may change currency:
        published IBM laundering chains convert currency at almost every hop, and no
        FX rates exist here, so amount continuity is only enforced within one
        currency. The `beam` fastest continuations are kept per transaction. Routes
        are candidate fund flows, never proof of the same funds.
        """
        if mode not in ("downstream", "upstream", "through") or not 1 <= depth <= 5:
            raise ValueError("Invalid flow scope")
        if gap_hours not in (6, 24, 48):
            raise ValueError("Invalid flow gap")
        bounds = (datetime.fromisoformat(start), datetime.fromisoformat(end))
        nodes: dict[str, dict] = {}
        pruned = False

        def accept(raw: list, parent_of: dict) -> list[dict]:
            nonlocal pruned
            added = []
            for record in raw:
                if len(record) != len(PATH_COLUMNS) + 1:
                    raise DataError("Invalid flow columns")
                row = dict(zip(["parent"] + PATH_COLUMNS, record))
                row["transaction_id"] = str(row["transaction_id"])
                at = moment(row["transaction_time"])
                if not bounds[0] <= at < bounds[1]:
                    raise DataError("Out-of-window flow transaction")
                row["transaction_time"] = at.isoformat()
                for key in PATH_COLUMNS[2:]:
                    row[key] = str(row[key])
                parent = parent_of.get(str(row.pop("parent")))
                if parent is not None:
                    self.check_link(nodes[parent], row, mode, gap_hours)
                existing = nodes.get(row["transaction_id"])
                if existing is not None:
                    # Several traced payments feed one continuation (gathering): keep every link.
                    if parent is not None and parent != existing["parent"]:
                        existing.setdefault("also_from", []).append(parent)
                    continue
                if len(nodes) >= max_nodes:
                    pruned = True
                    continue
                nodes[row["transaction_id"]] = {**row, "parent": parent}
                added.append(nodes[row["transaction_id"]])
            return added

        # Seeds: the account's own payments in the scoped direction.
        seed_side = "outgoing" if mode == "downstream" else "incoming"
        seed_sql = (
            "SELECT 0, " + ", ".join(PATH_COLUMNS) + " FROM aml.transactions "
            f"WHERE {self.predicate(account, seed_side)} "
            f"AND {SCOPE.format(p='', start=timestamp(start), end=timestamp(end))} "
            f"ORDER BY amount_paid DESC, transaction_time, transaction_id LIMIT {seeds + 1}"
        )
        raw = self.run(seed_sql, seeds + 1, reserve)
        pruned |= len(raw) > seeds
        frontier = accept(raw[:seeds], {})
        hops_run = 1
        for hop in range(2, depth + 1):
            if not frontier:
                break
            forward = mode != "upstream"
            parent_of = {str(index): node["transaction_id"] for index, node in enumerate(frontier)}
            values = ", ".join(
                "({pid}, '{bank}', '{account}', {at}, '{currency}', CAST('{amount}' AS DECIMAL(38,8)))".format(
                    pid=index,
                    bank=node["to_bank" if forward else "from_bank"],
                    account=node["to_account" if forward else "from_account"],
                    at=timestamp(node["transaction_time"]),
                    currency=node["receiving_currency" if forward else "payment_currency"],
                    amount=node["amount_received" if forward else "amount_paid"],
                )
                for index, node in enumerate(frontier)
            )
            if forward:
                join = (
                    "t.from_bank = f.bank AND t.from_account = f.account AND t.transaction_time > f.at "
                    f"AND t.transaction_time <= f.at + INTERVAL {gap_hours} HOURS "
                    # Lower bound only: gathering combines inflows, so onward amounts may exceed one input.
                    "AND (t.payment_currency <> f.currency OR t.amount_paid >= f.amount * 0.05)"
                )
                # Fastest onward movement first.
                closeness = "t.transaction_time"
            else:
                join = (
                    "t.to_bank = f.bank AND t.to_account = f.account AND t.transaction_time < f.at "
                    f"AND t.transaction_time >= f.at - INTERVAL {gap_hours} HOURS "
                    "AND (t.receiving_currency <> f.currency OR t.amount_received >= f.amount * 0.05)"
                )
                closeness = "t.transaction_time DESC"
            sql = (
                f"WITH frontier AS (SELECT * FROM VALUES {values} "
                "AS f(pid, bank, account, at, currency, amount)), hop AS (SELECT f.pid, "
                + ", ".join(f"t.{column}" for column in PATH_COLUMNS)
                + f", ROW_NUMBER() OVER (PARTITION BY f.pid ORDER BY {closeness}, t.transaction_time, t.transaction_id) AS rank "
                f"FROM frontier f JOIN aml.transactions t ON {join} "
                f"WHERE {SCOPE.format(p='t.', start=timestamp(start), end=timestamp(end))}) "
                "SELECT pid, " + ", ".join(PATH_COLUMNS)
                + f" FROM hop WHERE rank <= {beam} ORDER BY pid, rank LIMIT 201"
            )
            raw = self.run(sql, 201, reserve)
            pruned |= len(raw) > 200
            frontier = accept(raw[:200], parent_of)
            hops_run = hop
            # Keep the next frontier bounded; beam already limits per-parent fan-out.
            frontier = frontier[: max(1, max_nodes // depth)]
        return self.flow_summary(account, mode, nodes, pruned, hops_run, depth) | {
            "window": {"start": start, "end": end},
            "flow_scope": {"mode": mode, "depth": depth, "max_gap_hours": gap_hours, "beam": beam, "seeds": seeds},
        }

    @staticmethod
    def check_link(parent: dict, child: dict, mode: str, gap_hours: int) -> None:
        earlier, later = (child, parent) if mode == "upstream" else (parent, child)
        gap = (
            moment(later["transaction_time"]) - moment(earlier["transaction_time"])
        ).total_seconds() / 3600
        if (earlier["to_bank"], earlier["to_account"]) != (
            later["from_bank"],
            later["from_account"],
        ) or not 0 < gap <= gap_hours:
            raise DataError("Flow hop violates the template linkage")

    @staticmethod
    def flow_summary(account, mode, nodes, pruned, hops_run, depth) -> dict:
        children = {}
        for identifier, node in nodes.items():
            for parent in [node["parent"], *node.get("also_from", [])]:
                children.setdefault(parent, []).append(identifier)
        chains = []

        def walk(identifier, chain):
            chain = chain + [identifier]
            if not children.get(identifier):
                chains.append(chain)
            for child in children.get(identifier, []):
                walk(child, chain)

        for root in children.get(None, []):
            walk(root, [])
        origin = account.reference
        routes = []
        for chain in chains:
            ordered = [nodes[i] for i in (reversed(chain) if mode == "upstream" else chain)]
            accounts = [endpoint_ref(ordered[0], "from")] + [
                endpoint_ref(row, "to") for row in ordered
            ]
            first, last = Decimal(ordered[0]["amount_received"]), Decimal(ordered[-1]["amount_paid"])
            currencies = [row["payment_currency"] for row in ordered]
            same_currency = currencies[0] == currencies[-1]
            routes.append(
                {
                    "transaction_ids": [row["transaction_id"] for row in ordered],
                    "accounts": accounts,
                    "gap_hours": [
                        round((moment(b["transaction_time"]) - moment(a["transaction_time"])).total_seconds() / 3600, 2)
                        for a, b in zip(ordered, ordered[1:])
                    ],
                    "currencies": currencies,
                    "currency_changes": sum(a != b for a, b in zip(currencies, currencies[1:])),
                    # No FX rates: a ratio is only meaningful when both ends share a currency.
                    "amount_retained": str((last / first).quantize(Decimal("0.0001")))
                    if first and same_currency
                    else None,
                    "returns_to_origin": accounts.count(origin) > 1,
                }
            )
        routes.sort(key=lambda r: (-len(r["transaction_ids"]), r["transaction_ids"]))
        receivers: dict[str, set] = {}
        for node in nodes.values():
            receivers.setdefault(endpoint_ref(node, "to"), set()).add(
                endpoint_ref(node, "from")
            )
        convergence = sorted(ref for ref, senders in receivers.items() if len(senders) >= 2 and ref != origin)
        return {
            "routes": routes[:12],
            "routes_total": len(routes),
            "hops_reached": max((len(r["transaction_ids"]) for r in routes), default=0),
            "hops_requested": depth,
            "convergence_accounts": convergence[:10],
            "returns_to_origin": any(r["returns_to_origin"] for r in routes),
            "rows": [
                {k: v for k, v in node.items() if k not in ("parent", "also_from")}
                for node in nodes.values()
            ],
            "complete": not pruned,
            "pruned": pruned,
            "status": "complete" if not pruned else "partial",
            "scope": "Strictly ordered hops within the gap; currency may change per hop (no FX rates, so amount continuity applies only within one currency); beam keeps the fastest continuations. Routes are candidate fund flows, not proof of the same funds; partial means seeds or hops were capped.",
        }

    def transaction_context(self, rows: list[dict], reserve: Callable[[], None]) -> dict[str, dict]:
        """Backward-looking structure for each payment, computed in Zetaris.

        For a payment at time t only transactions before t are read, so the flags
        never use information after the payment (or the case cutoff): how recently
        the payer had received funds, from how many sources, whether any arrived in
        another currency, and whether the receiver had just paid the payer.
        """
        rows = rows[:200]
        if not rows:
            return {}
        values = ", ".join(
            "('{id}', '{fb}', '{fa}', '{tb}', '{ta}', {at}, '{cur}')".format(
                id=row["transaction_id"], fb=row["from_bank"], fa=row["from_account"],
                tb=row["to_bank"], ta=row["to_account"], at=timestamp(row["transaction_time"]),
                cur=row["payment_currency"],
            )
            for row in rows
        )
        times = [datetime.fromisoformat(row["transaction_time"]) for row in rows]
        earliest = (min(times) - timedelta(hours=48)).isoformat()
        latest = max(times).isoformat()
        gap = "(UNIX_TIMESTAMP(f.at) - UNIX_TIMESTAMP(a.transaction_time)) / 3600.0"
        sql = (
            f"WITH f AS (SELECT * FROM VALUES {values} AS v(id, payer_bank, payer_account, receiver_bank, receiver_account, at, currency)) "
            f"SELECT f.id, MIN({gap}), COUNT(DISTINCT CONCAT(a.from_bank, ':', a.from_account)), "
            "MAX(CASE WHEN a.receiving_currency <> f.currency THEN 1 ELSE 0 END), "
            "MAX(CASE WHEN a.from_bank = f.receiver_bank AND a.from_account = f.receiver_account THEN 1 ELSE 0 END) "
            "FROM f JOIN aml.transactions a ON a.to_bank = f.payer_bank AND a.to_account = f.payer_account "
            "AND a.transaction_time < f.at AND a.transaction_time >= f.at - INTERVAL 48 HOURS "
            f"WHERE {SCOPE.format(p='a.', start=timestamp(earliest), end=timestamp(latest))} GROUP BY f.id"
        )
        found = {str(r[0]): r for r in self.run(sql, 201, reserve)}
        context = {}
        for row in rows:
            hit = found.get(str(row["transaction_id"]))
            context[str(row["transaction_id"])] = {
                "payer_prior_in_gap_h": round(float(hit[1]), 2) if hit and hit[1] is not None else None,
                "payer_prior_sources_48h": int(hit[2]) if hit else 0,
                "payer_prior_other_currency": bool(hit and int(hit[3])),
                "reverse_edge_prior_48h": bool(hit and int(hit[4])),
            }
        return context

    def account_features(
        self, accounts_days: list[tuple[str, str]], as_of: str, reserve: Callable[[], None]
    ) -> dict[str, dict]:
        """Feature-store context for (account, day) pairs whose whole day precedes the cutoff."""
        cutoff = datetime.fromisoformat(as_of)
        eligible = sorted(
            {(ref, day) for ref, day in accounts_days if datetime.fromisoformat(day) + timedelta(hours=24) <= cutoff}
        )[:60]
        if not eligible:
            return {}
        keys = " OR ".join(
            f"(g.bank = '{ref.split(':')[0]}' AND g.account = '{ref.split(':')[1]}' AND g.day = DATE '{day}')"
            for ref, day in eligible
        )
        transit = ", ".join(f"tr.{name}" for name in ("transit_gap_hours", "round_trip_48h", "onward_currency_switch_48h"))
        # The day list on every table lets Zetaris read only those day shards instead of joining all of them.
        days = ", ".join(f"DATE '{day}'" for day in sorted({day for _, day in eligible}))
        sql = (
            "SELECT g.bank, g.account, CAST(g.day AS STRING), g.fan_in_48h, g.fan_out_24h, g.in_banks_24h, "
            f"g.out_banks_24h, g.currency_count_24h, {transit}, g.activity_change_24h, "
            "h.new_counterparty_share_24h, h.history_active_days_7d, h.amount_vs_history_24h "
            f"FROM (SELECT * FROM IBM_AML.agent_day_aggregates WHERE day IN ({days})) g "
            f"LEFT JOIN (SELECT * FROM IBM_AML.agent_day_transit WHERE day IN ({days})) tr "
            "ON tr.bank = g.bank AND tr.account = g.account AND tr.day = g.day "
            f"LEFT JOIN (SELECT * FROM IBM_AML.agent_day_history WHERE day IN ({days})) h "
            "ON h.bank = g.bank AND h.account = g.account AND h.day = g.day "
            f"WHERE {keys}"
        )
        context: dict[str, dict] = {}
        for row in self.run(sql, 201, reserve):
            values = dict(zip(CONTEXT_FEATURES, row[3:]))
            context.setdefault(f"{row[0]}:{row[1]}", {})[str(row[2])[:10]] = {
                key: (None if value is None else round(float(value), 4)) for key, value in values.items()
            }
        return context

    def risk_features(self, references: list[str], day: str, reserve: Callable[[], None]) -> dict[str, dict]:
        """All registered features of these accounts on one case day, from the feature store."""
        from .feature_registry import FEATURES
        from .feature_store import features_relation

        keys = " OR ".join(f"(f.bank = '{r.split(':')[0]}' AND f.account = '{r.split(':')[1]}')" for r in references)
        sql = (f"SELECT f.bank, f.account, {', '.join('f.' + n for n in FEATURES)} FROM "
               f"({features_relation(f"g.day = DATE '{day}'")}) f WHERE {keys}")
        return {f"{row[0]}:{row[1]}": {n: None if v is None else float(v) for n, v in zip(FEATURES, row[2:])}
                for row in self.run(sql, 10, reserve)}

    def baseline(
        self, account: Account, day: str, history_days: int, reserve: Callable[[], None]
    ) -> dict:
        """Current day against a chosen history length, from the Zetaris feature store.

        Inactive history days are zeros (no feature row exists), so means divide by
        the full history length rather than by active days only.
        """
        current = datetime.fromisoformat(day)
        start = (current - timedelta(days=history_days)).date().isoformat()
        metrics = ["in_count_24h", "out_count_24h", "fan_in_24h", "fan_out_24h", "currency_count_24h", "pass_through_ratio_24h"]
        history = ", ".join(
            f"SUM(CASE WHEN g.day < DATE '{day[:10]}' THEN g.{m} ELSE 0 END), "
            f"SUM(CASE WHEN g.day < DATE '{day[:10]}' THEN g.{m} * g.{m} ELSE 0 END), "
            f"MAX(CASE WHEN g.day < DATE '{day[:10]}' THEN g.{m} END), "
            f"MAX(CASE WHEN g.day = DATE '{day[:10]}' THEN g.{m} END)"
            for m in metrics
        )
        sql = (
            f"SELECT COUNT(CASE WHEN g.day < DATE '{day[:10]}' THEN 1 END), MIN(CAST(g.day AS STRING)), {history} "
            "FROM IBM_AML.agent_day_aggregates g "
            f"WHERE g.bank = '{account.bank}' AND g.account = '{account.account}' "
            f"AND g.day >= DATE '{start}' AND g.day <= DATE '{day[:10]}'"
        )
        row = self.run(sql, 2, reserve)[0]
        active_days, first_day = int(row[0] or 0), row[1]
        comparison = {}
        for index, metric in enumerate(metrics):
            total, squares, peak, today = (row[2 + 4 * index + k] for k in range(4))
            mean = float(total or 0) / history_days
            deviation = max(float(squares or 0) / history_days - mean * mean, 0.0) ** 0.5
            today_value = float(today or 0)
            comparison[metric] = {
                "current": today_value,
                "history_mean": round(mean, 4),
                "history_std": round(deviation, 4),
                "history_max": None if peak is None else float(peak),
                "ratio_to_mean": round(today_value / mean, 2) if mean else None,
                "z_score": round((today_value - mean) / deviation, 2) if deviation else None,
            }
        return {
            "day": day[:10],
            "history_days": history_days,
            "history_active_days": active_days,
            "history_window": {"start": start, "end_exclusive": day[:10]},
            "feature_store_first_day_seen": first_day,
            "baseline_reliable": active_days >= min(5, history_days // 2),
            "comparison": comparison,
            "scope": "Zetaris feature store; inactive days count as zero; days outside the materialised store are unknown, not zero",
        }

    def new_counterparties(
        self, account: Account, day: str, history_days: int, reserve: Callable[[], None]
    ) -> dict:
        """Share of the day's counterparties never seen in the chosen history window."""
        current = datetime.fromisoformat(day[:10])
        start = (current - timedelta(days=history_days)).isoformat()
        end = (current + timedelta(hours=24)).isoformat()

        def pick(incoming: str, outgoing: str) -> str:
            return f"CASE WHEN s.side = 'incoming' THEN {incoming} ELSE {outgoing} END"

        counterparty = pick("CONCAT(t.from_bank, ':', t.from_account)", "CONCAT(t.to_bank, ':', t.to_account)")
        sql = (
            "SELECT side, COUNT(*), SUM(CASE WHEN first_seen >= "
            f"{timestamp(current.isoformat())} THEN 1 ELSE 0 END) FROM (SELECT s.side, {counterparty} AS counterparty, "
            "MIN(t.transaction_time) AS first_seen, MAX(t.transaction_time) AS last_seen "
            "FROM aml.transactions t CROSS JOIN (SELECT 'incoming' AS side UNION ALL SELECT 'outgoing' AS side) s "
            f"WHERE ((s.side = 'incoming' AND t.to_bank = '{account.bank}' AND t.to_account = '{account.account}') "
            f"OR (s.side = 'outgoing' AND t.from_bank = '{account.bank}' AND t.from_account = '{account.account}')) "
            f"AND {SCOPE.format(p='t.', start=timestamp(start), end=timestamp(end))} GROUP BY 1, 2) c "
            f"WHERE last_seen >= {timestamp(current.isoformat())} GROUP BY side ORDER BY side"
        )
        result = {"incoming": {"counterparties_today": 0, "new_vs_history": 0}, "outgoing": {"counterparties_today": 0, "new_vs_history": 0}}
        for side, total, new in self.run(sql, 3, reserve):
            result[side] = {"counterparties_today": int(total), "new_vs_history": int(new or 0),
                            "new_share": round(int(new or 0) / int(total), 3) if int(total) else None}
        return result

