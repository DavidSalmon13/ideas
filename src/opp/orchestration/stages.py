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
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from opp.agents.base import AgentResult
from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.discovery import DiscoveryAgent
from opp.agents.runtime import FINISHED_AGENT_STATUSES, AgentRuntime
from opp.agents.shallow_validation import ShallowValidationAgent
from opp.agents.triage import PriorMatch, TriageAgent
from opp.config.profiles import get_profile
from opp.db.repositories import AgentRunRepository, LeadRepository, RunRepository
from opp.domain.base import new_id
from opp.domain.enums import (
    AgentRunStatus,
    CheckpointAction,
    CheckpointMode,
    LeadStatus,
    ModelTier,
    SignalSource,
)
from opp.domain.evidence import Claim
from opp.domain.lead import Lead
from opp.domain.opportunity import Customer, Opportunity, Problem, ProposedBusiness
from opp.domain.run import AgentRun, CheckpointDecision, ResearchRun, Stage
from opp.evidence.store import EvidenceStore


class StageOutcome(StrEnum):
    COMPLETED = "completed"  # move on to the next stage
    WAITING = "waiting"  # stop here until the operator acts (the checkpoint)
    HALTED_BUDGET = "halted_budget"  # stop and wait for a top-up
    FAILED = "failed"  # nothing usable came out: halt with an error
    CANCELLED = "cancelled"  # the operator cancelled mid-stage
    NOTHING_LEFT = "nothing_left"  # no candidate survived: the run ends as a partial


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
    def estimate_usd(self, ctx: StageContext) -> float:
        """The most this stage may still spend: the budget pre-check (specs.md §21.3)
        won't start it unless the run has that much left."""
        ...

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

    def estimate_usd(self, ctx: StageContext) -> float:
        sources = len(ctx.run.run_config.discovery_scope.signal_sources)
        return unspent_caps(
            self._sessions, ctx.stage, self._budget, expected=sources, prefix=DISCOVERY_AGENT_PREFIX
        )

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
        return outcome_of(results, "discovery")

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


class ScreeningStage:
    """Triage over every undecided lead of the run (specs.md §5.2)."""

    def __init__(
        self, sessions: sessionmaker[Session], runtime: AgentRuntime, budget: ResearchBudget
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._budget = budget

    def estimate_usd(self, ctx: StageContext) -> float:
        return unspent_caps(self._sessions, ctx.stage, self._budget, prefix="triage")

    async def run(self, ctx: StageContext) -> StageResult:
        with self._sessions() as session:
            store = EvidenceStore(session)
            leads = LeadRepository(session).list_for_run(ctx.run.id)
            sources = store.sources_for(c for lead in leads for c in lead.claims())
            prior: dict[UUID, PriorMatch] = {}
            for lead in leads:
                if lead.status is not LeadStatus.NEW:
                    continue
                match = store.find_duplicate(lead)  # cross-run index only
                if match is not None and match.indexed_status is not None:
                    prior[lead.id] = PriorMatch(
                        match.indexed_status, match.lead_id, match.opportunity_id
                    )
        agent_run = single_agent_run(self._sessions, ctx.stage, "triage", ModelTier.CHEAP)
        result = await self._runtime.run(
            TriageAgent(leads, sources, prior),
            agent_run.id,
            budget=self._budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        outcome = outcome_of([result], "triage")
        if outcome.outcome is StageOutcome.COMPLETED and not self._survivors(ctx.run.id):
            return StageResult(StageOutcome.NOTHING_LEFT, "no lead survived triage")
        return outcome

    def _survivors(self, run_id: UUID) -> list[Lead]:
        with self._sessions() as session:
            return LeadRepository(session).list_for_run(run_id, LeadStatus.KEPT)


class ShallowValidationStage:
    """Rough full-framework scores for the triage top-N (specs.md §5.3). Picks the
    candidates once, on first entry; a resumed stage scores the same ones."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        runtime: AgentRuntime,
        budget: ResearchBudget,
        *,
        fetch_pages: bool = True,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._budget = budget
        self._fetch_pages = fetch_pages

    def estimate_usd(self, ctx: StageContext) -> float:
        return unspent_caps(self._sessions, ctx.stage, self._budget, prefix="shallow_validation")

    async def run(self, ctx: StageContext) -> StageResult:
        config = ctx.run.run_config
        with self._sessions() as session:
            leads = LeadRepository(session).list_for_run(ctx.run.id)
            shortlisted = [lead for lead in leads if lead.status is LeadStatus.SHORTLISTED]
            kept = sorted(
                (lead for lead in leads if lead.status is LeadStatus.KEPT),
                key=lambda lead: lead.triage.rank if lead.triage and lead.triage.rank else 10**6,
            )
            candidates = (
                shortlisted + kept[: max(config.lead_targets.shortlist_n - len(shortlisted), 0)]
            )
            sources = EvidenceStore(session).sources_for(
                c for lead in candidates for c in lead.claims()
            )
        if not candidates:
            return StageResult(StageOutcome.NOTHING_LEFT, "no triaged lead to validate")
        profile = get_profile(config.profile_ref)
        agent_run = single_agent_run(
            self._sessions, ctx.stage, "shallow_validation", ModelTier.MEDIUM
        )
        result = await self._runtime.run(
            ShallowValidationAgent(
                candidates,
                sources,
                profile,
                profile.weights_for(config),
                fetch_pages=self._fetch_pages,
            ),
            agent_run.id,
            budget=self._budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        return outcome_of([result], "shallow validation")


def outcome_of(results: list[AgentResult], what: str) -> StageResult:
    """A stage's outcome from its agents' results."""
    statuses = [r.status for r in results]
    if AgentRunStatus.CANCELLED in statuses:
        return StageResult(StageOutcome.CANCELLED)
    if AgentRunStatus.HALTED_BUDGET in statuses:
        halted = [r for r in results if r.status is AgentRunStatus.HALTED_BUDGET]
        return StageResult(StageOutcome.HALTED_BUDGET, halted[0].error)
    if all(s is AgentRunStatus.FAILED for s in statuses):
        errors = "; ".join(r.error for r in results if r.error)
        return StageResult(StageOutcome.FAILED, f"{what} failed: {errors}")
    return StageResult(StageOutcome.COMPLETED)


def single_agent_run(
    sessions: sessionmaker[Session], stage: Stage, agent_name: str, tier: ModelTier
) -> AgentRun:
    """The stage's one agent run of that name, created on first entry."""
    with sessions() as session:
        repo = AgentRunRepository(session)
        for existing in repo.list_for_stage(stage.id):
            if existing.agent_name == agent_name:
                return existing
        agent_run = AgentRun(stage_id=stage.id, agent_name=agent_name, model_tier=tier)
        repo.add(agent_run)
        session.commit()
    return agent_run


def unspent_caps(
    sessions: sessionmaker[Session],
    stage: Stage,
    budget: ResearchBudget,
    *,
    prefix: str,
    expected: int = 1,
) -> float:
    """What the stage's agents may still spend: each unfinished agent's dollar cap minus
    what it has spent; agents not created yet count in full."""
    with sessions() as session:
        agent_runs = [
            a
            for a in AgentRunRepository(session).list_for_stage(stage.id)
            if a.agent_name.startswith(prefix)
        ]
    open_runs = [a for a in agent_runs if a.status not in FINISHED_AGENT_STATUSES]
    missing = max(expected - len(agent_runs), 0)
    return round(
        missing * budget.max_cost_usd
        + sum(max(budget.max_cost_usd - a.cost_usd, 0.0) for a in open_runs),
        6,
    )


class CheckpointStage:
    """The human gate before deep research (specs.md §2.7). In mandatory mode the run
    waits here until the operator decides (``Orchestrator.decide_checkpoint``); in
    autonomous mode the top-ranked lead is selected. Selecting a lead creates the
    Opportunity that deep research will fill in."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def estimate_usd(self, ctx: StageContext) -> float:
        return 0.0

    async def run(self, ctx: StageContext) -> StageResult:
        run = ctx.run
        shortlist = ranked_shortlist(self._sessions, run.id)
        if not shortlist:
            return StageResult(StageOutcome.NOTHING_LEFT, "nothing was shortlisted")
        decision = run.checkpoint
        if decision is None:
            if run.run_config.checkpoint_mode is not CheckpointMode.AUTONOMOUS:
                return StageResult(
                    StageOutcome.WAITING,
                    "waiting for the operator to approve, re-pick or veto the shortlist",
                )
            decision = CheckpointDecision(
                action=CheckpointAction.APPROVE, lead_id=shortlist[0].id, auto=True
            )
        if decision.action is CheckpointAction.VETO:
            self._record(run.id, decision, None)
            return StageResult(
                StageOutcome.NOTHING_LEFT, f"vetoed at the checkpoint: {decision.reason}"
            )

        lead = next((le for le in shortlist if le.id == decision.lead_id), None)
        if lead is None:
            return StageResult(
                StageOutcome.FAILED, f"the selected lead {decision.lead_id} is not shortlisted"
            )
        opportunity_id = run.selected_opportunity_id
        if opportunity_id is None:
            opportunity = opportunity_from_lead(lead, run.run_config.profile_ref)
            with self._sessions() as session:
                store = EvidenceStore(session)
                store.save_opportunity(opportunity)
                store.index_opportunity(opportunity)
                session.commit()
            opportunity_id = opportunity.id
        self._record(run.id, decision, opportunity_id)
        return StageResult(StageOutcome.COMPLETED)

    def _record(
        self, run_id: UUID, decision: CheckpointDecision, opportunity_id: UUID | None
    ) -> None:
        with self._sessions() as session:
            runs = RunRepository(session)
            stored = runs.get(run_id)
            assert stored is not None
            stored.checkpoint = decision
            stored.selected_opportunity_id = opportunity_id
            runs.upsert(stored)
            session.commit()


def ranked_shortlist(sessions: sessionmaker[Session], run_id: UUID) -> list[Lead]:
    """The run's shortlisted leads, best first (specs.md §18.3)."""
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run_id, LeadStatus.SHORTLISTED)
    return sorted(
        leads,
        key=lambda le: (le.shallow.rank if le.shallow and le.shallow.rank else 10**6, str(le.id)),
    )


def opportunity_from_lead(lead: Lead, profile_ref: str) -> Opportunity:
    """The seed Opportunity for deep research: the lead's three claims (copied, so the
    lead's own evidence stays as it was) placed where the Opportunity keeps them."""

    def copy(claim: Claim) -> Claim:
        return claim.model_copy(update={"id": new_id()}, deep=True)

    wedge = (lead.triage.wedge if lead.triage else "") or lead.candidate_wedge
    return Opportunity(
        run_id=lead.run_id,
        lead_id=lead.id,
        title=lead.title,
        business_shape=lead.business_shape,
        profile_ref=profile_ref,
        problem=Problem(description=copy(lead.specific_pain)),
        customer=Customer(segment=copy(lead.named_customer)),
        why_now=copy(lead.why_now),
        risks=[f"generic pattern: {lead.triage.generic_pattern}"]
        if lead.triage and lead.triage.generic_flag
        else [],
        proposed_business=ProposedBusiness(wedge=wedge),
    )
