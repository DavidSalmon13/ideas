"""Run a golden through the funnel offline and score it (plan.md §21).

Drives the deterministic orchestrator over the funnel fakes on a throwaway SQLite database,
then reads the persisted records back into a :class:`GoldenResult` — the hallucination
count, source-tier mix, triage kills, fatal-flaw detection, economics/small-team coverage,
report rubric and cost. No network, no keys."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker
from tests.agents.fakes import SnippetSearch

from evals.goldens import Golden
from evals.rubric import rubric_score
from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import (
    ClaimRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    ReportRepository,
    RunRepository,
    SourceRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import ClaimType, FailedGate, LeadStatus, RunStatus, Verdict
from opp.domain.evidence import Claim
from opp.domain.lead import Lead
from opp.domain.opportunity import Opportunity
from opp.llm.gateway import LLMGateway, ModelRoute, ModelRouting
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices

ROUTING = ModelRouting(
    cheap=ModelRoute(primary="claude-haiku-4-5"),
    medium=ModelRoute(primary="claude-sonnet-5"),
    premium=ModelRoute(primary="claude-sonnet-5"),
    premium_opus=ModelRoute(primary="claude-opus-4-8"),
)

# Triage kills that mean "this lead was too generic" (specs.md §16), as opposed to a duplicate.
_ANTI_GENERIC_GATES = frozenset(
    {
        FailedGate.NAMED_CUSTOMER,
        FailedGate.SPECIFIC_PAIN,
        FailedGate.WHY_NOW,
        FailedGate.GENERIC_PATTERN,
    }
)


@dataclass
class GoldenResult:
    golden: Golden
    status: RunStatus
    verdict: Verdict | None
    cost_usd: float
    leads: int = 0
    killed_with_reason: int = 0
    anti_generic_kills: int = 0
    fatal_flaw: bool = False
    fact_claims: int = 0
    hallucinated_facts: int = 0
    tier_distribution: dict[str, int] = field(default_factory=dict)
    economics_ok: bool | None = None
    small_team_ok: bool | None = None
    rubric_score: float | None = None

    @property
    def hallucination_rate(self) -> float:
        return self.hallucinated_facts / self.fact_claims if self.fact_claims else 0.0


@contextmanager
def _sessions() -> Iterator[sessionmaker[Session]]:
    with TemporaryDirectory() as tmp:
        engine = create_db_engine(f"sqlite:///{Path(tmp) / 'evals.db'}")
        Base.metadata.create_all(engine)
        try:
            yield create_session_factory(engine)
        finally:
            engine.dispose()


def run_golden(golden: Golden) -> GoldenResult:
    with _sessions() as sessions:
        sink = DbTraceSink(sessions)
        orchestrator = Orchestrator(
            StageServices(
                sessions=sessions,
                gateway=LLMGateway(golden.make_llm(), ROUTING, sink),
                search=SnippetSearch(),
                sink=sink,
                search_cost_estimate_usd=0.008,
                discovery_budget=ResearchBudget(),
            )
        )
        outcome = asyncio.run(orchestrator.start(golden.config))
        with sessions() as session:
            return _score(session, golden, outcome.run_id)


def _score(session: Session, golden: Golden, run_id: UUID) -> GoldenResult:
    run = RunRepository(session).get(run_id)
    assert run is not None
    leads = LeadRepository(session).list_for_run(run_id)
    opportunities = OpportunityRepository(session).list(run_id=run_id)
    selected = (
        OpportunityRepository(session).get(run.selected_opportunity_id)
        if run.selected_opportunity_id
        else None
    )

    result = GoldenResult(
        golden=golden,
        status=run.status,
        verdict=selected.evaluation.verdict if selected and selected.evaluation else None,
        cost_usd=run.cost_to_date_usd,
        leads=len(leads),
    )

    # Kills, split into anti-generic (too vague) vs. other (e.g. duplicate).
    kill_repo = KillRepository(session)
    for lead in leads:
        if lead.status not in (LeadStatus.KILLED, LeadStatus.DUPLICATE):
            continue
        for kill in kill_repo.list_for_subject(lead.id):
            if kill.reason:
                result.killed_with_reason += 1
            if kill.failed_gate in _ANTI_GENERIC_GATES:
                result.anti_generic_kills += 1

    # Hallucination + source-tier mix over every claim the run produced.
    claims = _all_claims(session, leads, opportunities)
    stored_sources = SourceRepository(session).get_many(
        {sid for claim in claims for sid in claim.source_ids}
    )
    for claim in claims:
        resolved = [stored_sources[sid] for sid in claim.source_ids if sid in stored_sources]
        if claim.type is ClaimType.FACT:
            result.fact_claims += 1
            if not resolved:
                result.hallucinated_facts += 1
        if claim.type in (ClaimType.FACT, ClaimType.ESTIMATE):
            for source in resolved:
                tier = source.credibility_tier.value
                result.tier_distribution[tier] = result.tier_distribution.get(tier, 0) + 1

    if selected is not None:
        result.fatal_flaw = selected.red_team.fatal_flaw
        result.economics_ok = _economics_ok(selected)
        result.small_team_ok = _small_team_ok(selected)
        reports = ReportRepository(session).list_for_run(run_id)
        if reports:
            latest = max(reports, key=lambda r: r.created_at)
            result.rubric_score = rubric_score(selected, latest)
    return result


def _all_claims(
    session: Session, leads: list[Lead], opportunities: list[Opportunity]
) -> list[Claim]:
    claims = ClaimRepository(session)
    by_id: dict[UUID, Claim] = {}
    for lead in leads:
        for claim in claims.list_for_lead(lead.id):
            by_id[claim.id] = claim
    for opp in opportunities:
        for claim in claims.list_for_opportunity(opp.id):
            by_id[claim.id] = claim
    return list(by_id.values())


def _economics_ok(opp: Opportunity) -> bool:
    """Three scenarios with outputs, load-bearing assumptions named, and every load-bearing
    one mapped to a validation experiment (specs.md §14-§15)."""
    scenarios = opp.economics.scenarios
    if not (scenarios.conservative.outputs and scenarios.base.outputs and scenarios.upside.outputs):
        return False
    load_bearing = opp.economics.load_bearing_assumption_ids
    if not load_bearing:
        return False
    tested = {exp.assumption_claim_id for exp in opp.validation_backlog}
    return all(assumption_id in tested for assumption_id in load_bearing)


def _small_team_ok(opp: Opportunity) -> bool:
    """Both mandated questions answered with a rationale (specs.md §11)."""
    feasibility = opp.small_team_feasibility
    return all(
        claim is not None and claim.statement and claim.confidence_reason
        for claim in (feasibility.can_one_person_start, feasibility.can_1_3_operate)
    )
