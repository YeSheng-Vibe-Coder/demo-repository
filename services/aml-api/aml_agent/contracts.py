"""Validated domain contracts; arbitrary SQL and executable rules are excluded."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Account(StrictModel):
    bank: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    account: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")

    @property
    def reference(self) -> str:
        return f"{self.bank}:{self.account}"


INVESTIGATION_TOOLS = (
    "assess_risk",
    "summarize_account",
    "query_account_transactions",
    "compare_account_baseline",
    "query_between_accounts",
    "check_structure",
)


class Assignment(StrictModel):
    """One specialist investigator's share of a case, chosen by the coordinator."""

    role: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=1, max_length=240)
    accounts: list[str] = Field(min_length=1, max_length=5)
    tools: list[str] = Field(min_length=1, max_length=len(INVESTIGATION_TOOLS))
    max_actions: int = Field(ge=1, le=6)

    @model_validator(mode="after")
    def known_tools(self) -> "Assignment":
        self.tools = [tool for tool in dict.fromkeys(self.tools) if tool in INVESTIGATION_TOOLS]
        if not self.tools:
            raise ValueError(f"Assign at least one of: {', '.join(INVESTIGATION_TOOLS)}")
        return self


class TeamPlan(StrictModel):
    rationale: str = Field(min_length=1, max_length=300)
    assignments: list[Assignment] = Field(min_length=1, max_length=4)


class CaseRequest(StrictModel):
    account: Account
    start: datetime
    as_of: datetime
    split: Literal["development", "validation", "test", "smoke"] = "smoke"
    rule_version: str | None = None
    test_release: bool = False
    alert_id: str | None = Field(default=None, max_length=100)
    # Set only on specialist sub-runs created by the coordinator.
    assignment: Assignment | None = None
    parent_run_id: str | None = Field(default=None, max_length=40)
    investigation_question: str = Field(
        default="Identify evidence-supported suspicious transaction IDs and connected upstream/downstream patterns within scope; distinguish observed facts from unknowns.",
        min_length=10,
        max_length=600,
    )

    @model_validator(mode="after")
    def valid_window(self) -> "CaseRequest":
        if self.start.tzinfo or self.as_of.tzinfo:
            raise ValueError(
                "IBM timestamps use dataset_naive time, not an inferred timezone"
            )
        if not timedelta(0) < self.as_of - self.start <= timedelta(days=7):
            raise ValueError("Window must be positive and at most seven days")
        if self.split == "test" and not self.test_release:
            raise ValueError(
                "Test cases are locked until an explicit final evaluation release"
            )
        return self


# One tool per investigation question; paging and retries are host concerns.
ToolName = Literal[
    "get_alert_context",
    "assess_risk",
    "summarize_account",
    "query_account_transactions",
    "compare_account_baseline",
    "query_between_accounts",
    "check_structure",
    "propose_rule_change",
]


class AccountWindow(StrictModel):
    account_ref: str
    window_ref: Literal["current", "history", "lookback"] = "current"


class ProfileArguments(StrictModel):
    account_refs: list[str] = Field(min_length=1, max_length=5)
    window_ref: Literal["current", "history", "lookback"] = "current"

    @model_validator(mode="after")
    def unique_accounts(self) -> "ProfileArguments":
        # Repeated references are normalised, not rejected: they ask for the same scope.
        self.account_refs = sorted(set(self.account_refs))
        return self


class RiskArguments(StrictModel):
    account_refs: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_accounts(self) -> "RiskArguments":
        self.account_refs = sorted(set(self.account_refs))
        return self


class TransactionArguments(AccountWindow):
    # The retained-row cap is fixed by the host; a planner-chosen smaller cap only
    # forced partial results and duplicate re-queries.
    direction: Literal["incoming", "outgoing", "both"] = "both"


class BaselineArguments(StrictModel):
    account_ref: str
    history_days: Literal[7, 14, 30] = 14


class BetweenArguments(AccountWindow):
    other_account_ref: str


class StructureArguments(AccountWindow):
    direction: Literal["downstream", "upstream", "through"]
    max_gap_hours: Literal[6, 24, 48] = 24
    depth: Literal[2, 3, 4, 5] = 3


class ProposalArguments(StrictModel):
    base_version: str
    evidence_refs: list[str] = Field(min_length=1, max_length=16)
    change_type: Literal["threshold", "feature", "combination", "weight", "new_rule"]
    hypothesis: str = Field(min_length=1, max_length=400)
    changes: dict[str, Any]

    @model_validator(mode="after")
    def proposal_payload(self) -> "ProposalArguments":
        keys = {
            "threshold": {"parameters"},
            "feature": {"feature_ids"},
            "combination": {"operator", "rule_ids"},
            "weight": {"weights"},
            "new_rule": {"draft"},
        }[self.change_type]
        if set(self.changes) != keys:
            raise ValueError("Change payload does not match change_type")
        if self.change_type == "threshold":
            parameters = self.changes["parameters"]
            if (
                not isinstance(parameters, dict)
                or not parameters
                or not set(parameters)
                <= {"window_hours", "min_counterparties", "gap_hours"}
            ):
                raise ValueError("Unknown threshold parameter")
            if any(
                type(value) is not int or not 1 <= value <= 168
                for value in parameters.values()
            ):
                raise ValueError("Invalid threshold value")
        if self.change_type == "combination" and self.changes["operator"] not in (
            "AND",
            "OR",
        ):
            raise ValueError("Unknown combination operator")
        if self.change_type == "weight":
            weights = self.changes["weights"]
            if (
                not isinstance(weights, dict)
                or not weights
                or any(
                    type(value) not in (int, float) or not 0 <= value <= 10
                    for value in weights.values()
                )
            ):
                raise ValueError("Invalid rule weights")
        if self.change_type == "feature" and not isinstance(
            self.changes["feature_ids"], list
        ):
            raise ValueError("Expected feature IDs")
        if self.change_type == "new_rule" and not isinstance(
            self.changes["draft"], dict
        ):
            raise ValueError("Expected rule draft; executable code is not accepted")
        return self


TOOL_ARGUMENT_SCHEMAS = {
    "get_alert_context": StrictModel,
    "assess_risk": RiskArguments,
    "summarize_account": ProfileArguments,
    "query_account_transactions": TransactionArguments,
    "compare_account_baseline": BaselineArguments,
    "query_between_accounts": BetweenArguments,
    "check_structure": StructureArguments,
    "propose_rule_change": ProposalArguments,
}


# Every model-written field is bounded so the provider grammar has a finite worst
# case well below the per-request output reservation (see workflow.output_bound).
ShortFinding = Annotated[str, Field(min_length=1, max_length=160)]
EVIDENCE_REF = Annotated[str, Field(max_length=40)]


QuestionId = Annotated[int, Field(ge=1, le=4)]


class Action(StrictModel):
    tool: ToolName
    arguments: dict[str, Any]
    expected_observation: str = Field(min_length=1, max_length=240)
    # Which plan question this action answers; None only for host prefetch.
    question_id: QuestionId | None = None

    @model_validator(mode="after")
    def tool_arguments(self) -> "Action":
        self.arguments = (
            TOOL_ARGUMENT_SCHEMAS[self.tool].model_validate(self.arguments).model_dump()
        )
        return self


class QuestionUpdate(StrictModel):
    question_id: QuestionId
    status: Literal["open", "answered", "unanswerable"]
    evidence_refs: list[EVIDENCE_REF] = Field(default_factory=list, max_length=2)
    note: str = Field(default="", max_length=120)


class DecisionBase(StrictModel):
    schema_version: Literal["planner/1.0"]
    based_on_state_version: int = Field(ge=0)
    plan_version: int = Field(ge=1)
    brief_reason: str = Field(min_length=1, max_length=240)
    evidence_refs: list[EVIDENCE_REF] = Field(max_length=10)
    # The planner's own questions; the host does not prescribe their content or number.
    initial_plan: list[ShortFinding] | None = Field(
        default=None, min_length=1, max_length=4
    )
    question_updates: list[QuestionUpdate] = Field(default_factory=list, max_length=3)


class ActionDecision(DecisionBase):
    decision: Literal["action"]
    next_action: Action


REPLAN_REASONS = (
    "hypothesis_refuted",
    "new_branch",
    "direction_exhausted",
    "budget_low",
    "other",
)


class ReplanDecision(DecisionBase):
    decision: Literal["replan"]
    # Recorded for analysis only; the host does not gate replans on a fixed taxonomy.
    reason: Literal[REPLAN_REASONS]
    previous_plan_version: int = Field(ge=1)
    plan_patch: list[ShortFinding] = Field(min_length=1, max_length=4)
    # A replan must also act; plan-only replans spent budget without new evidence.
    next_action: Action


class TransferClaim(StrictModel):
    transaction_id: str = Field(max_length=20)
    from_account_ref: str = Field(max_length=130)
    to_account_ref: str = Field(max_length=130)
    evidence_refs: list[EVIDENCE_REF] = Field(min_length=1, max_length=3)


class CandidateDecision(StrictModel):
    transaction_id: str = Field(max_length=20)
    decision: Literal["confirm", "exclude", "unknown"]
    reason: str = Field(min_length=1, max_length=100)
    evidence_refs: list[EVIDENCE_REF] = Field(default_factory=list, max_length=1)


class AccountVerdict(StrictModel):
    """Aggregator only: a verdict on one account of the reconstructed chain."""
    account_ref: str = Field(max_length=60)
    verdict: Literal["suspicious", "cleared", "unknown"]
    reason: str = Field(min_length=1, max_length=100)
    evidence_refs: list[EVIDENCE_REF] = Field(default_factory=list, max_length=1)


class FinishDecision(DecisionBase):
    decision: Literal["finish"]
    conclusion: Literal["suspicious", "not_supported", "insufficient_evidence"]
    supported_findings: list[ShortFinding] = Field(default_factory=list, max_length=3)
    counterevidence: list[ShortFinding] = Field(default_factory=list, max_length=2)
    unresolved_questions: list[ShortFinding] = Field(default_factory=list, max_length=3)
    stop_reason: str = Field(min_length=1, max_length=200)
    transaction_claims: list[TransferClaim] = Field(default_factory=list, max_length=3)
    suspect_transaction_ids: list[str] = Field(default_factory=list, max_length=20)
    # Aggregator only: the reconstructed alert chain, as ordered short steps.
    chain: list[ShortFinding] = Field(default_factory=list, max_length=6)
    # One judgement per host-computed candidate; confirmed IDs must be suspects.
    candidate_decisions: list[CandidateDecision] = Field(
        default_factory=list, max_length=12
    )
    # Aggregator only: how strongly the cited evidence supports the conclusion, and one verdict
    # per account of the chain (the alerted account and the counterparties the team reached).
    suspicion: Literal["low", "medium", "high"] = "low"
    account_verdicts: list[AccountVerdict] = Field(default_factory=list, max_length=12)


Decision = Annotated[
    ActionDecision | ReplanDecision | FinishDecision, Field(discriminator="decision")
]
DECISIONS = TypeAdapter(Decision)


class StepRequest(StrictModel):
    expected_state_version: int = Field(ge=0)
    action_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    memory: str = Field(default="", max_length=3200)
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    fence: int = Field(ge=1)
