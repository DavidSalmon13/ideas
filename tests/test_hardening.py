"""Phase 9 hardening (plan.md §24: "SSRF/injection review passed; restart-during-run soak
resumes correctly"). The review record is ``docs/security-review.md``; these are its
executable checks that aren't already covered elsewhere.

- **Restart/resume soak** — kill a full autonomous funnel across a spread of points and
  resume each with a fresh orchestrator (a new process). Every restart must reach the same
  verdict, under budget, with no lead lost or committed twice and exactly one decision.
  Existing resume tests (``test_resume.py``) only interrupt discovery; this spans the funnel.
- **Prompt injection** — untrusted search content carrying an instruction must reach the
  model only inside a delimited DATA block, with its closing delimiter neutralised, and must
  not change what the agent produces. (SSRF is covered by ``research/test_fetcher.py``; the
  DATA-block helper by ``test_prompts.py``; tool allow-listing by ``agents/test_runtime.py``.)
"""

import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import (
    LeadRepository,
    OpportunityRepository,
    ReportRepository,
    RunRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import CheckpointMode, RunStatus, SignalSource, StageName
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import DiscoveryStage, StageServices
from opp.prompts import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from tests.agents.fakes import DiscoveryLLM, FullFunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING
from tests.test_resume import Killed

AUTONOMOUS = RunConfig(
    discovery_scope=DiscoveryScope(
        industry="HVAC",
        signal_sources=[SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS],
    ),
    lead_targets=LeadTargets(discovery_n=6),
    checkpoint_mode=CheckpointMode.AUTONOMOUS,
)


class DyingFunnelLLM(FullFunnelLLM):
    """A full-funnel LLM that dies (like the process being killed) on the ``die_on``-th call."""

    def __init__(self, die_on: int) -> None:
        super().__init__()
        self._die_on = die_on
        self._calls = 0

    async def complete(self, request):  # type: ignore[no-untyped-def]
        self._calls += 1
        if self._calls == self._die_on:
            raise Killed
        return await super().complete(request)


def _services(sessions: sessionmaker[Session], llm: object, search: object) -> StageServices:
    sink = DbTraceSink(sessions)
    return StageServices(
        sessions=sessions,
        gateway=LLMGateway(llm, ROUTING, sink),  # type: ignore[arg-type]
        search=search,  # type: ignore[arg-type]
        sink=sink,
        search_cost_estimate_usd=0.008,
        discovery_budget=ResearchBudget(),
    )


def _fresh_db(tmp_path: Path, name: str) -> tuple[sessionmaker[Session], object]:
    engine = create_db_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine), engine


def _decision(sessions: sessionmaker[Session], run_id: UUID) -> tuple[str, int]:
    """The run's verdict and its lead count, after checking the resume invariants: a judged
    selected opportunity, exactly one decision report, and no lead committed twice."""
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        assert run is not None and run.selected_opportunity_id is not None
        opportunity = OpportunityRepository(session).get(run.selected_opportunity_id)
        assert opportunity is not None and opportunity.evaluation is not None
        leads = LeadRepository(session).list_for_run(run_id)
        # No sub-task committed the same lead twice across the restart (unique by title).
        assert len({lead.title for lead in leads}) == len(leads), "a lead was committed twice"
        # Every lead is still sourced — a restart never fabricates one.
        assert all(lead.source_ids for lead in leads)
        # Exactly one completed (non-incomplete) decision report was emitted.
        reports = [r for r in ReportRepository(session).list_for_run(run_id) if not r.incomplete]
        assert len(reports) == 1, f"expected one decision report, got {len(reports)}"
    return opportunity.evaluation.verdict.value, len(leads)


def test_restart_resume_soak(tmp_path: Path) -> None:
    """Interrupt the full funnel across the stages and resume each restart to the same decision."""
    ref_sessions, ref_engine = _fresh_db(tmp_path, "reference.db")
    ref_llm = FullFunnelLLM()
    reference = asyncio.run(
        Orchestrator(_services(ref_sessions, ref_llm, SnippetSearch())).start(AUTONOMOUS)
    )
    assert reference.status is RunStatus.COMPLETED
    total_calls = len(ref_llm.requests)
    assert total_calls >= 8  # the funnel really does span many calls across the stages
    want_verdict, want_lead_count = _decision(ref_sessions, reference.run_id)
    ref_engine.dispose()  # type: ignore[attr-defined]

    # Interrupt at a spread of points across the funnel (deep research alone is ~12 near
    # identical calls, so hitting every one adds no coverage): an even sample plus the tail,
    # where the single-call stages — synthesis, strategy, red-team, judge — live.
    step = max(1, total_calls // 8)
    die_points = sorted(
        set(range(1, total_calls + 1, step)) | set(range(total_calls - 3, total_calls + 1))
    )

    interrupted_stages: set[StageName] = set()
    for die_on in die_points:
        sessions, engine = _fresh_db(tmp_path, f"soak_{die_on}.db")

        # Start a run that dies on the die_on-th LLM call (the process being killed mid-stage).
        with pytest.raises(Killed):
            asyncio.run(
                Orchestrator(_services(sessions, DyingFunnelLLM(die_on), SnippetSearch())).start(
                    AUTONOMOUS
                )
            )
        with sessions() as session:
            [resumable] = RunRepository(session).list_resumable()
            interrupted_stages.add(resumable.current_stage or StageName.DISCOVERY)
        run_id = resumable.id

        # A *fresh* orchestrator (a new process, healthy LLM) resumes it to a verdict.
        outcome = asyncio.run(
            Orchestrator(_services(sessions, FullFunnelLLM(), SnippetSearch())).resume(run_id)
        )
        assert outcome.status is RunStatus.COMPLETED, f"died on call {die_on}"
        assert outcome.cost_usd <= AUTONOMOUS.budget_ceiling_usd, f"over budget after call {die_on}"
        got_verdict, got_lead_count = _decision(sessions, run_id)
        assert got_verdict == want_verdict, f"verdict changed after a restart on call {die_on}"
        assert got_lead_count == want_lead_count, (
            f"a lead was lost after a restart on call {die_on}"
        )
        engine.dispose()  # type: ignore[attr-defined]

    # The soak really did interrupt across the funnel, not just one stage.
    assert len(interrupted_stages) >= 4


def test_untrusted_search_content_cannot_hijack_an_agent(tmp_path: Path) -> None:
    """The injecting snippet reaches the prompt only inside a neutralised DATA block, and the
    agent's output is unaffected (every surviving lead is still properly sourced)."""
    sessions, engine = _fresh_db(tmp_path, "injection.db")
    search = SnippetSearch()  # its third result per query is a prompt-injection attempt
    llm = DiscoveryLLM()
    services = _services(sessions, llm, search)
    outcome = asyncio.run(
        Orchestrator(services, {StageName.DISCOVERY: DiscoveryStage(services)}).start(AUTONOMOUS)
    )
    assert outcome.status is RunStatus.COMPLETED_PARTIAL

    injected = [
        req.messages[-1].content
        for req in llm.requests
        if "IGNORE PREVIOUS INSTRUCTIONS" in req.messages[-1].content
    ]
    assert injected, "the injection payload should have reached discovery's prompt"
    for prompt in injected:
        start = prompt.index(UNTRUSTED_OPEN)
        end = prompt.index(UNTRUSTED_CLOSE, start + len(UNTRUSTED_OPEN))
        payload_at = prompt.index("IGNORE PREVIOUS INSTRUCTIONS")
        # The instruction sits inside the DATA block...
        assert start < payload_at < end
        # ...and its attempt to close the block early was neutralised (no real DATA>>> in it).
        assert "DATA>>>" not in prompt[start + len(UNTRUSTED_OPEN) : end]

    # The agent was not steered: every lead that survived is sourced, none fabricated.
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(outcome.run_id)
    assert leads and all(lead.source_ids for lead in leads)
    engine.dispose()  # type: ignore[attr-defined]
