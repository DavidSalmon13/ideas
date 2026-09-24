"""Phase 3 acceptance (plan.md §24): killing the process mid-run and resuming continues
from the ledger, and no lead is ever persisted without a source."""

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.agents.discovery import DiscoveryAgent
from opp.cli.discover import (
    DiscoveryServices,
    execute_discovery,
    run_discovery,
    signal_of,
    start_discovery,
)
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    LeadRepository,
    RunRepository,
    StageRepository,
    TraceRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import AgentRunStatus, RunStatus, SignalSource
from opp.domain.run import LeadTargets, RunConfig
from opp.evidence.audit import audit_run
from opp.llm.factory import build_gateway
from opp.observability.traces import DbTraceSink
from tests.agents.fakes import DiscoveryLLM, SnippetSearch

ROOT = Path(__file__).resolve().parents[1]

# Runs discovery in a child process and kills it hard (os._exit, no cleanup) in the
# middle of the third LLM call, as a crash or `kill -9` would.
CHILD = textwrap.dedent(
    """
    import asyncio, os, sys
    from opp.agents.budget import ResearchBudget
    from opp.cli.discover import DiscoveryServices, run_discovery
    from opp.config.settings import get_settings
    from opp.db.session import create_db_engine, create_session_factory
    from opp.domain.enums import SignalSource
    from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
    from opp.llm.factory import build_gateway
    from opp.observability.traces import DbTraceSink
    from tests.agents.fakes import DiscoveryLLM, SnippetSearch

    class KillingLLM(DiscoveryLLM):
        async def complete(self, request):
            if len(self.requests) == 2:
                os._exit(137)
            return await super().complete(request)

    sessions = create_session_factory(create_db_engine(sys.argv[1]))
    sink = DbTraceSink(sessions)
    services = DiscoveryServices(
        sessions=sessions,
        gateway=build_gateway(get_settings(), sink, provider=KillingLLM()),
        search=SnippetSearch(),
        sink=sink,
        budget=ResearchBudget(max_cost_usd=1.0),
        search_cost_estimate_usd=0.008,
    )
    config = RunConfig(
        discovery_scope=DiscoveryScope(industry="HVAC"),
        lead_targets=LeadTargets(discovery_n=6),
    )
    asyncio.run(run_discovery(
        services, config, [SignalSource.UNMET_NEED, SignalSource.MANUAL_PROCESS]
    ))
    """
)


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'killed.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def services(
    sessions: sessionmaker[Session], llm: DiscoveryLLM, search: SnippetSearch
) -> DiscoveryServices:
    sink = DbTraceSink(sessions)
    return DiscoveryServices(
        sessions=sessions,
        gateway=build_gateway(get_settings(), sink, provider=llm),
        search=search,
        sink=sink,
        budget=ResearchBudget(max_cost_usd=1.0),
        search_cost_estimate_usd=0.008,
    )


def test_killed_run_resumes_from_the_ledger(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'killed.db'}"
    child = subprocess.run(
        [sys.executable, "-c", CHILD, url],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 137, child.stderr

    # The process died mid-run: the run is non-terminal and some work is committed.
    with sessions() as session:
        [run] = RunRepository(session).list_resumable()
        assert run.status is RunStatus.DISCOVERING
        [stage] = StageRepository(session).list_for_run(run.id)
        before = {ar.id: ar for ar in AgentRunRepository(session).list_for_stage(stage.id)}
        leads_before = LeadRepository(session).list_for_run(run.id)
        spent_before = TraceRepository(session).total_cost_for_run(run.id)
    assert 1 <= len(leads_before) < 6
    assert any(ar.status is AgentRunStatus.RUNNING for ar in before.values())
    done_queries = {
        query
        for ar in before.values()
        for query in _queries(ar, run.run_config, ar.work_ledger.done)
    }
    assert done_queries

    # Resume with healthy providers.
    search = SnippetSearch()
    outcome = asyncio.run(execute_discovery(services(sessions, DiscoveryLLM(), search), run.id))

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert not done_queries & set(search.calls), "committed sub-tasks were redone"
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run.id)
        after = AgentRunRepository(session).list_for_stage(stage.id)
        assert audit_run(session, run.id) == []
        total_spent = TraceRepository(session).total_cost_for_run(run.id)
        stored_run = RunRepository(session).get(run.id)
    assert len(leads) == 6  # the target, not more: nothing duplicated by the resume
    assert len({lead.title for lead in leads}) == 6
    assert {lead.id for lead in leads_before} <= {lead.id for lead in leads}
    assert all(ar.status is AgentRunStatus.COMPLETED for ar in after)
    # Spend from before the crash still counts against the run.
    assert stored_run is not None
    assert stored_run.cost_to_date_usd == pytest.approx(total_spent)
    assert total_spent > spent_before


def _queries(agent_run, run_config, tasks):  # type: ignore[no-untyped-def]
    agent = DiscoveryAgent(signal_of(agent_run), target_leads=3)
    queries = agent.queries(run_config)
    return [queries[int(task.removeprefix("query-"))] for task in tasks]


def test_resuming_a_finished_run_does_nothing(sessions: sessionmaker[Session]) -> None:
    config = RunConfig(lead_targets=LeadTargets(discovery_n=2))
    first = asyncio.run(
        run_discovery(
            services(sessions, DiscoveryLLM(), SnippetSearch()), config, [SignalSource.UNMET_NEED]
        )
    )
    llm, search = DiscoveryLLM(), SnippetSearch()
    again = asyncio.run(execute_discovery(services(sessions, llm, search), first.run_id))

    assert again.status is RunStatus.COMPLETED_PARTIAL
    assert llm.requests == [] and search.calls == []
    assert again.cost_usd == pytest.approx(first.cost_usd)


def test_top_up_must_raise_the_ceiling(sessions: sessionmaker[Session]) -> None:
    run_id = start_discovery(sessions, RunConfig(budget_ceiling_usd=10), [SignalSource.UNMET_NEED])
    with pytest.raises(ValueError, match="raise"):
        asyncio.run(
            execute_discovery(
                services(sessions, DiscoveryLLM(), SnippetSearch()), run_id, budget_ceiling_usd=5
            )
        )
