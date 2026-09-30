"""Request and response shapes for the API (plan.md §11). Detail endpoints return the
domain records directly (they are already Pydantic); list, run and progress endpoints use
the compact projections below."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from opp.config.profiles import Profile
from opp.domain.enums import (
    BusinessShape,
    CheckpointAction,
    CheckpointMode,
    LeadStatus,
    OpportunityStatus,
    RunMode,
    RunStatus,
    SignalSource,
    StageName,
    Verdict,
)
from opp.domain.evaluation import Evaluation
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Kill, Lead
from opp.domain.opportunity import Opportunity
from opp.domain.report import Report
from opp.domain.run import CheckpointDecision, ResearchRun, RunTransition


class RunSummary(BaseModel):
    """A run at a glance, for the Library and run lists."""

    id: UUID
    status: RunStatus
    profile_ref: str
    mode: RunMode
    checkpoint_mode: CheckpointMode
    current_stage: StageName | None
    cost_to_date_usd: float
    budget_ceiling_usd: float
    remaining_budget_usd: float
    is_terminal: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, run: ResearchRun) -> "RunSummary":
        return cls(
            id=run.id,
            status=run.status,
            profile_ref=run.run_config.profile_ref,
            mode=run.run_config.mode,
            checkpoint_mode=run.run_config.checkpoint_mode,
            current_stage=run.current_stage,
            cost_to_date_usd=run.cost_to_date_usd,
            budget_ceiling_usd=run.budget_ceiling_usd,
            remaining_budget_usd=run.remaining_budget_usd,
            is_terminal=run.is_terminal,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


class RunDetail(RunSummary):
    """A run in full: its config, its state-transition history (why it moved/stopped), the
    checkpoint decision, and whether a report has been emitted."""

    transitions: list[RunTransition]
    checkpoint: CheckpointDecision | None
    selected_opportunity_id: UUID | None
    shortlist_size: int
    has_report: bool
    running: bool

    @classmethod
    def of_run(
        cls, run: ResearchRun, *, shortlist_size: int, has_report: bool, running: bool
    ) -> "RunDetail":
        return cls(
            **RunSummary.of(run).model_dump(),
            transitions=run.transitions,
            checkpoint=run.checkpoint,
            selected_opportunity_id=run.selected_opportunity_id,
            shortlist_size=shortlist_size,
            has_report=has_report,
            running=running,
        )


class RunEvent(BaseModel):
    """A progress snapshot pushed over SSE (plan.md §13): the live funnel view's data."""

    status: RunStatus
    current_stage: StageName | None
    cost_to_date_usd: float
    budget_ceiling_usd: float
    remaining_budget_usd: float
    leads: int
    kept_leads: int
    killed_leads: int
    opportunities: int
    shortlist: int
    has_report: bool
    awaiting_checkpoint: bool
    is_terminal: bool
    running: bool
    updated_at: datetime


class ClaimBrief(BaseModel):
    """A claim's statement with its type and confidence, for compact listings."""

    id: UUID
    statement: str
    type: str
    confidence: str

    @classmethod
    def of(cls, claim: Claim) -> "ClaimBrief":
        return cls(
            id=claim.id,
            statement=claim.statement,
            type=claim.type.value,
            confidence=claim.confidence.value,
        )


class LeadDTO(BaseModel):
    """A discovered lead with its three specificity claims and the sources behind them —
    ``source_ids`` is a derived property on the domain model, so it is projected here."""

    id: UUID
    title: str
    signal_source: SignalSource
    business_shape: BusinessShape
    status: LeadStatus
    candidate_wedge: str
    named_customer: ClaimBrief
    specific_pain: ClaimBrief
    why_now: ClaimBrief
    source_ids: list[UUID]
    discovered_at: datetime

    @classmethod
    def of(cls, lead: Lead) -> "LeadDTO":
        return cls(
            id=lead.id,
            title=lead.title,
            signal_source=lead.signal_source,
            business_shape=lead.business_shape,
            status=lead.status,
            candidate_wedge=lead.candidate_wedge,
            named_customer=ClaimBrief.of(lead.named_customer),
            specific_pain=ClaimBrief.of(lead.specific_pain),
            why_now=ClaimBrief.of(lead.why_now),
            source_ids=sorted(lead.source_ids, key=str),
            discovered_at=lead.discovered_at,
        )


class LeadsResponse(BaseModel):
    """A run's leads with their triage outcome, plus the kill records with reasons."""

    leads: list[LeadDTO]
    kills: list[Kill]


class ShallowScoreDTO(BaseModel):
    rank: int | None
    composite: float
    weights_ref: str


class OpportunitySummary(BaseModel):
    """An opportunity at a glance, for the Library and a run's shortlist."""

    id: UUID
    run_id: UUID
    title: str
    business_shape: BusinessShape
    status: OpportunityStatus
    verdict: Verdict | None
    composite: float | None
    shallow: ShallowScoreDTO | None

    @classmethod
    def of(cls, opp: Opportunity) -> "OpportunitySummary":
        evaluation = opp.evaluation
        return cls(
            id=opp.id,
            run_id=opp.run_id,
            title=opp.title,
            business_shape=opp.business_shape,
            status=opp.status,
            verdict=evaluation.verdict if evaluation else None,
            composite=(
                evaluation.composite
                if evaluation
                else opp.shallow.composite
                if opp.shallow
                else None
            ),
            shallow=(
                ShallowScoreDTO(
                    rank=opp.shallow.rank,
                    composite=opp.shallow.composite,
                    weights_ref=opp.shallow.weights_ref,
                )
                if opp.shallow
                else None
            ),
        )


class EvidenceResponse(BaseModel):
    """Every claim behind an opportunity and the sources they trace to (specs.md §8.3)."""

    claims: list[Claim]
    sources: list[Source]
    evaluation: Evaluation | None


class ReportResponse(BaseModel):
    """The latest report for a run: the 1-page brief and the full dossier (specs.md §18)."""

    run_id: UUID
    opportunity_id: UUID | None
    incomplete: bool
    halted_at_stage: StageName | None
    brief: dict[str, Any]
    dossier: dict[str, Any]
    markdown: str
    created_at: datetime

    @classmethod
    def of(cls, report: Report, markdown: str) -> "ReportResponse":
        return cls(
            run_id=report.run_id,
            opportunity_id=report.opportunity_id,
            incomplete=report.incomplete,
            halted_at_stage=report.halted_at_stage,
            brief=report.brief,
            dossier=report.dossier,
            markdown=markdown,
            created_at=report.created_at,
        )


class ProfileDTO(BaseModel):
    id: str
    name: str
    max_capital_usd: float
    max_time_to_revenue_days: float
    weights: dict[str, float]

    @classmethod
    def of(cls, profile: Profile) -> "ProfileDTO":
        return cls(
            id=profile.id,
            name=profile.name,
            max_capital_usd=profile.max_capital_usd,
            max_time_to_revenue_days=profile.max_time_to_revenue_days,
            weights={dim.value: weight for dim, weight in profile.weights.items()},
        )


class ConfigResponse(BaseModel):
    """What the New Run form needs: the profiles and the enumerations it offers."""

    profiles: list[ProfileDTO]
    default_profile: str
    default_budget_usd: float
    stage_minimums_usd: dict[str, float]
    signal_sources: list[str]
    business_shapes: list[str]
    checkpoint_modes: list[str]
    run_modes: list[str]
    dimensions: list[str]


# --- request bodies -----------------------------------------------------------------------


class ResumeRequest(BaseModel):
    budget_ceiling_usd: float | None = Field(default=None, gt=0)


class CheckpointRequest(BaseModel):
    action: CheckpointAction
    opportunity_id: UUID | None = None
    reason: str | None = None


class PromoteRequest(BaseModel):
    """Promote a shortlisted runner-up to its own deep-research run (specs.md §2.11).
    Both fields default to the source run's config."""

    budget_ceiling_usd: float | None = Field(default=None, gt=0)
    checkpoint_mode: CheckpointMode | None = None


class RefreshRequest(BaseModel):
    """Re-verify an opportunity's stale claims (specs.md §2.11). Both fields default to the
    configured refresh budget and staleness window."""

    budget_ceiling_usd: float | None = Field(default=None, gt=0)
    max_age_days: float | None = Field(default=None, gt=0)


class RefreshResponse(BaseModel):
    """The outcome of a refresh: how many claims were stale and re-verified, and the small
    run that did it (``run_id`` is null when nothing was stale)."""

    opportunity_id: UUID
    run_id: UUID | None
    stale_claims: int
    refreshed: int
    status: RunStatus | None
    cost_usd: float
    detail: str


class PatchOpportunityRequest(BaseModel):
    status: OpportunityStatus
    # An optional one-line reason; a discard/park reason feeds preference learning (§2.12).
    reason: str | None = None
