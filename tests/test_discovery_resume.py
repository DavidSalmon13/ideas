"""Phase 3 acceptance: a discovery run killed mid-way (or halted on budget) continues from
its ledgers, and every lead it ends with has a source."""

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.discovery import DiscoveryAgent
from opp.cli import main as cli
from opp.cli.discover import (
    ResumeError,
    create_discovery_run,
    execute_discovery,
    resume_discovery,
    run_discovery,
)
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    LeadRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import AgentRunStatus, RunStatus, SignalSource, StageStatus
from opp.domain.run import AgentRun, DiscoveryScope, LeadTargets, RunConfig
from opp.research.search import Freshness, SearchResponse
from tests.agents.fakes import DiscoveryLLM, SnippetSearch
from tests.test_cli import services

SIGNAL = SignalSource.UNMET_NEED
CONFIG = RunConfig(
    discovery_scope=DiscoveryScope(industry="HVAC"), lead_targets=LeadTargets(discovery_n=4)
)


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_db_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


class Killed(BaseException):
    """Stands in for the process dying: not an ``Exception``, so nothing catches it."""


@dataclass
class DyingSearch(SnippetSearch):
    die_on_call: int = 3

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        if len(self.calls) + 1 == self.die_on_call:
            raise Killed
        return await super().search(query, k=k, freshness=freshness)


def agent_run(sessions: sessionmaker[Session], run_id: UUID) -> AgentRun:
    with sessions() as session:
        [stage] = StageRepository(session).list_for_run(run_id)
        [found] = AgentRunRepository(session).list_for_stage(stage.id)
    return found


def assert_all_leads_sourced(sessions: sessionmaker[Session], run_id: UUID) -> int:
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run_id)
        sources = SourceRepository(session).get_many({s for le in leads for s in le.source_ids})
    assert leads
    for lead in leads:
        assert lead.source_ids and lead.source_ids <= set(sources)
    return len(leads)


def test_killed_run_resumes_from_the_ledger(sessions: sessionmaker[Session]) -> None:
    run_id = create_discovery_run(sessions, CONFIG, [SIGNAL])
    dying = DyingSearch(die_on_call=3)
    with pytest.raises(Killed):
        asyncio.run(execute_discovery(services(sessions, DiscoveryLLM(), dying), run_id))

    # What the dead process left behind: two committed sub-tasks, their leads, a live run.
    interrupted = agent_run(sessions, run_id)
    assert interrupted.status is AgentRunStatus.RUNNING
    assert interrupted.work_ledger.done == ["query-0", "query-1"]
    assert assert_all_leads_sourced(sessions, run_id) == 2
    with sessions() as session:
        run = RunRepository(session).get(run_id)
    assert run is not None and run.status is RunStatus.DISCOVERING
    spent_before = run.cost_to_date_usd

    search = SnippetSearch()
    outcome = asyncio.run(resume_discovery(services(sessions, DiscoveryLLM(), search), run_id))

    queries = DiscoveryAgent(SIGNAL, target_leads=4).queries(CONFIG)
    assert search.calls == queries[2:4]  # nothing done twice; stops once the target is met
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert outcome.results[SIGNAL].leads_saved == 2
    assert assert_all_leads_sourced(sessions, run_id) == 4
    finished = agent_run(sessions, run_id)
    assert finished.status is AgentRunStatus.COMPLETED
    assert finished.work_ledger.pending == []
    assert finished.output_counts["leads"] == 4
    assert outcome.cost_usd > spent_before
    with sessions() as session:
        [stage] = StageRepository(session).list_for_run(run_id)
    assert stage.status is StageStatus.COMPLETED


def test_budget_halt_resumes_after_a_top_up(sessions: sessionmaker[Session]) -> None:
    tight = CONFIG.model_copy(update={"budget_ceiling_usd": 0.02})
    halted = asyncio.run(
        run_discovery(services(sessions, DiscoveryLLM(), SnippetSearch()), tight, [SIGNAL])
    )
    assert halted.status is RunStatus.HALTED_BUDGET
    assert agent_run(sessions, halted.run_id).status is AgentRunStatus.HALTED_BUDGET

    # Resuming without more money just halts again; nothing is lost.
    again = asyncio.run(
        resume_discovery(services(sessions, DiscoveryLLM(), SnippetSearch()), halted.run_id)
    )
    assert again.status is RunStatus.HALTED_BUDGET

    topped = asyncio.run(
        resume_discovery(
            services(sessions, DiscoveryLLM(), SnippetSearch()),
            halted.run_id,
            budget_ceiling_usd=20.0,
        )
    )
    assert topped.status is RunStatus.COMPLETED_PARTIAL
    assert assert_all_leads_sourced(sessions, halted.run_id) == 4
    with sessions() as session:
        run = RunRepository(session).get(halted.run_id)
    assert run is not None and run.budget_ceiling_usd == 20.0


def test_only_unfinished_discovery_runs_resume(sessions: sessionmaker[Session]) -> None:
    svc = services(sessions, DiscoveryLLM(), SnippetSearch())
    done = asyncio.run(run_discovery(svc, CONFIG, [SIGNAL]))
    with pytest.raises(ResumeError, match="completed_partial"):
        asyncio.run(resume_discovery(svc, done.run_id))
    with pytest.raises(ResumeError, match="No run"):
        asyncio.run(resume_discovery(svc, uuid4()))


def test_cli_resume(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tight = CONFIG.model_copy(update={"budget_ceiling_usd": 0.02})
    halted = asyncio.run(
        run_discovery(services(sessions, DiscoveryLLM(), SnippetSearch()), tight, [SIGNAL])
    )
    monkeypatch.setattr(cli, "_providers", lambda *_: (DiscoveryLLM(), SnippetSearch()))

    assert cli.main(["resume", str(halted.run_id), "--budget", "20"]) == 0
    captured = capsys.readouterr()
    assert f"Resuming run {halted.run_id}" in captured.err
    assert "4 lead(s)" in captured.out
    assert "of $20.00" in captured.out

    assert cli.main(["resume", str(halted.run_id)]) == 1
    assert "nothing to resume" in capsys.readouterr().err
