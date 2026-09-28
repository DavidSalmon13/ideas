"""The cost meter: a run's spend rolled up from its traces (plan.md §19 "cost ledger";
specs.md §21.4). Traces are written the moment each call ends, so the meter is live and
includes calls whose sub-task was later lost in a crash."""

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from opp.db.repositories import RunRepository, TraceRepository
from opp.domain.enums import StageName, TraceKind


@dataclass(frozen=True)
class CostMeter:
    run_id: UUID
    ceiling_usd: float
    spent_usd: float
    calls: int
    failed_calls: int
    by_stage: dict[StageName, float] = field(default_factory=dict)
    by_agent: dict[str, float] = field(default_factory=dict)
    by_kind: dict[TraceKind, float] = field(default_factory=dict)

    @property
    def remaining_usd(self) -> float:
        return max(self.ceiling_usd - self.spent_usd, 0.0)

    @property
    def used_fraction(self) -> float:
        return self.spent_usd / self.ceiling_usd if self.ceiling_usd else 1.0


def cost_meter(session: Session, run_id: UUID) -> CostMeter:
    run = RunRepository(session).get(run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    traces = TraceRepository(session).list_for_run(run_id)
    by_stage: dict[StageName, float] = defaultdict(float)
    by_agent: dict[str, float] = defaultdict(float)
    by_kind: dict[TraceKind, float] = defaultdict(float)
    for trace in traces:
        if trace.stage is not None:
            by_stage[trace.stage] += trace.cost_usd
        by_agent[trace.agent or "(none)"] += trace.cost_usd
        by_kind[trace.kind] += trace.cost_usd
    spent = max(run.cost_to_date_usd, sum(t.cost_usd for t in traces))
    return CostMeter(
        run_id=run_id,
        ceiling_usd=run.budget_ceiling_usd,
        spent_usd=spent,
        calls=len(traces),
        failed_calls=sum(not t.ok for t in traces),
        by_stage=dict(by_stage),
        by_agent=dict(by_agent),
        by_kind=dict(by_kind),
    )
