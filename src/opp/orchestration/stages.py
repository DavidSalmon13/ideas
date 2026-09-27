"""Stage handlers: what the orchestrator runs for each state of the funnel (plan.md §9).

A handler does one stage's work for one run and reports how it ended; it never moves the
run's state itself — the orchestrator owns that. Handlers must be resumable: called again
on a stage that was interrupted, they continue it (agent runs resume from their ledgers)
instead of starting over.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from opp.agents.base import AgentResult
from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.discovery import DiscoveryAgent
from opp.agents.runtime import AgentRuntime
from opp.db.repositories import AgentRunRepository
from opp.domain.enums import AgentRunStatus, ModelTier, SignalSource, StageName
from opp.domain.run import AgentRun, ResearchRun, Stage
from opp.llm.gateway import LLMGateway
from opp.research.search import SearchProvider, TraceSink


@dataclass(frozen=True)
class StageServices:
    """Everything stages need from outside, injected so tests and cassettes can swap it."""

    sessions: sessionmaker[Session]
    gateway: LLMGateway
    search: SearchProvider
    sink: TraceSink
    search_cost_estimate_usd: float
    discovery_budget: ResearchBudget

    def runtime(self) -> AgentRuntime:
        return AgentRuntime(
            self.sessions,
            gateway=self.gateway,
            sink=self.sink,
            search=self.search,
            search_cost_estimate_usd=self.search_cost_estimate_usd,
        )


class StageResult(StrEnum):
    COMPLETED = "completed"
    HALTED_BUDGET = "halted_budget"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class StageOutcome:
    result: StageResult
    cost_usd: float
    detail: str = ""


@dataclass(frozen=True)
class StageContext:
    run: ResearchRun
    stage: Stage
    run_budget: RunBudget
    should_stop: Callable[[], bool]


class StageHandler(Protocol):
    name: StageName

    async def run(self, ctx: StageContext) -> StageOutcome: ...


class DiscoveryStage:
    """One DiscoveryAgent per signal source, in parallel, under the shared run budget."""

    name = StageName.DISCOVERY

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        agent_runs = self._agent_runs(ctx)
        sources = [discovery_signal(agent_run) for agent_run in agent_runs]
        target = DiscoveryAgent.per_source_target(ctx.run.run_config, len(sources))
        runtime = self._services.runtime()
        results = await asyncio.gather(
            *(
                runtime.run(
                    DiscoveryAgent(source, target_leads=target),
                    agent_run.id,
                    budget=self._services.discovery_budget,
                    run_budget=ctx.run_budget,
                    should_stop=ctx.should_stop,
                )
                for source, agent_run in zip(sources, agent_runs, strict=True)
            )
        )
        return outcome_of(list(results))

    def _agent_runs(self, ctx: StageContext) -> list[AgentRun]:
        """The stage's agent runs: the existing ones on resume, else one per source."""
        with self._services.sessions() as session:
            repo = AgentRunRepository(session)
            existing = repo.list_for_stage(ctx.stage.id)
            if existing:
                return sorted(existing, key=lambda ar: ar.agent_name)
            created = [
                AgentRun(
                    stage_id=ctx.stage.id,
                    agent_name=f"{DiscoveryAgent.name}:{source.value}",
                    model_tier=ModelTier.CHEAP,
                )
                for source in ctx.run.run_config.discovery_scope.signal_sources
            ]
            for agent_run in created:
                repo.add(agent_run)
            session.commit()
        return created


def discovery_signal(agent_run: AgentRun) -> SignalSource:
    return SignalSource(agent_run.agent_name.removeprefix(f"{DiscoveryAgent.name}:"))


def outcome_of(results: list[AgentResult]) -> StageOutcome:
    """How a stage of parallel agents ended: a budget stop wins (resumable), then a
    cancel; the stage failed only when every agent failed."""
    cost = sum(r.cost_usd for r in results)
    statuses = [r.status for r in results]
    if AgentRunStatus.HALTED_BUDGET in statuses:
        detail = next(r.error for r in results if r.budget_exhausted) or "budget exhausted"
        return StageOutcome(StageResult.HALTED_BUDGET, cost, detail)
    if AgentRunStatus.CANCELLED in statuses:
        return StageOutcome(StageResult.CANCELLED, cost, "cancelled by the operator")
    if results and all(s is AgentRunStatus.FAILED for s in statuses):
        errors = "; ".join(sorted({r.error or "unknown error" for r in results}))
        return StageOutcome(StageResult.FAILED, cost, f"every agent failed: {errors}")
    return StageOutcome(StageResult.COMPLETED, cost)
