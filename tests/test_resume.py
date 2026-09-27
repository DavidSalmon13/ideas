"""Phase 3 acceptance: leads, claims and sources persist; no lead without a source;
killing a discovery run mid-way and resuming it continues from the agents' ledgers."""

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.cli.discover import DiscoveryServices, ResumeError, resume_discovery, run_discovery
from opp.cli.main import main
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    ClaimRepository,
    LeadRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
    TraceRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import AgentRunStatus, ClaimType, RunStatus, SignalSource
from opp.domain.run import AgentRun, DiscoveryScope, LeadTargets, RunConfig
from opp.llm.factory import build_gateway
from opp.llm.types import Completion, CompletionRequest
from opp.observability.traces import DbTraceSink
from tests.agents.fakes import DiscoveryLLM, SnippetSearch

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNALS = [SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS]
TARGET = 4  # 2 per signal source; the fake LLM yields one sourced lead per query
CONFIG = RunConfig(
    discovery_scope=DiscoveryScope(industry="HVAC"), lead_targets=LeadTargets(discovery_n=TARGET)
)


class Killed(BaseException):
    """Stands in for the process dying: not an ``Exception``, so nothing catches it."""


class DyingLLM(DiscoveryLLM):
    """Answers normally until call number ``die_on``, then the 'process' dies."""

    def __init__(self, die_on: int) -> None:
        super().__init__()
        self.die_on = die_on

    async def complete(self, request: CompletionRequest) -> Completion:
        if len(self.requests) + 1 == self.die_on:
            raise Killed
        return await super().complete(request)


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_db_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def services(sessions: sessionmaker[Session], llm: object, search: object) -> DiscoveryServices:
    sink = DbTraceSink(sessions)
    return DiscoveryServices(
        sessions=sessions,
        gateway=build_gateway(get_settings(), sink, provider=llm),  # type: ignore[arg-type]
        search=search,  # type: ignore[arg-type]
        sink=sink,
        budget=ResearchBudget(max_cost_usd=0.75, max_searches=8, max_llm_calls=10, max_fetches=0),
        search_cost_estimate_usd=0.008,
    )


def only_run(sessions: sessionmaker[Session]) -> UUID:
    with sessions() as session:
        [run] = RunRepository(session).list_resumable()
    return run.id


def agent_runs(sessions: sessionmaker[Session], run_id: UUID) -> list[AgentRun]:
    with sessions() as session:
        [stage] = StageRepository(session).list_for_run(run_id)
        return AgentRunRepository(session).list_for_stage(stage.id)


def assert_every_lead_is_sourced(sessions: sessionmaker[Session], run_id: UUID) -> int:
    """No lead without a source, and every lead claim is in the claims index."""
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run_id)
        sources = SourceRepository(session).get_many({s for lead in leads for s in lead.source_ids})
        claims = ClaimRepository(session)
        for lead in leads:
            assert lead.source_ids and lead.source_ids <= set(sources)
            assert any(c.type is ClaimType.FACT for c in lead.claims())
            assert {c.id for c in claims.list_for_lead(lead.id)} == {c.id for c in lead.claims()}
    assert len({lead.title for lead in leads}) == len(leads)  # nothing committed twice
    return len(leads)


def test_resume_after_an_interruption_continues_from_the_ledgers(
    sessions: sessionmaker[Session],
) -> None:
    with pytest.raises(Killed):
        asyncio.run(
            run_discovery(services(sessions, DyingLLM(3), SnippetSearch()), CONFIG, SIGNALS)
        )

    run_id = only_run(sessions)
    with sessions() as session:
        interrupted = RunRepository(session).get(run_id)
        spent_before = TraceRepository(session).total_cost_for_run(run_id)
    assert interrupted is not None and interrupted.status is RunStatus.DISCOVERING
    before = agent_runs(sessions, run_id)
    assert any(ar.status is AgentRunStatus.RUNNING for ar in before)
    done_before = {ar.agent_name: list(ar.work_ledger.done) for ar in before}
    leads_before = assert_every_lead_is_sourced(sessions, run_id)
    assert 0 < leads_before < TARGET

    search = SnippetSearch()
    outcome = asyncio.run(resume_discovery(services(sessions, DiscoveryLLM(), search), run_id))

    assert outcome.run_id == run_id
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert assert_every_lead_is_sourced(sessions, run_id) == TARGET
    # Only the missing work was redone: one search per lead still owed.
    assert len(search.calls) == TARGET - leads_before
    for ar in agent_runs(sessions, run_id):
        assert ar.status is AgentRunStatus.COMPLETED
        assert ar.work_ledger.done[: len(done_before[ar.agent_name])] == done_before[ar.agent_name]
    # The spend of the interrupted run is carried over, not forgotten.
    assert outcome.cost_usd > spent_before > 0


def test_resume_after_the_process_is_killed(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    """The real thing: SIGKILL a discovery process mid-run, then resume the run."""
    script = textwrap.dedent(
        """
        import asyncio, os, signal
        from tests.agents.fakes import DiscoveryLLM, SnippetSearch
        from tests.test_resume import CONFIG, SIGNALS, services
        from opp.cli.discover import run_discovery
        from opp.config.settings import get_settings
        from opp.db.session import create_db_engine, create_session_factory

        class SigKilledLLM(DiscoveryLLM):
            async def complete(self, request):
                if len(self.requests) == 2:
                    os.kill(os.getpid(), signal.SIGKILL)
                return await super().complete(request)

        sessions = create_session_factory(create_db_engine(get_settings().database_url))
        fakes = services(sessions, SigKilledLLM(), SnippetSearch())
        asyncio.run(run_discovery(fakes, CONFIG, SIGNALS))
        """
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
        "OPP_DATABASE_URL": get_settings().database_url,
    }
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, timeout=60
    )
    assert proc.returncode == -9, proc.stderr.decode()

    run_id = only_run(sessions)
    leads_before = assert_every_lead_is_sourced(sessions, run_id)
    assert 0 < leads_before < TARGET

    search = SnippetSearch()
    outcome = asyncio.run(resume_discovery(services(sessions, DiscoveryLLM(), search), run_id))

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert assert_every_lead_is_sourced(sessions, run_id) == TARGET
    assert len(search.calls) == TARGET - leads_before


def test_a_budget_halted_run_resumes_after_a_top_up(sessions: sessionmaker[Session]) -> None:
    tight = CONFIG.model_copy(update={"budget_ceiling_usd": 0.04})
    halted = asyncio.run(
        run_discovery(services(sessions, DiscoveryLLM(), SnippetSearch()), tight, SIGNALS)
    )
    assert halted.status is RunStatus.HALTED_BUDGET
    leads_before = assert_every_lead_is_sourced(sessions, halted.run_id)
    assert leads_before < TARGET

    with pytest.raises(ResumeError, match="leaves nothing"):
        asyncio.run(
            resume_discovery(
                services(sessions, DiscoveryLLM(), SnippetSearch()),
                halted.run_id,
                budget_ceiling_usd=0.01,
            )
        )

    outcome = asyncio.run(
        resume_discovery(
            services(sessions, DiscoveryLLM(), SnippetSearch()),
            halted.run_id,
            budget_ceiling_usd=20.0,
        )
    )
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert assert_every_lead_is_sourced(sessions, halted.run_id) == TARGET
    with sessions() as session:
        run = RunRepository(session).get(halted.run_id)
    assert run is not None and run.budget_ceiling_usd == 20.0


def test_finished_or_unknown_runs_are_not_resumed(sessions: sessionmaker[Session]) -> None:
    done = asyncio.run(
        run_discovery(services(sessions, DiscoveryLLM(), SnippetSearch()), CONFIG, SIGNALS)
    )
    for run_id, message in ((done.run_id, "already ended"), (UUID(int=1), "no run")):
        with pytest.raises(ResumeError, match=message):
            asyncio.run(
                resume_discovery(services(sessions, DiscoveryLLM(), SnippetSearch()), run_id)
            )


def test_cli_resume(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(Killed):
        asyncio.run(
            run_discovery(services(sessions, DyingLLM(2), SnippetSearch()), CONFIG, SIGNALS)
        )
    run_id = only_run(sessions)
    monkeypatch.setattr("opp.cli.main._providers", lambda *_: (DiscoveryLLM(), SnippetSearch()))

    assert main(["resume", str(run_id)]) == 0
    out = capsys.readouterr().out
    assert f"{TARGET} lead(s)" in out
    assert "Run completed_partial." in out

    assert main(["resume", str(run_id)]) == 2
    assert "already ended" in capsys.readouterr().err
