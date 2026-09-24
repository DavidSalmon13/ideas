"""Stage handlers: what the executor calls for each pipeline stage (plan.md §9).

A handler does one stage's work for a run and reports how it ended; the executor owns
every run/stage status change. Handlers must be safe to call again on the same stage
(resume after a crash, a top-up or a restart): the agent runtime's ledgers make that so.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.discovery import DiscoveryAgent
from opp.agents.runtime import AgentRuntime
from opp.db.repositories import AgentRunRepository
from opp.domain.enums import AgentRunStatus, ModelTier, SignalSource
from opp.domain.run import AgentRun, ResearchRun, Stage


class StageOutcome(StrEnum):
    COMPLETED = "completed"  # move on to the next stage
    WAITING = "waiting"  # stop here until the operator acts (the checkpoint)
    HALTED_BUDGET = "halted_budget"  # stop and wait for a top-up
    FAILED = "failed"  # nothing usable came out: halt with an error
    CANCELLED = "cancelled"  # the operator cancelled mid-stage


@dataclass(frozen=True)
class StageResult:
    outcome: StageOutcome
    reason: str | None = None


@dataclass
class StageContext:
    run: ResearchRun
    stage: Stage
    run_budget: RunBudget
    should_stop: Callable[[], bool]


class StageHandler(Protocol):
    async def run(self, ctx: StageContext) -> StageResult: ...


DISCOVERY_AGENT_PREFIX = "discovery:"


class DiscoveryStage:
    """One DiscoveryAgent per configured signal source, in parallel, under the shared run
    budget. Agent runs are created on first entry and reused on every later one."""

    def __init__(
        self, sessions: sessionmaker[Session], runtime: AgentRuntime, budget: ResearchBudget
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._budget = budget

    async def run(self, ctx: StageContext) -> StageResult:
        agent_runs = self._agent_runs(ctx)
        target = DiscoveryAgent.per_source_target(ctx.run.run_config, len(agent_runs))
        results = await asyncio.gather(
            *(
                self._runtime.run(
                    DiscoveryAgent(source, target_leads=target),
                    agent_run.id,
                    budget=self._budget,
                    run_budget=ctx.run_budget,
                    should_stop=ctx.should_stop,
                )
                for source, agent_run in agent_runs.items()
            )
        )
        statuses = [r.status for r in results]
        if AgentRunStatus.CANCELLED in statuses:
            return StageResult(StageOutcome.CANCELLED)
        if AgentRunStatus.HALTED_BUDGET in statuses:
            halted = [r for r in results if r.status is AgentRunStatus.HALTED_BUDGET]
            return StageResult(StageOutcome.HALTED_BUDGET, halted[0].error)
        if all(s is AgentRunStatus.FAILED for s in statuses):
            errors = "; ".join(r.error for r in results if r.error)
            return StageResult(StageOutcome.FAILED, f"every discovery agent failed: {errors}")
        return StageResult(StageOutcome.COMPLETED)

    def _agent_runs(self, ctx: StageContext) -> dict[SignalSource, AgentRun]:
        with self._sessions() as session:
            repo = AgentRunRepository(session)
            existing = {
                SignalSource(a.agent_name.removeprefix(DISCOVERY_AGENT_PREFIX)): a
                for a in repo.list_for_stage(ctx.stage.id)
                if a.agent_name.startswith(DISCOVERY_AGENT_PREFIX)
            }
            if existing:
                return existing
            created = {
                source: AgentRun(
                    stage_id=ctx.stage.id,
                    agent_name=f"{DISCOVERY_AGENT_PREFIX}{source.value}",
                    model_tier=ModelTier.CHEAP,
                )
                for source in ctx.run.run_config.discovery_scope.signal_sources
            }
            for agent_run in created.values():
                repo.add(agent_run)
            session.commit()
        return created
