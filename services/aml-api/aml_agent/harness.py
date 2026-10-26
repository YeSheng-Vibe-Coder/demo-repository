"""Domain execution guards inspired by Scout's structured allow/block pattern.

Project code; Scout does not ship a ready-made domain loop detector.
Guards do not choose the next investigation strategy or call another model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .contracts import Action
from .tools import case_windows

POLICY_VERSION = "aml-execution-guards/1.0"


def content_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def action_fingerprint(action: Action, state: dict) -> str:
    arguments = dict(action.arguments)
    if "window_ref" in arguments:
        arguments["window"] = case_windows(state["request"])[
            arguments.pop("window_ref")
        ]
    for key in ("account_ref", "other_account_ref"):
        if key in arguments:
            arguments[key] = state["accounts"].get(arguments[key], arguments[key])
    if "account_refs" in arguments:
        arguments["account_refs"] = [
            state["accounts"].get(ref, ref) for ref in sorted(arguments["account_refs"])
        ]
    return content_hash(
        {
            "tool": action.tool,
            "arguments": arguments,
            "as_of": state["request"]["as_of"],
            "source": "aml.transactions",
            "snapshot": "immutable-uploaded-IBM-HI-Large-v8",
            "rule_version": state["request"].get("rule_version"),
        }
    )


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    code: str
    fingerprint: str
    reason: str
    evidence_ref: str | None = None
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class ExecutionHarness:
    def __init__(self, max_stalls: int = 2):
        self.max_stalls = max_stalls

    def check(self, action: Action, state: dict) -> GuardVerdict:
        fingerprint = action_fingerprint(action, state)
        for observation in state["observations"]:
            previous = observation.get("action_fingerprint")
            if previous is None and observation.get("arguments") is not None:
                previous = action_fingerprint(
                    Action(
                        tool=observation["tool"],
                        arguments=observation["arguments"],
                        expected_observation="Previously completed action",
                    ),
                    state,
                )
            if previous == fingerprint:
                ref = observation["evidence_ref"]
                return GuardVerdict(
                    False,
                    "duplicate_completed_action",
                    fingerprint,
                    f"{action.tool} with these exact arguments already completed as {ref}; its result is in evidence_inventory and cannot change. "
                    "Only this exact tool+arguments is blocked: every other tool, account, window or direction remains allowed. "
                    "Choose a different action that answers an open question, or finish.",
                    ref,
                )
        return GuardVerdict(True, "allowed", fingerprint, "New authorized action scope")

    def blocked(self, state: dict, verdict: GuardVerdict) -> None:
        record = state.setdefault("harness", {})
        record["blocked_without_progress"] = (
            record.get("blocked_without_progress", 0) + 1
        )
        record["feedback"] = verdict.to_dict()
        if record["blocked_without_progress"] >= self.max_stalls:
            # Not a hard stop: the planner must finish with an evidence-bound judgement.
            record["force_finish"] = True

    @staticmethod
    def next_offset(state: dict, verdict: GuardVerdict, page_size: int) -> int:
        """Where the next unseen page of a duplicate's stored rows starts (the first page was shown with the result)."""
        return state.get("harness", {}).get("served_offsets", {}).get(verdict.evidence_ref, page_size)

    @classmethod
    def has_unseen_page(cls, state: dict, verdict: GuardVerdict, page_size: int) -> bool:
        """A duplicate detail query can still serve retained rows the planner never saw."""
        rows = state["evidence"][verdict.evidence_ref].get("rows", [])
        return len(rows) > page_size and cls.next_offset(state, verdict, page_size) < len(rows)

    @classmethod
    def redirect(cls, state: dict, verdict: GuardVerdict, page_size: int) -> dict:
        """Serve the next unseen page of stored rows for a duplicate action instead of a second source call.

        Only reached when has_unseen_page: other duplicates are rejected before they run.
        """
        stored = state["evidence"][verdict.evidence_ref]
        rows = stored["rows"]
        offset = cls.next_offset(state, verdict, page_size)
        state.setdefault("harness", {}).setdefault("served_offsets", {})[verdict.evidence_ref] = offset + page_size
        return {
            "duplicate_of": verdict.evidence_ref,
            "source_call": False,
            "note": "Identical action already completed; stored evidence served without a new source call. Choose a new scope, read pages, or finish.",
            "rows": rows[offset : offset + page_size],
            "stored_rows": len(rows),
            "page_offset": offset,
            "next_offset": offset + page_size if offset + page_size < len(rows) else None,
            "source_query_complete": stored.get("complete", False),
            "complete": False,
        }

    def observe(self, state: dict, result: dict, fingerprint: str) -> dict:
        record = state.setdefault("harness", {})
        signature = content_hash(
            {
                "scope": fingerprint,
                "result": {
                    key: value
                    for key, value in result.items()
                    if key
                    not in (
                        "cache_hit",
                        "proposal_id",
                        "duplicate_of",
                        "source_call",
                        "note",
                    )
                },
            }
        )
        signatures = record.setdefault("observation_signatures", [])
        progress = signature not in signatures
        if progress:
            signatures.append(signature)
            record["blocked_without_progress"] = 0
            record["no_progress_observations"] = 0
            # Stale block feedback made the planner believe whole tools were disabled.
            record.pop("feedback", None)
        else:
            record["no_progress_observations"] = (
                record.get("no_progress_observations", 0) + 1
            )
        record["last_progress"] = {
            "new_observation": progress,
            "observation_signature": signature,
            "action_fingerprint": fingerprint,
        }
        if record.get("no_progress_observations", 0) >= self.max_stalls:
            record["force_finish"] = True
        return record["last_progress"]
