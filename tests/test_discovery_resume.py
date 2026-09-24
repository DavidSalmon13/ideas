"""A discovery run killed mid-way (or halted on budget) continues from its ledgers through
the orchestrator, and every lead it ends with has a source."""

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.discovery import DiscoveryAgent
from opp.cli import main as cli
from opp.cli.pipeline import build_orchestrator
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    LeadRepository,
    SourceRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    AgentRunStatus,
    CheckpointMode,
    RunStatus,
    SignalSource,
    StageName,
    StageStatus,
)
from opp.domain.run import AgentRun, DiscoveryScope, LeadTargets, ResearchRun, RunConfig
from opp.orchestration.engine import Orchestrator, RunNotFoundError, RunStateError
from opp.research.search import Freshness, SearchResponse
from tests.agents.fakes import FunnelLLM, PageStub, SnippetSearch
from tests.test_cli import services

SIGNAL = SignalSource.UNMET_NEED
# Autonomous: these tests are about resuming, so the checkpoint picks rank 1 itself.
CONFIG = RunConfig(
    discovery_scope=DiscoveryScope(industry="HVAC", signal_sources=[SIGNAL]),
    lead_targets=LeadTargets(discovery_n=8),
    checkpoint_mode=CheckpointMode.AUTONOMOUS,
)
TIGHT = CONFIG.model_copy(update={"budget_ceiling_usd": 0.02})


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


def orchestrator(
    sessions: sessionmaker[Session], search: SnippetSearch | None = None
) -> Orchestrator:
    return build_orchestrator(services(sessions, FunnelLLM(), search or SnippetSearch()))


def agent_run(sessions: sessionmaker[Session], run_id: UUID) -> AgentRun:
    """The discovery agent run."""
    with sessions() as session:
        stages = StageRepository(session).list_for_run(run_id)
        [stage] = [s for s in stages if s.name is StageName.DISCOVERY]
        [found] = AgentRunRepository(session).list_for_stage(stage.id)
    return found


def sourced_leads(sessions: sessionmaker[Session], run_id: UUID) -> int:
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run_id)
        sources = SourceRepository(session).get_many({s for le in leads for s in le.source_ids})
    assert leads
    for lead in leads:
        assert lead.source_ids and lead.source_ids <= set(sources)
    return len(leads)


def halted_run(sessions: sessionmaker[Session]) -> ResearchRun:
    orch = orchestrator(sessions)
    run = asyncio.run(orch.execute(orch.create_run(TIGHT)))
    assert run.status is RunStatus.HALTED_BUDGET
    return run


def test_killed_run_resumes_from_the_ledger(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, DyingSearch(die_on_call=3))
    run_id = orch.create_run(CONFIG)
    with pytest.raises(Killed):
        asyncio.run(orch.execute(run_id))

    # What the dead process left behind: two committed sub-tasks, their leads, a live run.
    interrupted = agent_run(sessions, run_id)
    assert interrupted.status is AgentRunStatus.RUNNING
    assert interrupted.work_ledger.done == ["query-0", "query-1"]
    assert sourced_leads(sessions, run_id) == 4  # 2 per query: the real lead + the decoy
    before = orch.get_run(run_id)
    assert before.status is RunStatus.DISCOVERING

    search = SnippetSearch()
    run = asyncio.run(orchestrator(sessions, search).resume(run_id))

    queries = DiscoveryAgent(SIGNAL, target_leads=8).queries(CONFIG)
    # Discovery repeats nothing and stops at its target; then shallow validation searches.
    assert search.calls[:2] == queries[2:4]
    assert all(call not in queries for call in search.calls[2:])
    assert run.status is RunStatus.COMPLETED_PARTIAL
    assert sourced_leads(sessions, run_id) == 8
    finished = agent_run(sessions, run_id)
    assert finished.status is AgentRunStatus.COMPLETED
    assert finished.work_ledger.pending == []
    assert finished.output_counts["leads"] == 8
    assert run.cost_to_date_usd > before.cost_to_date_usd
    with sessions() as session:
        stages = {s.name: s for s in StageRepository(session).list_for_run(run_id)}
    assert set(stages) == {
        StageName.DISCOVERY,
        StageName.SCREENING,
        StageName.SHALLOW_VALIDATION,
        StageName.CHECKPOINT,
    }
    assert all(s.status is StageStatus.COMPLETED and s.attempt == 1 for s in stages.values())
    assert sum(s.cost_usd for s in stages.values()) == pytest.approx(run.cost_to_date_usd)


def test_recovery_scan_continues_a_killed_run(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, DyingSearch(die_on_call=2))
    run_id = orch.create_run(CONFIG)
    with pytest.raises(Killed):
        asyncio.run(orch.execute(run_id))
    halted = halted_run(sessions)

    report = asyncio.run(orchestrator(sessions).recover())

    assert [r.id for r in report.resumed] == [run_id]
    assert report.resumed[0].status is RunStatus.COMPLETED_PARTIAL
    assert [r.id for r in report.waiting] == [halted.id]  # budget halts wait for a top-up
    assert sourced_leads(sessions, run_id) == 8


def test_budget_halt_resumes_after_a_top_up(sessions: sessionmaker[Session]) -> None:
    halted = halted_run(sessions)
    # The pre-check stops it before discovery spends anything: 1 source x $0.75 cap.
    assert halted.current_stage is StageName.DISCOVERY
    assert halted.status_reason == (
        "the discovery stage may need up to $0.75; $0.02 of the $0.02 budget is left"
    )
    assert halted.cost_to_date_usd == 0

    # Resuming without more money just halts again; nothing is lost.
    again = asyncio.run(orchestrator(sessions).resume(halted.id))
    assert again.status is RunStatus.HALTED_BUDGET

    topped = asyncio.run(orchestrator(sessions).resume(halted.id, budget_ceiling_usd=20.0))
    assert topped.status is RunStatus.COMPLETED_PARTIAL
    assert topped.status_reason == "the pipeline currently ends before the deep_research stage"
    assert topped.selected_opportunity_id is not None  # autonomous: rank 1 was selected
    assert topped.budget_ceiling_usd == 20.0
    assert sourced_leads(sessions, halted.id) == 8


def test_only_unfinished_runs_resume(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions)
    done = asyncio.run(orch.execute(orch.create_run(CONFIG)))
    with pytest.raises(RunStateError, match="completed_partial"):
        asyncio.run(orch.resume(done.id))
    with pytest.raises(RunNotFoundError, match="No run"):
        asyncio.run(orch.resume(uuid4()))


def test_cli_resume_recover_and_cancel(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_providers", lambda *_: (FunnelLLM(), SnippetSearch(), PageStub()))
    halted = halted_run(sessions)

    assert cli.main(["recover"]) == 0
    out = capsys.readouterr().out
    assert f"Waiting: run {halted.id} is halted_budget" in out
    assert "Recovered 0 run(s); 1 waiting." in out

    assert cli.main(["resume", str(halted.id), "--budget", "20"]) == 0
    captured = capsys.readouterr()
    assert f"Resuming run {halted.id}" in captured.err
    assert "8 lead(s)" in captured.out
    assert "of $20.00" in captured.out

    assert cli.main(["resume", str(halted.id)]) == 1
    assert "nothing to resume" in capsys.readouterr().err

    again = halted_run(sessions)
    assert cli.main(["cancel", str(again.id)]) == 0
    assert f"Run {again.id}: cancelled" in capsys.readouterr().out
    assert cli.main(["cancel", str(again.id)]) == 1
