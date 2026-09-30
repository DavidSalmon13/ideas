"""The refresh endpoint (plan.md §11, specs.md §2.11): POST /opportunities/{id}/refresh
re-verifies stale claims and reports what changed. Runs offline over the refresh fakes."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from opp.agents.budget import ResearchBudget
from opp.api.app import create_app
from opp.api.engine import Engine, RunManager
from opp.db.base import Base
from opp.db.repositories import OpportunityRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.base import utcnow
from opp.domain.enums import BusinessShape, ClaimType, Level, SourceType
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Market, Opportunity
from opp.domain.run import RunConfig
from opp.evidence.store import EvidenceStore
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices
from tests.agents.fakes import SnippetSearch
from tests.agents.test_discovery import ROUTING
from tests.orchestration.test_refresh import RefreshLLM


class RefreshHarness:
    def __init__(self, tmp_path: Path) -> None:
        db = create_db_engine(f"sqlite:///{tmp_path / 'refresh_api.db'}")
        Base.metadata.create_all(db)
        self.sessions = create_session_factory(db)
        sink = DbTraceSink(self.sessions)
        services = StageServices(
            sessions=self.sessions,
            gateway=LLMGateway(RefreshLLM(), ROUTING, sink),
            search=SnippetSearch(),
            sink=sink,
            search_cost_estimate_usd=0.008,
            discovery_budget=ResearchBudget(),
        )
        self.orchestrator = Orchestrator(services)
        self.engine = Engine(
            sessions=self.sessions,
            orchestrator=self.orchestrator,
            run_manager=RunManager(),
            stage_minimums_usd=services.stage_minimums_usd,
        )
        self.app = create_app()
        self.app.state.engine = self.engine

    def seed(self, *, days_old: float) -> UUID:
        run = self.orchestrator.create_run(RunConfig())
        src = Source(
            url="https://oldreport.example.com/market",
            title="Old report",
            source_type=SourceType.INDUSTRY_REPORT,
            credibility_tier=Level.HIGH,
            excerpt="the original figure",
        )
        opp = Opportunity(
            run_id=run.id,
            title="Refrigerant tracker",
            business_shape=BusinessShape.SAAS,
            profile_ref="bootstrap",
            market=Market(
                size=Claim(
                    statement="market is $50M",
                    type=ClaimType.FACT,
                    source_ids=[src.id],
                    confidence=Level.HIGH,
                    confidence_reason="stated in a report",
                    observed_at=utcnow() - timedelta(days=days_old),
                )
            ),
        )
        with self.sessions() as session:
            store = EvidenceStore(session)
            store.add_source(src)
            store.save_opportunity(opp)
            session.commit()
        return opp.id


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[RefreshHarness]:
    yield RefreshHarness(tmp_path)


def test_refresh_endpoint_reverifies_and_reports(harness: RefreshHarness) -> None:
    opp_id = harness.seed(days_old=200)
    with TestClient(harness.app) as client:
        response = client.post(f"/opportunities/{opp_id}/refresh", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["stale_claims"] == 1 and body["refreshed"] == 1
    assert body["status"] == "completed" and body["run_id"] is not None

    with harness.sessions() as session:
        opp = OpportunityRepository(session).get(opp_id)
    assert opp is not None and opp.market.size is not None
    assert opp.market.size.value == 99.0  # updated in place by the refresh


def test_refresh_endpoint_reports_nothing_stale(harness: RefreshHarness) -> None:
    opp_id = harness.seed(days_old=5)
    with TestClient(harness.app) as client:
        body = client.post(f"/opportunities/{opp_id}/refresh", json={}).json()
    assert body["stale_claims"] == 0 and body["run_id"] is None


def test_refresh_endpoint_unknown_opportunity_is_404(harness: RefreshHarness) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    with TestClient(harness.app) as client:
        response = client.post(f"/opportunities/{missing}/refresh", json={})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
