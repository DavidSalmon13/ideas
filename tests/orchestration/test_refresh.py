"""V1 refresh (plan.md §35 Phase 9, specs.md §2.11, §7.8): re-verify an opportunity's stale
claims against fresh evidence. Driven through the orchestrator on offline fakes — a scripted
search and an LLM that returns a ClaimUpdate — so no network or keys are touched."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import ClaimRepository, OpportunityRepository, SourceRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.base import utcnow
from opp.domain.enums import BusinessShape, ClaimType, Level, RunStatus, SourceType
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Market, Opportunity, Problem
from opp.domain.run import RunConfig
from opp.evidence.store import EvidenceStore
from opp.llm.fake import reply
from opp.llm.gateway import LLMGateway
from opp.llm.types import Completion, CompletionRequest
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator, RefreshError
from opp.orchestration.stages import StageServices
from tests.agents.fakes import SnippetSearch
from tests.agents.test_discovery import ROUTING


@dataclass
class RefreshLLM:
    """Answers the refresh judgement: every claim comes back 'updated' with a new figure
    cited to the first fresh snippet."""

    name: str = "fake-refresh-llm"
    requests: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        kind = request.output_type.__name__ if request.output_type else ""
        assert kind == "ClaimUpdate", f"unexpected output type {kind}"
        payload = {
            "status": "updated",
            "statement": "market is now $99M",
            "kind": "fact",
            "value": 99.0,
            "unit": "USD M",
            "snippets": [1],
            "confidence": "medium",
            "confidence_reason": "a 2026 industry report gives the new figure",
        }
        return reply(json.dumps(payload), model=request.model, input_tokens=300, output_tokens=80)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'refresh.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session]) -> Orchestrator:
    sink = DbTraceSink(sessions)
    return Orchestrator(
        StageServices(
            sessions=sessions,
            gateway=LLMGateway(RefreshLLM(), ROUTING, sink),
            search=SnippetSearch(),
            sink=sink,
            search_cost_estimate_usd=0.008,
            discovery_budget=ResearchBudget(),
        )
    )


def _source(url: str) -> Source:
    return Source(
        url=url,
        title="Old report",
        source_type=SourceType.INDUSTRY_REPORT,
        credibility_tier=Level.HIGH,
        excerpt="the original figure",
    )


def _fact(statement: str, *, days_old: float, sources: list[Source]) -> Claim:
    return Claim(
        statement=statement,
        type=ClaimType.FACT,
        source_ids=[s.id for s in sources],
        confidence=Level.HIGH,
        confidence_reason="stated in a report",
        stage="deep_research",
        agent="specialist:market",
        observed_at=utcnow() - timedelta(days=days_old),
    )


def _seed_opportunity(
    sessions: sessionmaker[Session], orch: Orchestrator, *, days_old: float
) -> UUID:
    run = orch.create_run(RunConfig())
    src_a, src_b = (
        _source("https://oldreport.example.com/market"),
        _source("https://oldpain.example.com"),
    )
    opp = Opportunity(
        run_id=run.id,
        title="Refrigerant tracker",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        market=Market(size=_fact("market is $50M", days_old=days_old, sources=[src_a])),
        problem=Problem(
            description=_fact("clinics lose a day a week", days_old=days_old, sources=[src_b])
        ),
    )
    with sessions() as session:
        store = EvidenceStore(session)
        store.add_source(src_a)
        store.add_source(src_b)
        store.save_opportunity(opp)
        session.commit()
    return opp.id


def test_refresh_reverifies_stale_claims_in_place(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions)
    opp_id = _seed_opportunity(sessions, orch, days_old=200)
    before = utcnow()

    outcome = asyncio.run(orch.refresh(opp_id))

    assert outcome.run_id is not None
    assert outcome.stale_claims == 2 and outcome.refreshed == 2
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.cost_usd > 0  # the refresh spent on search + judgement, metered

    with sessions() as session:
        opp = OpportunityRepository(session).get(opp_id)
        assert opp is not None
        size = opp.market.size
        claims = ClaimRepository(session).list_for_opportunity(opp_id)
        source_urls = {
            s.url
            for s in SourceRepository(session)
            .get_many({sid for c in claims for sid in c.source_ids})
            .values()
        }
    # The claim was updated in place: same id, fresh figure, fresh timestamp.
    assert size is not None
    assert size.value == 99.0 and size.unit == "USD M"
    assert size.observed_at > before
    assert "refreshed" in (size.notes or "")
    # The claim now cites the fresh source found during the refresh.
    assert any("reddit.com" in url for url in source_urls)
    # The index reflects the update too (traceability holds).
    indexed = {c.id: c for c in claims}
    assert indexed[size.id].value == 99.0


def test_refresh_with_nothing_stale_does_no_work(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions)
    opp_id = _seed_opportunity(sessions, orch, days_old=5)  # everything fresh

    outcome = asyncio.run(orch.refresh(opp_id))

    assert outcome.run_id is None
    assert outcome.stale_claims == 0 and outcome.refreshed == 0
    assert outcome.cost_usd == 0.0
    assert "no stale claims" in outcome.detail


def test_refresh_respects_a_tighter_max_age(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions)
    opp_id = _seed_opportunity(sessions, orch, days_old=40)  # older than 30, younger than 90

    # Default window (90d) sees nothing; a 30-day window sees both claims.
    assert asyncio.run(orch.refresh(opp_id)).run_id is None
    tighter = asyncio.run(orch.refresh(opp_id, max_age_days=30))
    assert tighter.refreshed == 2


def test_refresh_unknown_opportunity_raises(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions)
    with pytest.raises(RefreshError, match="no opportunity"):
        asyncio.run(orch.refresh(uuid4()))
