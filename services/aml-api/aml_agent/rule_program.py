"""Executable, source-bound rule programs over registered features; no generated code."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import Field, model_validator

from .contracts import StrictModel
from .feature_registry import FEATURES, POLICY
from .harness import content_hash
from .settings import ROOT

BASE_FEATURES = set(FEATURES)


def policy_registry() -> list[dict]:
    """Policy paragraphs whose quoted anchor resolves in the preserved source snapshot."""
    snapshot = (ROOT / "sources/austrac_banking_indicators.txt").read_bytes()
    digest = hashlib.sha256(snapshot).hexdigest()
    text = snapshot.decode()
    records = json.loads((ROOT / "sources/policy_source_records.json").read_text())
    return [
        {**record, "snapshot_sha256": digest}
        for record in records
        if record["source_text_sha256"] == digest and record["anchor_text"] in text
    ]


class RuleAtom(StrictModel):
    rule_id: str = Field(pattern=r"^IBM-[A-Za-z0-9_-]{1,60}$")
    feature_id: str
    operator: Literal["ge", "le"]
    threshold: float = Field(ge=0, le=1000000, allow_inf_nan=False)
    source_refs: list[str] = Field(min_length=1, max_length=3)


class RuleGroup(StrictModel):
    group_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    operator: Literal["all", "any", "weighted"]
    rule_ids: list[str] = Field(min_length=1, max_length=16)
    weights: list[float] = Field(default_factory=list, max_length=16)
    cutoff: float | None = Field(default=None, gt=0, le=80, allow_inf_nan=False)

    @model_validator(mode="after")
    def weights_match(self):
        if len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("Duplicate rule in group")
        if self.operator == "weighted":
            if len(self.weights) != len(self.rule_ids) or any(
                not math.isfinite(v) or not 0 < v <= 10 for v in self.weights
            ):
                raise ValueError(
                    "Weighted group needs one bounded positive weight per rule"
                )
            if self.cutoff is None or self.cutoff > sum(self.weights):
                raise ValueError("Weighted cutoff must be reachable")
        elif self.weights or self.cutoff is not None:
            raise ValueError("Only weighted groups may set weights/cutoff")
        return self


class RuleProgram(StrictModel):
    schema_version: Literal["rule-program/1.0"] = "rule-program/1.0"
    # Sized for a high-recall layer of up to 48 tree leaves merged into subtrees.
    rules: list[RuleAtom] = Field(min_length=1, max_length=160)
    groups: list[RuleGroup] = Field(min_length=1, max_length=32)
    group_operator: Literal["all", "any"]
    threshold_basis: Literal["research_hypothesis_not_regulatory_limit"] = (
        "research_hypothesis_not_regulatory_limit"
    )


def compile_program(program: RuleProgram) -> dict:
    """Check a program against the feature registry and verified policy paragraphs."""
    registry = {r["paragraph_id"] for r in policy_registry()}
    rule_ids = [r.rule_id for r in program.rules]
    if len(set(rule_ids)) != len(rule_ids):
        raise ValueError("Duplicate rule identifiers")
    for rule in program.rules:
        if rule.feature_id not in BASE_FEATURES:
            raise ValueError("Unimplemented feature extractor")
        if not set(rule.source_refs) <= registry:
            raise ValueError("Unknown policy paragraph")
        if not set(POLICY[rule.feature_id]) & set(rule.source_refs):
            raise ValueError(f"{rule.feature_id} needs provenance from {', '.join(POLICY[rule.feature_id])}")
    if len({g.group_id for g in program.groups}) != len(program.groups):
        raise ValueError("Duplicate group identifier")
    if any(not set(g.rule_ids) <= set(rule_ids) for g in program.groups):
        raise ValueError("Unknown rule reference")
    if {r for g in program.groups for r in g.rule_ids} != set(rule_ids):
        raise ValueError("Disabled rules must be removed from candidate, retained in parent history")
    return {
        "program": program.model_dump(mode="json"),
        "program_sha256": content_hash(program.model_dump(mode="json")),
        "compiler_version": "rule-program-compiler/2.0",
        "production_eligible": False,
    }


def program_of(body: dict) -> RuleProgram:
    """The executable program of a stored version (versions frozen before 2.0 carry
    an always-empty derived_features field, which is dropped here)."""
    program = dict(body["program"])
    if program.get("derived_features") == []:
        program.pop("derived_features")
    return RuleProgram.model_validate(program)


def load_program(store, version: str) -> dict:
    """A frozen rule version: integrity-checked on its stored content, then compiled."""
    body = store.read_artifact(version, "rule_program_version")
    if content_hash(body["program"]) != body["program_sha256"]:
        raise ValueError("Frozen rule program hash mismatch")
    compile_program(program_of(body))
    return body


def reference_program() -> RuleProgram:
    return RuleProgram(
        rules=[
            RuleAtom(
                rule_id="IBM-REF-FANIN",
                feature_id="fan_in_24h",
                operator="ge",
                threshold=3,
                source_refs=["P02"],
            ),
            RuleAtom(
                rule_id="IBM-REF-FANOUT",
                feature_id="fan_out_24h",
                operator="ge",
                threshold=3,
                source_refs=["P03"],
            ),
            RuleAtom(
                rule_id="IBM-REF-TRANSIT",
                feature_id="transit_gap_hours",
                operator="le",
                threshold=24,
                source_refs=["P03"],
            ),
        ],
        groups=[
            RuleGroup(
                group_id="reference-any",
                operator="any",
                rule_ids=["IBM-REF-FANIN", "IBM-REF-FANOUT", "IBM-REF-TRANSIT"],
            )
        ],
        group_operator="any",
    )
