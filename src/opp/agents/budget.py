"""Budget guard (plan.md §7.3, §22; intent.md §21.3).

Two layers, both checked *before* every external call:

- ``RunBudget`` — the run's hard ceiling ($20 by default), shared by every agent in the
  run, including agents running in parallel.
- ``AgentBudget`` — one agent's research budget: a dollar cap plus caps on LLM calls,
  searches and fetches, so no single agent can quietly eat the run.

A call whose estimated cost doesn't fit raises ``BudgetExceededError`` and is never made.
Actual cost is charged after the call returns (or fails), from what the gateway and
providers report.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import Field

from opp.domain.base import DomainModel


class CallKind(StrEnum):
    LLM = "llm"
    SEARCH = "search"
    FETCH = "fetch"


class BudgetExceededError(Exception):
    """The next call can't be afforded. Not a failure: the agent stops and waits."""

    def __init__(self, message: str, *, scope: str) -> None:
        super().__init__(message)
        self.scope = scope  # "run" or "agent"


class ResearchBudget(DomainModel):
    """Per-agent research budget (config, not code: plan.md §30)."""

    max_cost_usd: float = Field(default=1.0, gt=0)
    max_llm_calls: int = Field(default=50, ge=0)
    max_searches: int = Field(default=20, ge=0)
    max_fetches: int = Field(default=10, ge=0)

    def limit(self, kind: CallKind) -> int:
        return {
            CallKind.LLM: self.max_llm_calls,
            CallKind.SEARCH: self.max_searches,
            CallKind.FETCH: self.max_fetches,
        }[kind]


class RunBudget:
    """The run-wide meter. One instance is shared by all agents of a run."""

    def __init__(self, ceiling_usd: float, spent_usd: float = 0.0) -> None:
        self.ceiling_usd = ceiling_usd
        self.spent_usd = spent_usd

    @property
    def remaining_usd(self) -> float:
        return max(self.ceiling_usd - self.spent_usd, 0.0)

    def charge(self, cost_usd: float) -> None:
        self.spent_usd += cost_usd


@dataclass
class AgentBudget:
    """One agent's spend against its ``ResearchBudget`` and the shared ``RunBudget``."""

    limits: ResearchBudget
    run: RunBudget
    spent_usd: float = 0.0
    calls: dict[CallKind, int] = field(default_factory=lambda: dict.fromkeys(CallKind, 0))

    @property
    def remaining_usd(self) -> float:
        return max(self.limits.max_cost_usd - self.spent_usd, 0.0)

    def check(self, kind: CallKind, estimated_cost_usd: float) -> None:
        """Raise before a call that would break any limit."""
        if self.calls[kind] >= self.limits.limit(kind):
            raise BudgetExceededError(
                f"agent reached its {kind.value} limit ({self.limits.limit(kind)})", scope="agent"
            )
        if estimated_cost_usd > self.remaining_usd:
            raise BudgetExceededError(
                f"agent budget: next {kind.value} call (~${estimated_cost_usd:.4f}) exceeds "
                f"the ${self.remaining_usd:.4f} left",
                scope="agent",
            )
        if estimated_cost_usd > self.run.remaining_usd:
            raise BudgetExceededError(
                f"run budget: next {kind.value} call (~${estimated_cost_usd:.4f}) exceeds "
                f"the ${self.run.remaining_usd:.4f} left",
                scope="run",
            )

    def charge(self, kind: CallKind, cost_usd: float) -> None:
        self.calls[kind] += 1
        self.spent_usd += cost_usd
        self.run.charge(cost_usd)
