"""The cost ledger (plan.md §19, specs.md §21.4): a run's spend rolled up per stage, agent
and call kind from its traces. Traces are written the moment a call ends, so the ledger
is live mid-run and also counts calls whose sub-task was lost in a crash."""

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from opp.db.repositories import TraceRepository
from opp.domain.run import ResearchRun

UNATTRIBUTED = "unattributed"


@dataclass(frozen=True)
class CostLedger:
    run_id: UUID
    ceiling_usd: float
    spent_usd: float
    by_stage: dict[str, float] = field(default_factory=dict)
    by_agent: dict[str, float] = field(default_factory=dict)
    by_kind: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    failed_calls: int = 0

    @property
    def remaining_usd(self) -> float:
        return max(self.ceiling_usd - self.spent_usd, 0.0)

    @property
    def used_fraction(self) -> float:
        return self.spent_usd / self.ceiling_usd if self.ceiling_usd else 0.0


def cost_ledger(session: Session, run: ResearchRun) -> CostLedger:
    by_stage: dict[str, float] = defaultdict(float)
    by_agent: dict[str, float] = defaultdict(float)
    by_kind: dict[str, float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)
    failed = 0
    traced = 0.0
    for trace in TraceRepository(session).list_for_run(run.id):
        traced += trace.cost_usd
        by_stage[trace.stage.value if trace.stage else UNATTRIBUTED] += trace.cost_usd
        by_agent[trace.agent or UNATTRIBUTED] += trace.cost_usd
        by_kind[trace.kind.value] += trace.cost_usd
        calls[trace.kind.value] += 1
        failed += not trace.ok
    return CostLedger(
        run_id=run.id,
        ceiling_usd=run.budget_ceiling_usd,
        spent_usd=max(run.cost_to_date_usd, traced),
        by_stage=dict(by_stage),
        by_agent=dict(by_agent),
        by_kind=dict(by_kind),
        calls=dict(calls),
        failed_calls=failed,
    )
