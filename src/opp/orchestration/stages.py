"""Stage handlers: what the orchestrator runs for each state of the funnel (plan.md §9).

A handler does one stage's work for one run and reports how it ended; it never moves the
run's state itself — the orchestrator owns that. Handlers must be resumable: called again
on a stage that was interrupted, they continue it (agent runs resume from their ledgers)
instead of starting over.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from opp.agents.base import AgentResult
from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.discovery import DiscoveryAgent
from opp.agents.judge import JudgeAgent
from opp.agents.redteam import RedTeamAgent
from opp.agents.runtime import AgentRuntime
from opp.agents.shallow_validation import ShallowValidationAgent, lead_task
from opp.agents.specialists import AXES, SpecialistAgent
from opp.agents.strategy import StrategyAgent
from opp.agents.synthesis import SynthesisAgent
from opp.agents.triage import TriageAgent
from opp.config.settings import DEFAULT_STAGE_MINIMUMS_USD
from opp.db.repositories import (
    AgentRunRepository,
    ClaimRepository,
    LeadRepository,
    OpportunityRepository,
    SourceRepository,
)
from opp.domain.enums import (
    AgentRunStatus,
    DedupStatus,
    LeadStatus,
    ModelTier,
    SignalSource,
    StageName,
)
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.opportunity import Opportunity
from opp.domain.run import AgentRun, ResearchRun, Stage
from opp.evidence.store import EvidenceStore
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
    triage_budget: ResearchBudget = field(
        default_factory=lambda: ResearchBudget(
            max_cost_usd=0.75, max_llm_calls=10, max_searches=0, max_fetches=0
        )
    )
    shallow_budget: ResearchBudget = field(
        default_factory=lambda: ResearchBudget(
            max_cost_usd=3.0, max_llm_calls=8, max_searches=6, max_fetches=0
        )
    )
    # Per specialist (four run in parallel; the run budget still caps the total).
    specialist_budget: ResearchBudget = field(
        default_factory=lambda: ResearchBudget(
            max_cost_usd=1.5, max_llm_calls=6, max_searches=4, max_fetches=0
        )
    )
    synthesis_budget: ResearchBudget = field(
        default_factory=lambda: ResearchBudget(
            max_cost_usd=1.0, max_llm_calls=3, max_searches=0, max_fetches=0
        )
    )
    # Premium tier, one call plus its re-asks; no search (it reasons over the research).
    strategy_budget: ResearchBudget = field(
        default_factory=lambda: ResearchBudget(
            max_cost_usd=1.5, max_llm_calls=3, max_searches=0, max_fetches=0
        )
    )
    # Premium tier; red-team + judge share specs.md §21.2's $2-3 (one bounded call each).
    redteam_budget: ResearchBudget = field(
        default_factory=lambda: ResearchBudget(
            max_cost_usd=1.5, max_llm_calls=3, max_searches=0, max_fetches=0
        )
    )
    judge_budget: ResearchBudget = field(
        default_factory=lambda: ResearchBudget(
            max_cost_usd=1.5, max_llm_calls=3, max_searches=0, max_fetches=0
        )
    )
    # What each stage must have left in the run budget before it may start (plan.md §22).
    stage_minimums_usd: Mapping[StageName, float] = field(
        default_factory=lambda: dict(DEFAULT_STAGE_MINIMUMS_USD)
    )

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
    # Nothing survived the stage: the run can't go on and ends partial, honestly.
    NO_CANDIDATES = "no_candidates"
    # The stage did its work but found a blocking gap: send the opportunity back one stage
    # for one more targeted pass (the orchestrator enforces the ≤1/stage cap).
    KICKBACK = "kickback"


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


class TriageStage:
    """Dedup (deterministic, against the run and the cross-run index), then the
    TriageAgent's gates and cheap scores, then index every decided lead so a later run
    never resurfaces it (specs.md §4.5-§4.6)."""

    name = StageName.SCREENING

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        self._dedup(ctx)
        leads, sources = _leads_and_sources(self._services.sessions, ctx.run.id)
        [agent_run] = _agent_runs(self._services.sessions, ctx.stage, [TriageAgent.name])
        result = await self._services.runtime().run(
            TriageAgent(leads, sources),
            agent_run.id,
            budget=self._services.triage_budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        kept = self._index(ctx)
        outcome = outcome_of([result])
        if outcome.result is StageResult.COMPLETED and kept == 0:
            return StageOutcome(
                StageResult.NO_CANDIDATES, outcome.cost_usd, "no lead survived triage"
            )
        return outcome

    def _dedup(self, ctx: StageContext) -> None:
        stage = ctx.stage.name.value
        with self._services.sessions() as session:
            store = EvidenceStore(session)
            leads = LeadRepository(session).list_for_run(ctx.run.id)
            by_id = {lead.id: lead for lead in leads}
            for lead in sorted(leads, key=lambda lead: (lead.discovered_at, str(lead.id))):
                if lead.status is not LeadStatus.NEW:
                    continue
                others = [
                    other
                    for other in leads
                    if other.id != lead.id
                    and other.status not in (LeadStatus.KILLED, LeadStatus.DUPLICATE)
                ]
                match = store.find_duplicate(lead, others)
                if match is None:
                    continue
                other = by_id.get(match.lead_id) if match.lead_id else None
                if match.indexed_status is not None or other is None:
                    store.mark_duplicate(
                        lead, of=match.lead_id or match.opportunity_id, stage=stage
                    )
                elif other.status is LeadStatus.KEPT:
                    store.mark_duplicate(lead, of=other.id, stage=stage)
                else:
                    store.resolve_duplicate(lead, other, stage=stage)
            session.commit()

    def _index(self, ctx: StageContext) -> int:
        with self._services.sessions() as session:
            store = EvidenceStore(session)
            kept = 0
            for lead in LeadRepository(session).list_for_run(ctx.run.id):
                if lead.status is LeadStatus.KEPT:
                    store.index_lead(lead, DedupStatus.ACTIVE)
                    kept += 1
                elif lead.status is LeadStatus.KILLED:
                    store.index_lead(lead, DedupStatus.KILLED)
            session.commit()
        return kept


class ShallowValidationStage:
    """Scores the top ``shortlist_n`` triage survivors on the full framework, then ranks
    them by composite: rank 1 is the candidate for deep research, the rest are the
    promotable shortlist (specs.md §4.3, §5.3)."""

    name = StageName.SHALLOW_VALIDATION

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        leads, sources = _leads_and_sources(self._services.sessions, ctx.run.id)
        [agent_run] = _agent_runs(self._services.sessions, ctx.stage, [ShallowValidationAgent.name])
        candidates = self._candidates(ctx, leads, agent_run)
        result = await self._services.runtime().run(
            ShallowValidationAgent(candidates, sources),
            agent_run.id,
            budget=self._services.shallow_budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        scored = self._rank(ctx)
        outcome = outcome_of([result])
        if outcome.result is StageResult.COMPLETED and scored == 0:
            return StageOutcome(
                StageResult.NO_CANDIDATES, outcome.cost_usd, "no candidate could be scored"
            )
        return outcome

    @staticmethod
    def _candidates(ctx: StageContext, leads: dict[UUID, Lead], agent_run: AgentRun) -> list[Lead]:
        """On resume, the candidates the agent run already planned; else the best kept."""
        planned = [*agent_run.work_ledger.done, *agent_run.work_ledger.pending]
        if planned:
            wanted = {lead_task(lead_id): lead_id for lead_id in leads}
            return [leads[wanted[task]] for task in planned if task in wanted]
        kept = [lead for lead in leads.values() if lead.status is LeadStatus.KEPT]
        kept.sort(
            key=lambda lead: (
                -(lead.triage.rank_score if lead.triage else 0.0),
                lead.discovered_at,
                str(lead.id),
            )
        )
        return kept[: ctx.run.run_config.lead_targets.shortlist_n]

    def _rank(self, ctx: StageContext) -> int:
        with self._services.sessions() as session:
            repo = OpportunityRepository(session)
            scored = [o for o in repo.list(run_id=ctx.run.id) if o.shallow is not None]
            scored.sort(key=lambda o: (-o.shallow.composite, o.created_at, str(o.id)))  # type: ignore[union-attr]
            for rank, opportunity in enumerate(scored, start=1):
                assert opportunity.shallow is not None
                opportunity.shallow.rank = rank
                repo.upsert(opportunity)
            session.commit()
        return len(scored)


class DeepResearchStage:
    """The four specialists research the one selected opportunity in parallel, under the
    shared run budget (specs.md §5.4). Each files its axis claims loose under the
    opportunity, so the parallel runs never overwrite one another."""

    name = StageName.DEEP_RESEARCH

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        opportunity = _selected_opportunity(self._services.sessions, ctx.run)
        if opportunity is None:
            return StageOutcome(StageResult.NO_CANDIDATES, 0.0, "no opportunity was selected")
        names = [axis.agent_name for axis in AXES]
        agent_runs = {
            ar.agent_name: ar
            for ar in _agent_runs(self._services.sessions, ctx.stage, names, ModelTier.MEDIUM)
        }
        runtime = self._services.runtime()
        results = await asyncio.gather(
            *(
                runtime.run(
                    SpecialistAgent(axis, opportunity),
                    agent_runs[axis.agent_name].id,
                    budget=self._services.specialist_budget,
                    run_budget=ctx.run_budget,
                    should_stop=ctx.should_stop,
                )
                for axis in AXES
            )
        )
        return outcome_of(list(results))


class SynthesisStage:
    """Merges the specialists' claims into one picture and surfaces their contradictions
    (specs.md §5.5). Reads the opportunity's deep-research claims and reconciles them."""

    name = StageName.SYNTHESIS

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        opportunity = _selected_opportunity(self._services.sessions, ctx.run)
        if opportunity is None:
            return StageOutcome(StageResult.NO_CANDIDATES, 0.0, "no opportunity was selected")
        claims, sources = _deep_research_claims(self._services.sessions, opportunity.id)
        [agent_run] = _agent_runs(
            self._services.sessions, ctx.stage, [SynthesisAgent.name], ModelTier.MEDIUM
        )
        result = await self._services.runtime().run(
            SynthesisAgent(opportunity, claims, sources),
            agent_run.id,
            budget=self._services.synthesis_budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        outcome = outcome_of([result])
        if outcome.result is StageResult.COMPLETED:
            gap = self._blocking_gap(opportunity.id)
            if gap:
                return StageOutcome(StageResult.KICKBACK, outcome.cost_usd, gap)
        return outcome

    def _blocking_gap(self, opportunity_id: UUID) -> str:
        with self._services.sessions() as session:
            opportunity = OpportunityRepository(session).get(opportunity_id)
        if opportunity is None or opportunity.synthesis is None:
            return ""
        return opportunity.synthesis.blocking_gap


class StrategyStage:
    """Turns the synthesized research into a concrete business plan for the one selected
    opportunity: MVP, business model, scenario economics, small-team feasibility and the
    validation backlog (specs.md §5.6). Premium tier, one bounded call."""

    name = StageName.STRATEGY

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        opportunity = _selected_opportunity(self._services.sessions, ctx.run)
        if opportunity is None:
            return StageOutcome(StageResult.NO_CANDIDATES, 0.0, "no opportunity was selected")
        [agent_run] = _agent_runs(
            self._services.sessions, ctx.stage, [StrategyAgent.name], ModelTier.PREMIUM
        )
        result = await self._services.runtime().run(
            StrategyAgent(opportunity),
            agent_run.id,
            budget=self._services.strategy_budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        return outcome_of([result])


class RedTeamStage:
    """Attacks the selected opportunity and hunts the fatal flaw before the Judge scores it
    (specs.md §5.7). Premium tier, one bounded call over the whole opportunity."""

    name = StageName.RED_TEAM

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        opportunity = _selected_opportunity(self._services.sessions, ctx.run)
        if opportunity is None:
            return StageOutcome(StageResult.NO_CANDIDATES, 0.0, "no opportunity was selected")
        claims, sources = _opportunity_evidence(self._services.sessions, opportunity)
        [agent_run] = _agent_runs(
            self._services.sessions, ctx.stage, [RedTeamAgent.name], ModelTier.PREMIUM
        )
        result = await self._services.runtime().run(
            RedTeamAgent(opportunity, claims, sources),
            agent_run.id,
            budget=self._services.redteam_budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        return outcome_of([result])


class EvaluationStage:
    """The Judge scores the opportunity against the framework after the attack and issues the
    categorical verdict, with the non-compensatory overrides applied (specs.md §5.8, §17)."""

    name = StageName.EVALUATION

    def __init__(self, services: StageServices) -> None:
        self._services = services

    async def run(self, ctx: StageContext) -> StageOutcome:
        opportunity = _selected_opportunity(self._services.sessions, ctx.run)
        if opportunity is None:
            return StageOutcome(StageResult.NO_CANDIDATES, 0.0, "no opportunity was selected")
        claims, sources = _opportunity_evidence(self._services.sessions, opportunity)
        [agent_run] = _agent_runs(
            self._services.sessions, ctx.stage, [JudgeAgent.name], ModelTier.PREMIUM
        )
        result = await self._services.runtime().run(
            JudgeAgent(opportunity, claims, sources),
            agent_run.id,
            budget=self._services.judge_budget,
            run_budget=ctx.run_budget,
            should_stop=ctx.should_stop,
        )
        return outcome_of([result])


def _opportunity_evidence(
    sessions: sessionmaker[Session], opportunity: Opportunity
) -> tuple[list[Claim], dict[UUID, Source]]:
    """Every claim in the opportunity document and the sources it cites."""
    claims = list(opportunity.claims())
    with sessions() as session:
        sources = SourceRepository(session).get_many(
            {sid for claim in claims for sid in claim.source_ids}
        )
    return claims, sources


def _selected_opportunity(sessions: sessionmaker[Session], run: ResearchRun) -> Opportunity | None:
    if run.selected_opportunity_id is None:
        return None
    with sessions() as session:
        return OpportunityRepository(session).get(run.selected_opportunity_id)


def _deep_research_claims(
    sessions: sessionmaker[Session], opportunity_id: UUID
) -> tuple[list[Claim], dict[UUID, Source]]:
    """The opportunity's loose deep-research claims (best-effort stable order) and the
    sources they cite."""
    with sessions() as session:
        claims = [
            claim
            for claim in ClaimRepository(session).list_for_opportunity(opportunity_id)
            if claim.stage == StageName.DEEP_RESEARCH.value
        ]
        claims.sort(key=lambda c: (c.agent or "", c.observed_at, str(c.id)))
        sources = SourceRepository(session).get_many(
            {sid for claim in claims for sid in claim.source_ids}
        )
    return claims, sources


def _leads_and_sources(
    sessions: sessionmaker[Session], run_id: UUID
) -> tuple[dict[UUID, Lead], dict[UUID, Source]]:
    with sessions() as session:
        leads = {lead.id: lead for lead in LeadRepository(session).list_for_run(run_id)}
        sources = SourceRepository(session).get_many(
            {sid for lead in leads.values() for sid in lead.source_ids}
        )
    return leads, sources


def _agent_runs(
    sessions: sessionmaker[Session],
    stage: Stage,
    names: list[str],
    model_tier: ModelTier = ModelTier.CHEAP,
) -> list[AgentRun]:
    """The stage's agent runs by name, created on first entry (reused on resume)."""
    with sessions() as session:
        repo = AgentRunRepository(session)
        existing = {ar.agent_name: ar for ar in repo.list_for_stage(stage.id)}
        runs = []
        for name in names:
            if name not in existing:
                existing[name] = repo.add(
                    AgentRun(stage_id=stage.id, agent_name=name, model_tier=model_tier)
                )
            runs.append(existing[name])
        session.commit()
    return runs


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
