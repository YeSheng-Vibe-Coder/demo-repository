"""Path C: the rule-update agent proposes new risk features from a catalogue of Zetaris templates.

Each template is a fixed, label-free Zetaris computation with bounded parameters; the agent picks
templates and parameters from investigation evidence (which checklist flags separated laundering
from false alarms). A proposal is recorded with the agent's rationale; materialising it means
writing a new day-sharded feature table for every partition (hours of Zetaris time), registering it,
and refitting the rules and tiers, after which the usual gates decide.
"""

from __future__ import annotations

import json
import time

from pydantic import Field

from .contracts import StrictModel

CATALOGUE = {
    "onward_to_new_within_hours": {
        "description": "Outgoing payments to counterparties new to the account (no transaction in the prior 7 days) "
                       "within H hours of an incoming payment",
        "parameters": {"hours": [2, 6, 24]}, "evidence_flags": ["onward_within_24h", "to_new_counterparty"]},
    "counterparties_alerted": {
        "description": "Counterparties of the last 24h that the layer-1 rule also alerts on that day",
        "parameters": {}, "evidence_flags": ["counterparty_alerted"]},
    "currency_switch_downstream": {
        "description": "Payments received by this account's payees in the last 24h that leave in a different currency within H hours",
        "parameters": {"hours": [6, 24]}, "evidence_flags": ["currency_switch_on_chain"]},
    "sources_converging": {
        "description": "Distinct paying accounts in the last 48h whose own sources were new to them in the prior 7 days",
        "parameters": {}, "evidence_flags": ["convergence_from_sources"]},
    "dormancy_break": {
        "description": "Days since the previous payment before today, capped at 30",
        "parameters": {}, "evidence_flags": ["dormant_then_active"]},
}


class Proposal(StrictModel):
    rationale: str = Field(min_length=1, max_length=400)
    template: str = Field(max_length=40)
    hours: int = 0


class Proposals(StrictModel):
    analysis: str = Field(min_length=1, max_length=500)
    proposals: list[Proposal] = Field(default_factory=list, max_length=3)


PROMPT = """You are the AML rule-update agent. Propose up to 3 new risk features for the layer-1 rules and tiers,
chosen from the FEATURE CATALOGUE (template plus parameter), based on which evidence flags separated laundering from
false alarms in the investigated development cases (EVIDENCE TABLE). Prefer features whose flags had a clearly higher
laundering rate than the base rate and that the current features cannot express. Return one JSON instance."""


def propose(provider, evidence_table: dict, current_features: list[str]) -> dict:
    schema = Proposals.model_json_schema()
    schema["$defs"]["Proposal"]["properties"]["template"] = {"type": "string", "pattern": "^[a-z_]{1,40}$", "maxLength": 40}
    schema["$defs"]["Proposal"]["properties"]["hours"] = {"type": "integer"}
    packet = {"FEATURE CATALOGUE": {k: {kk: v[kk] for kk in ("description", "parameters")} for k, v in CATALOGUE.items()},
              "EVIDENCE TABLE": evidence_table, "CURRENT FEATURES": current_features}
    raw, usage = provider.generate(PROMPT, packet, schema)
    if "_invalid_response" in raw:
        return {"status": "invalid", "error": raw["_invalid_response"], "created": time.time()}
    parsed = Proposals.model_validate(raw)
    accepted = []
    for item in parsed.proposals:
        template = CATALOGUE.get(item.template)
        if template is None:
            continue
        hours = item.hours if item.hours in template["parameters"].get("hours", []) else (template["parameters"].get("hours") or [0])[0]
        accepted.append({"template": item.template, "hours": hours, "description": template["description"],
                         "rationale": item.rationale, "status": "proposed_pending_materialisation"})
    return {"status": "proposed", "analysis": parsed.analysis, "proposals": accepted, "created": time.time(),
            "evidence_table": evidence_table, "usage": usage}


def record(store, result: dict) -> str:
    return store.artifact("feature_proposal", json.loads(json.dumps(result, default=str)))
