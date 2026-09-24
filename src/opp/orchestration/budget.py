"""The run's budget meter (specs.md §21): spend per stage against its target allocation.

Every call is metered as a Trace attributed to its stage (specs.md §21.4), so the meter
is read straight from the traces. Targets are the §21.2 allocation for a $20 run, scaled
to the run's ceiling; they're guidance for the operator, not caps. What stops a stage
from starting is the pre-check: its handler's estimate of what it may still spend
(``StageHandler.estimate_usd``) against what's left of the run's ceiling.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opp.db import models
from opp.domain.enums import StageName
from opp.domain.run import ResearchRun

# specs.md §21.2, mid-points for a $20 run; the rest ($2.25) is the reserve.
STAGE_TARGETS_USD: dict[StageName, float] = {
    StageName.DISCOVERY: 3.5,
    StageName.SCREENING: 0.75,
    StageName.SHALLOW_VALIDATION: 3.0,
    StageName.CHECKPOINT: 0.0,
    StageName.DEEP_RESEARCH: 6.0,
    StageName.SYNTHESIS: 1.0,
    StageName.STRATEGY: 1.0,
    StageName.RED_TEAM: 1.25,
    StageName.EVALUATION: 1.25,
}
REFERENCE_CEILING_USD = 20.0


@dataclass(frozen=True)
class StageSpend:
    stage: StageName
    spent_usd: float
    target_usd: float

    @property
    def over_target(self) -> bool:
        return self.spent_usd > self.target_usd > 0


@dataclass(frozen=True)
class BudgetMeter:
    ceiling_usd: float
    spent_usd: float
    stages: list[StageSpend]

    @property
    def remaining_usd(self) -> float:
        return max(self.ceiling_usd - self.spent_usd, 0.0)

    def lines(self) -> list[str]:
        out = [
            f"Budget: ${self.spent_usd:.4f} spent of ${self.ceiling_usd:.2f} "
            f"(${self.remaining_usd:.4f} left)"
        ]
        for s in self.stages:
            if s.spent_usd or s.target_usd:
                flag = "  OVER TARGET" if s.over_target else ""
                out.append(
                    f"  {s.stage.value:<20} ${s.spent_usd:.4f} of ~${s.target_usd:.2f}{flag}"
                )
        return out

    def as_dict(self) -> dict[str, object]:
        return {
            "ceiling_usd": self.ceiling_usd,
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
            "stages": {
                s.stage.value: {"spent_usd": round(s.spent_usd, 6), "target_usd": s.target_usd}
                for s in self.stages
            },
        }


def stage_target_usd(stage: StageName, ceiling_usd: float) -> float:
    return round(STAGE_TARGETS_USD[stage] * ceiling_usd / REFERENCE_CEILING_USD, 2)


def read_meter(session: Session, run: ResearchRun) -> BudgetMeter:
    spent_by_stage = _spent_by_stage(session, run.id)
    traced = sum(spent_by_stage.values())
    return BudgetMeter(
        ceiling_usd=run.budget_ceiling_usd,
        spent_usd=max(run.cost_to_date_usd, traced),
        stages=[
            StageSpend(
                stage=stage,
                spent_usd=spent_by_stage.get(stage.value, 0.0),
                target_usd=stage_target_usd(stage, run.budget_ceiling_usd),
            )
            for stage in StageName
        ],
    )


def _spent_by_stage(session: Session, run_id: UUID) -> dict[str | None, float]:
    rows = session.execute(
        select(models.TraceRow.stage, func.sum(models.TraceRow.cost_usd))
        .where(models.TraceRow.run_id == run_id)
        .group_by(models.TraceRow.stage)
    ).all()
    return {stage: float(total or 0.0) for stage, total in rows}
