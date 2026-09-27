"""Shallow validation: full-framework provisional scores, held to their evidence."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.runtime import AgentRuntime
from opp.agents.shallow_validation import ShallowValidationAgent
from opp.config.profiles import composite, effective_weights
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    OpportunityRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import ClaimType, Dimension, EvidenceBasis, ModelTier, StageName
from opp.domain.run import AgentRun, ResearchRun, RunConfig, Stage
from opp.evidence.store import EvidenceStore
from opp.llm.fake import reply
from opp.llm.gateway import LLMGateway
from opp.llm.types import Completion, CompletionRequest
from opp.observability.traces import DbTraceSink
from opp.research.search import Freshness, SearchError, SearchResponse, SearchResult
from tests.agents.test_discovery import ROUTING
from tests.agents.test_triage import FORUM, RULE, SOURCES, lead


@dataclass
class ScoringLLM:
    dimensions: list[dict[str, Any]]
    findings: list[dict[str, Any]] = field(default_factory=list)
    name: str = "scorer"
    requests: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        body = {"dimensions": self.dimensions, "findings": self.findings}
        return reply(json.dumps(body), model=request.model)


@dataclass
class CorroborationSearch:
    name: str = "corroboration"
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        self.calls.append(query)
        if self.fail:
            raise SearchError("provider down", provider=self.name)
        results = [
            SearchResult(
                title="Survey",
                url="https://www.achrnews.com/survey",
                snippet="62% of small HVAC firms still dispatch by phone",
            )
        ]
        return SearchResponse(query=query, provider=self.name, results=results, cost_usd=0.008)


def dim(dimension: Dimension, score: int, evidence: str, cites: list[str]) -> dict[str, Any]:
    return {
        "dimension": dimension.value,
        "score": score,
        "rationale": f"{dimension.value} rationale",
        "evidence": evidence,
        "cites": cites,
    }


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'shallow.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def score(
    sessions: sessionmaker[Session], llm: ScoringLLM, search: CorroborationSearch
) -> tuple[ResearchRun, Any]:
    run = ResearchRun(run_config=RunConfig())
    stage = Stage(run_id=run.id, name=StageName.SHALLOW_VALIDATION)
    agent_run = AgentRun(stage_id=stage.id, agent_name="shallow", model_tier=ModelTier.MEDIUM)
    candidate = lead()
    candidate.run_id = run.id
    with sessions() as session:
        RunRepository(session).add(run)
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        store = EvidenceStore(session)
        for source in SOURCES.values():
            store.add_source(source)
        store.save_lead(candidate)
        session.commit()
    sink = DbTraceSink(sessions)
    runtime = AgentRuntime(
        sessions, gateway=LLMGateway(llm, ROUTING, sink), sink=sink, search=search
    )
    result = asyncio.run(
        runtime.run(
            ShallowValidationAgent([candidate], SOURCES),
            agent_run.id,
            budget=ResearchBudget(max_cost_usd=1.0),
            run_budget=RunBudget(20),
        )
    )
    with sessions() as session:
        [opportunity] = OpportunityRepository(session).list(run_id=run.id)
    return run, (opportunity, candidate, result)


def test_scores_are_capped_by_their_evidence(sessions: sessionmaker[Session]) -> None:
    llm = ScoringLLM(
        dimensions=[
            dim(Dimension.CUSTOMER_PAIN, 5, "sourced", ["C2", "F1"]),
            dim(Dimension.WHY_NOW, 5, "sourced", ["C9"]),  # cites nothing real
            dim(Dimension.GROSS_MARGIN, 5, "inferred", []),
            dim(Dimension.WILLINGNESS_TO_PAY, 5, "insufficient", []),
        ],
        findings=[
            {"statement": "62% still dispatch by phone", "snippets": [1], "confidence": "medium",
             "confidence_reason": "trade survey"},
            {"statement": "invented", "snippets": [7], "confidence": "high",
             "confidence_reason": "none"},
        ],
    )  # fmt: skip
    _, (opportunity, candidate, result) = score(sessions, llm, CorroborationSearch())
    shallow = opportunity.shallow
    assert shallow is not None

    pain = shallow.score_for(Dimension.CUSTOMER_PAIN)
    [finding] = opportunity.screening_claims  # the uncited finding was dropped
    assert pain.score == 5 and pain.evidence is EvidenceBasis.SOURCED
    assert pain.supporting_claim_ids == [candidate.specific_pain.id, finding.id]
    why = shallow.score_for(Dimension.WHY_NOW)
    assert (why.score, why.evidence) == (3, EvidenceBasis.INFERRED)
    assert shallow.score_for(Dimension.GROSS_MARGIN).score == 3
    assert shallow.score_for(Dimension.WILLINGNESS_TO_PAY).score == 2
    unassessed = shallow.score_for(Dimension.SCALABILITY)
    assert (unassessed.score, unassessed.evidence) == (1, EvidenceBasis.INSUFFICIENT)
    assert len(shallow.dimension_scores) == len(Dimension)

    expected = composite(
        {s.dimension: s.score for s in shallow.dimension_scores}, effective_weights(RunConfig())
    )
    assert shallow.composite == pytest.approx(expected, abs=1e-3)
    assert shallow.weights_ref == "bootstrap"

    # The finding is a sourced fact; its snippet is stored as a source.
    assert finding.type is ClaimType.FACT and finding.source_ids
    with sessions() as session:
        stored = SourceRepository(session).get_many(finding.source_ids)
    assert [s.url for s in stored.values()] == ["https://www.achrnews.com/survey"]
    # The opportunity is seeded from the lead's own claims.
    assert opportunity.lead_id == candidate.id
    assert opportunity.customer.segment.id == candidate.named_customer.id
    assert result.tasks_done == 1


def test_a_failed_search_still_scores_on_the_leads_own_evidence(
    sessions: sessionmaker[Session],
) -> None:
    llm = ScoringLLM(dimensions=[dim(Dimension.CUSTOMER_PAIN, 4, "sourced", ["C2"])])
    _, (opportunity, _, _) = score(sessions, llm, CorroborationSearch(fail=True))

    assert opportunity.shallow.score_for(Dimension.CUSTOMER_PAIN).score == 4
    assert "search failed" in llm.requests[0].messages[-1].content
    # The lead's quoted evidence reaches the model only inside DATA blocks.
    prompt = llm.requests[0].messages[-1].content
    assert FORUM.url in prompt and RULE.url in prompt
