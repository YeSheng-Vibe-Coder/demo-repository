"""Canonical transaction facts independent of planner narratives or labels."""

from __future__ import annotations

from .contracts import FinishDecision
from .harness import content_hash


class EvidenceConflict(ValueError):
    pass


def transaction_ledger(state: dict) -> dict:
    unique = {}
    for ref, evidence in state["evidence"].items():
        for row in evidence.get("rows", []):
            transaction_id = str(row["transaction_id"])
            fact = {
                "transaction_id": transaction_id,
                "from_account_ref": str(row["from_bank"])
                + ":"
                + str(row["from_account"]),
                "to_account_ref": str(row["to_bank"]) + ":" + str(row["to_account"]),
                "time": row["transaction_time"],
                "amount_received": str(row["amount_received"]),
                "receiving_currency": row["receiving_currency"],
                "amount_paid": str(row["amount_paid"]),
                "payment_currency": row["payment_currency"],
            }
            if transaction_id in unique:
                existing = {
                    k: v
                    for k, v in unique[transaction_id].items()
                    if k != "evidence_refs"
                }
                if existing != fact:
                    raise EvidenceConflict(
                        "Conflicting immutable transaction facts for one transaction ID"
                    )
                unique[transaction_id]["evidence_refs"].append(ref)
            else:
                unique[transaction_id] = {**fact, "evidence_refs": [ref]}
    rows = sorted(unique.values(), key=lambda x: (x["time"], x["transaction_id"]))
    focus = state["request"]["account"]
    focus_ref = focus["bank"] + ":" + focus["account"]
    return {
        "unique_count": len(rows),
        "transactions": rows,
        "focus_account_ref": focus_ref,
        "observed_incoming_count": sum(r["to_account_ref"] == focus_ref for r in rows),
        "observed_outgoing_count": sum(
            r["from_account_ref"] == focus_ref for r in rows
        ),
        "fact_sha256": content_hash(rows),
        "scope": "Union of retained evidence only; unknown/unqueried activity is not zero; never proves complete network absence.",
    }


def verify_claims(proposal: FinishDecision, ledger: dict) -> list[dict]:
    by_id = {row["transaction_id"]: row for row in ledger["transactions"]}
    findings = []
    seen = set()
    for claim in proposal.transaction_claims:
        row = by_id.get(claim.transaction_id)
        if claim.transaction_id in seen:
            code = "duplicate_transaction"
        elif row is None:
            code = "unsupported_claim"
        elif (claim.from_account_ref, claim.to_account_ref) != (
            row["from_account_ref"],
            row["to_account_ref"],
        ):
            code = "direction_error"
        elif not set(claim.evidence_refs) <= set(row["evidence_refs"]):
            code = "unsupported_claim"
        else:
            seen.add(claim.transaction_id)
            continue
        findings.append(
            {
                "code": code,
                "severity": "major",
                "claim": "Structured transfer claim contradicts canonical facts or cites an unrelated source",
                "evidence_refs": claim.evidence_refs,
            }
        )
        seen.add(claim.transaction_id)
    return findings
