"""Shallow validation: one search + at most one page per lead, mechanical score caps,
profile-weighted composite, ranking."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.runtime import AgentRuntime
from opp.agents.shallow_validation import (
    DimensionAssessment,
    ShallowValidationAgent,
    corroboration_query,
)
from opp.config.profiles import BOOTSTRAP, weighted_composite
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    ClaimRepository,
    LeadRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    AgentRunStatus,
    ClaimType,
    Dimension,
    EvidenceSufficiency,
    LeadStatus,
    Level,
    ModelTier,
    StageName,
)
from opp.domain.evidence import Claim
from opp.domain.lead import Lead, TriageAssessment
from opp.domain.run import AgentRun, ResearchRun, RunConfig, Stage
from opp.evidence.store import EvidenceStore
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from tests.agents.fakes import FunnelLLM, PageStub, SnippetSearch
from tests.agents.test_runtime import ROUTING
from tests.agents.test_triage import FORUM, GOV, build_leads

BUDGET = ResearchBudget(max_cost_usd=3.0, max_llm_calls=10, max_searches=8, max_fetches=5)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'shallow.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def setup(sessions: sessionmaker[Session]) -> tuple[UUID, UUID, list[Lead]]:
    run = ResearchRun()
    stage = Stage(run_id=run.id, name=StageName.SHALLOW_VALIDATION)
    agent_run = AgentRun(
        stage_id=stage.id, agent_name="shallow_validation", model_tier=ModelTier.MEDIUM
    )
    candidates = []
    for rank, name in enumerate(["good", "seen_before"], start=1):
        lead = build_leads(run.id)[name]
        lead.status = LeadStatus.KEPT
        lead.triage = TriageAssessment(
            source_strength=4,
            specificity=4,
            smallteam_fit=4,
            cheap_score=4.0,
            reason="kept",
            rank=rank,
        )
        candidates.append(lead)
    with sessions() as session:
        RunRepository(session).add(run)
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        store = EvidenceStore(session)
        for source in (FORUM, GOV):
            store.add_source(source)
        for lead in candidates:
            store.save_lead(lead)
        session.commit()
    return run.id, agent_run.id, candidates


def score(
    sessions: sessionmaker[Session],
    agent_run_id: UUID,
    candidates: list[Lead],
    fetcher: PageStub,
    budget: ResearchBudget = BUDGET,
) -> tuple[AgentRunStatus, FunnelLLM, SnippetSearch]:
    llm, search, sink = FunnelLLM(), SnippetSearch(), DbTraceSink(sessions)
    runtime = AgentRuntime(
        sessions,
        gateway=LLMGateway(llm, ROUTING, sink),
        sink=sink,
        search=search,
        fetcher=fetcher,
    )
    agent = ShallowValidationAgent(
        candidates,
        {s.id: s for s in (FORUM, GOV)},
        BOOTSTRAP,
        BOOTSTRAP.weights_for(RunConfig()),
    )
    result = asyncio.run(
        runtime.run(agent, agent_run_id, budget=budget, run_budget=RunBudget(20.0))
    )
    return result.status, llm, search


def test_scores_ranks_and_caps(sessions: sessionmaker[Session]) -> None:
    run_id, agent_run_id, candidates = setup(sessions)
    fetcher = PageStub(fail_for=("/comments/2",))

    status, llm, search = score(sessions, agent_run_id, candidates, fetcher)

    assert status is AgentRunStatus.COMPLETED
    assert len(search.calls) == 2 and len(llm.requests) == 2
    assert search.calls[0] == corroboration_query(candidates[0])
    assert len(fetcher.calls) == 2  # one page per lead; the second fails and is skipped
    with sessions() as session:
        leads = {le.id: le for le in LeadRepository(session).list_for_run(run_id)}
        lead_claims = ClaimRepository(session).list_for_lead(candidates[0].id)
        sources = SourceRepository(session)
        first = leads[candidates[0].id]
        assert first.shallow is not None
        [finding] = [c for c in lead_claims if c.statement.startswith("Competing dispatch")]
        [finding_source] = sources.get_many(finding.source_ids).values()

    assert all(le.status is LeadStatus.SHORTLISTED for le in leads.values())
    shallow = first.shallow
    assert len(shallow.dimension_scores) == len(Dimension)
    defensibility = shallow.score_for(Dimension.DEFENSIBILITY)
    assert defensibility is not None
    assert (defensibility.score, defensibility.evidence) == (2, EvidenceSufficiency.INSUFFICIENT)
    pain = shallow.score_for(Dimension.CUSTOMER_PAIN)
    assert pain is not None and pain.score == 4
    assert pain.supporting_claim_ids == [candidates[0].named_customer.id, finding.id]
    assert shallow.provisional_composite == weighted_composite(
        ((s.dimension, s.score) for s in shallow.dimension_scores), BOOTSTRAP.weights
    )
    assert shallow.weights_ref == "bootstrap" and shallow.summary == "Real pain."
    # The finding is a sourced fact filed under the lead, citing the search snippet.
    assert finding.type is ClaimType.FACT and finding_source.url.startswith("https://www.reddit")
    # Same composite: the triage rank breaks the tie.
    ranks = {le.id: le.shallow.rank for le in leads.values() if le.shallow}
    assert ranks == {candidates[0].id: 1, candidates[1].id: 2}
    # New evidence reaches the model only inside the untrusted block; the page text too.
    prompt = llm.requests[0].messages[-1].content
    assert "<<<DATA" in prompt and "start at $49 per month" in prompt


def test_fetch_budget_limits_pages(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id, candidates = setup(sessions)
    fetcher = PageStub()
    one_page = BUDGET.model_copy(update={"max_fetches": 1})
    status, _, _ = score(sessions, agent_run_id, candidates, fetcher, one_page)
    assert status is AgentRunStatus.COMPLETED
    assert len(fetcher.calls) == 1  # the second lead is scored from snippets alone


def test_mechanical_caps_on_provisional_scores() -> None:
    assumption = Claim(
        statement="they would pay",
        type=ClaimType.ASSUMPTION,
        confidence=Level.LOW,
        confidence_reason="a guess",
    )
    fact = Claim(
        statement="they pay $49",
        type=ClaimType.FACT,
        source_ids=[FORUM.id],
        confidence=Level.MEDIUM,
        confidence_reason="pricing page",
    )
    scores = ShallowValidationAgent.provisional_scores(
        [
            DimensionAssessment(
                dimension=Dimension.WILLINGNESS_TO_PAY,
                score=5,
                rationale="they'd pay",
                evidence=EvidenceSufficiency.SUFFICIENT,
                claims=["C1"],
            ),
            DimensionAssessment(
                dimension=Dimension.GROSS_MARGIN,
                score=5,
                rationale="software",
                evidence=EvidenceSufficiency.PARTIAL,
                claims=["C2", "X9"],
            ),
            DimensionAssessment(
                dimension=Dimension.SCALABILITY,
                score=4,
                rationale="no idea",
                evidence=EvidenceSufficiency.INSUFFICIENT,
                claims=["C2"],
            ),
        ],
        {"C1": assumption, "C2": fact},
    )
    by = {s.dimension: s for s in scores}
    assert len(scores) == len(Dimension)
    assert by[Dimension.WILLINGNESS_TO_PAY].score == 2  # rests on an assumption only
    assert by[Dimension.WILLINGNESS_TO_PAY].evidence is EvidenceSufficiency.INSUFFICIENT
    assert by[Dimension.GROSS_MARGIN].score == 5  # a solid fact; unknown labels ignored
    assert by[Dimension.GROSS_MARGIN].supporting_claim_ids == [fact.id]
    assert by[Dimension.SCALABILITY].score == 2  # the model itself said insufficient
    assert by[Dimension.MARKET_ATTRACTIVENESS].score == 1  # skipped by the model
