"""Run-level budget policy: per-stage allocations, pre-flight and per-stage pre-checks,
and the ceiling guard (specs.md §2.3, §21; plan.md §9, §22).

The per-agent research budgets and the shared run meter (``opp.agents.budget``) stop
individual calls; this module decides whether a *stage* may start at all. A stage starts
only when the money left covers its minimum; otherwise the run halts ``HALTED_BUDGET``
before spending anything on it, and a top-up resumes it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from opp.domain.enums import StageName


@dataclass(frozen=True)
class StageAllocation:
    """``target_usd`` is the planned spend (specs.md §21.2); ``minimum_usd`` is the least
    the stage needs to do meaningful work, which is what the pre-check requires."""

    target_usd: float
    minimum_usd: float


# specs.md §21.2 targets (midpoints); minimums are roughly one sub-task per agent.
DEFAULT_ALLOCATIONS: dict[StageName, StageAllocation] = {
    StageName.DISCOVERY: StageAllocation(target_usd=3.5, minimum_usd=0.25),
    StageName.SCREENING: StageAllocation(target_usd=0.75, minimum_usd=0.05),
    StageName.SHALLOW_VALIDATION: StageAllocation(target_usd=3.0, minimum_usd=0.4),
    StageName.CHECKPOINT: StageAllocation(target_usd=0.0, minimum_usd=0.0),
    StageName.DEEP_RESEARCH: StageAllocation(target_usd=6.0, minimum_usd=3.0),
    StageName.SYNTHESIS: StageAllocation(target_usd=1.0, minimum_usd=0.3),
    StageName.STRATEGY: StageAllocation(target_usd=1.0, minimum_usd=0.3),
    StageName.RED_TEAM: StageAllocation(target_usd=1.25, minimum_usd=0.3),
    StageName.EVALUATION: StageAllocation(target_usd=1.25, minimum_usd=0.3),
}
RESERVE_USD = 2.0
# Dollar comparisons tolerate float noise (0.25 + 0.05 + 0.4 != 0.7).
_EPSILON = 1e-9


class BudgetPolicyError(ValueError):
    """A run's ceiling is refused: over the maximum without confirmation, or too small
    for the stages it would run."""


@dataclass(frozen=True)
class BudgetPolicy:
    allocations: Mapping[StageName, StageAllocation] = field(
        default_factory=lambda: dict(DEFAULT_ALLOCATIONS)
    )
    # Ceilings above this need explicit operator confirmation (specs.md §2.3, §21.1).
    max_ceiling_usd: float = 20.0

    @classmethod
    def unconstrained(cls) -> "BudgetPolicy":
        """No stage minimums: only the agents' own budget guards apply. For tests."""
        return cls(
            allocations={s: StageAllocation(0.0, 0.0) for s in StageName},
            max_ceiling_usd=float("inf"),
        )

    def minimum(self, stage: StageName) -> float:
        allocation = self.allocations.get(stage)
        return allocation.minimum_usd if allocation else 0.0

    def target(self, stage: StageName) -> float:
        allocation = self.allocations.get(stage)
        return allocation.target_usd if allocation else 0.0

    def check_ceiling(self, ceiling_usd: float, *, confirmed: bool) -> None:
        if ceiling_usd > self.max_ceiling_usd and not confirmed:
            raise BudgetPolicyError(
                f"a ${ceiling_usd:.2f} ceiling is above the ${self.max_ceiling_usd:.2f} "
                "maximum; confirm it explicitly to go ahead"
            )

    def preflight(self, ceiling_usd: float, stages: Iterable[StageName]) -> None:
        """Reject a run whose minimum projected cost already exceeds its ceiling."""
        needed = sum(self.minimum(stage) for stage in stages)
        if needed > ceiling_usd + _EPSILON:
            raise BudgetPolicyError(
                f"a ${ceiling_usd:.2f} ceiling can't cover the ${needed:.2f} the run's stages "
                "need at minimum"
            )

    def precheck(self, stage: StageName, remaining_usd: float) -> str | None:
        """Why ``stage`` can't start with ``remaining_usd`` left, or None if it can."""
        needed = self.minimum(stage)
        if remaining_usd + _EPSILON < needed:
            return (
                f"budget pre-check: {stage.value} needs at least ${needed:.2f}, "
                f"${remaining_usd:.2f} is left"
            )
        return None
