"""Run configuration and the nested run -> stage -> agent state (specs.md §3.3, §19)."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from opp.domain.base import DomainModel, Entity, utcnow
from opp.domain.enums import (
    TERMINAL_RUN_STATUSES,
    AgentRunStatus,
    BusinessShape,
    CheckpointMode,
    Dimension,
    Level,
    ModelTier,
    RunMode,
    RunStatus,
    StageName,
    StageStatus,
)

DEFAULT_PROFILE = "bootstrap"
DEFAULT_BUDGET_USD = 20.0


class HardGateOverrides(DomainModel):
    """Per-run gate values. Relaxing a profile gate must be surfaced in the run and report."""

    max_capital_usd: float | None = Field(default=None, gt=0)
    max_time_to_revenue_days: float | None = Field(default=None, gt=0)


class DiscoveryScope(DomainModel):
    industry: str | None = None
    geography: str | None = None
    business_shape: BusinessShape | None = None
    target_customer: str | None = None


class LeadTargets(DomainModel):
    discovery_n: int = Field(default=30, ge=1)
    shortlist_n: int = Field(default=5, ge=1)


class Constraint(DomainModel):
    text: str = Field(min_length=1)
    hard: bool = False


class RunConfig(DomainModel):
    """Operator inputs compiled over the active profile. Empty input is valid (open-ended)."""

    profile_ref: str = DEFAULT_PROFILE
    mode: RunMode = RunMode.OPEN_ENDED
    hard_gates_override: HardGateOverrides = Field(default_factory=HardGateOverrides)
    weight_overrides: dict[Dimension, float] = Field(default_factory=dict)
    discovery_scope: DiscoveryScope = Field(default_factory=DiscoveryScope)
    revenue_target_monthly_usd: float | None = Field(default=None, gt=0)
    min_gross_margin: float | None = Field(default=None, ge=0, le=1)
    risk_tolerance: Level | None = None
    skills: list[str] = Field(default_factory=list)
    time_commitment_hours_per_week: float | None = Field(default=None, gt=0)
    team_size: int | None = Field(default=None, ge=1)
    soft_preferences: list[str] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    budget_ceiling_usd: float = Field(default=DEFAULT_BUDGET_USD, gt=0)
    checkpoint_mode: CheckpointMode = CheckpointMode.MANDATORY
    lead_targets: LeadTargets = Field(default_factory=LeadTargets)


class ResearchRun(Entity):
    status: RunStatus = RunStatus.CREATED
    run_config: RunConfig = Field(default_factory=RunConfig)
    cost_to_date_usd: float = Field(default=0.0, ge=0)
    current_stage: StageName | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def budget_ceiling_usd(self) -> float:
        return self.run_config.budget_ceiling_usd

    @property
    def remaining_budget_usd(self) -> float:
        return max(self.budget_ceiling_usd - self.cost_to_date_usd, 0.0)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


class Stage(Entity):
    run_id: UUID
    name: StageName
    status: StageStatus = StageStatus.PENDING
    attempt: int = Field(default=1, ge=1)
    kickback_count: int = Field(default=0, ge=0, le=1)
    cost_usd: float = Field(default=0.0, ge=0)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class WorkLedger(DomainModel):
    """Durable record of an agent's sub-tasks, so an interrupted agent resumes (plan.md §7)."""

    done: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    # Sub-tasks that were attempted and failed (they also count as done: not retried).
    failed: list[str] = Field(default_factory=list)

    @property
    def next_task(self) -> str | None:
        return self.pending[0] if self.pending else None


class AgentRun(Entity):
    stage_id: UUID
    agent_name: str = Field(min_length=1)
    model_tier: ModelTier
    model_id: str | None = None
    prompt_version: str | None = None
    status: AgentRunStatus = AgentRunStatus.PENDING
    attempt: int = Field(default=1, ge=1)
    work_ledger: WorkLedger = Field(default_factory=WorkLedger)
    summary: str = ""
    input_ref: str | None = None
    output_ref: str | None = None
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None
