"""Evaluation side: chronological partitions and label checks after investigation.

Labels are read here only, by exact transaction ID and only after a case has
ended; they never enter an investigator's packet, memory or the case graph.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from .contracts import CaseRequest
from .evidence import transaction_ledger
from .settings import ROOT


def split_plan() -> dict:
    return json.loads((ROOT / "evaluations/split_plan.v2.json").read_text())


def validate_partition(case: CaseRequest) -> None:
    if case.split == "smoke":
        return
    partition = split_plan()[case.split]
    if (
        not datetime.fromisoformat(partition["start"])
        <= case.start
        < case.as_of
        <= datetime.fromisoformat(partition["end"])
    ):
        raise ValueError("Case lies outside the frozen chronological partition")


def has_model_finish(state: dict) -> bool:
    """A case usable for rule learning ended with the planner's own finish and evidence."""
    return (
        state["status"] in ("completed", "insufficient_evidence")
        and (state.get("conclusion") or {}).get("decision") == "finish"
        and transaction_ledger(state)["unique_count"] > 0
    )


def grade_samples(state: dict, labels: dict[str, int]) -> dict:
    """Exact-ID grading of retrieved and selected transactions (not account labels)."""
    ledger = transaction_ledger(state)
    retrieved = {r["transaction_id"] for r in ledger["transactions"]}
    selected = set((state.get("conclusion") or {}).get("suspect_transaction_ids", []))
    if not selected <= retrieved or not retrieved <= set(labels):
        raise ValueError("Sample/reference/label alignment mismatch")
    if any(type(labels[r]) is not int or labels[r] not in (0, 1) for r in retrieved):
        raise ValueError("Invalid transaction truth labels")
    tp = sum(labels[r] for r in selected)
    positives_retrieved = sum(labels[r] for r in retrieved)
    return {
        "selected_transaction_ids": sorted(selected),
        "retrieved_unique_transactions": len(retrieved),
        "selected_suspicious_transactions": len(selected),
        "tp": tp,
        "fp": len(selected) - tp,
        "precision": tp / len(selected) if selected else None,
        "positive_transactions_retrieved": positives_retrieved,
        "selected_recall_within_retrieved": tp / positives_retrieved if positives_retrieved else None,
        "global_transaction_recall": None,
        "global_recall_reason": "No closed full-network eligible transaction universe defined",
        "label_scope": "exact transaction IDs; not inherited account-window labels",
        "labels_exposed_to_investigator": False,
    }


class TransactionLabelReader:
    """Evaluation-only exact-ID label lookup; never registered as an agent tool."""

    def __init__(self, feature_store):
        self.store = feature_store
        self.calls = 0

    def query(self, sql: str, limit: int) -> list[list]:
        self.calls += 1
        return self.store.run(sql, limit)

    def read(self, transaction_ids: list[str]) -> dict[str, int]:
        ids = sorted(set(transaction_ids))
        if any(not re.fullmatch(r"[0-9]{1,20}", value) for value in ids) or len(ids) > 2000:
            raise ValueError("Invalid or excessive transaction IDs")
        labels = {}
        for offset in range(0, len(ids), 200):
            batch = ids[offset : offset + 200]
            rows = self.query(
                "SELECT transaction_id, is_laundering FROM IBM_AML.hi_large_transactions WHERE transaction_id IN ("
                + ",".join(batch)
                + ") ORDER BY transaction_id LIMIT 201",
                202,
            )
            for identifier, label in rows:
                identifier = str(identifier)
                if identifier not in batch or identifier in labels or str(label) not in ("0", "1"):
                    raise ValueError("Invalid exact-ID label result")
                labels[identifier] = int(label)
            if set(batch) - set(labels):
                raise ValueError("Missing transaction labels; no negative-label substitution")
        return labels
