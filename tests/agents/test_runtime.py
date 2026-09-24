import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.base import Agent, AgentContext, AgentResult, SubTaskResult
from opp.agents.budget import CallKind, ResearchBudget, RunBudget
from opp.agents.memory import AgentMemory
from opp.agents.runtime import AgentRuntime
from opp.db import models
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    ClaimRepository,
    LeadRepository,
    RunRepository,
    StageRepository,
    TraceRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    AgentRunStatus,
    BusinessShape,
    ClaimType,
    Level,
    ModelTier,
    SignalSource,
    SourceType,
    StageName,
)
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.run import AgentRun, ResearchRun, Stage
from opp.llm.fake import FakeLLMProvider, reply
from opp.llm.gateway import LLMGateway, ModelRoute, ModelRouting
from opp.llm.types import CompletionRequest
from opp.observability.traces import DbTraceSink
from opp.research.fake import FakeSearchProvider
from opp.research.search import SearchResult

ROUTING = ModelRouting(
    cheap=ModelRoute(primary="claude-haiku-4-5"),
    medium=ModelRoute(primary="claude-sonnet-5"),
    premium=ModelRoute(primary="claude-sonnet-5"),
    premium_opus=ModelRoute(primary="claude-opus-4-8"),
)
BIG = ResearchBudget(max_cost_usd=5.0)


class Point(BaseModel):
    point: str


class Crash(BaseException):
    """Stands in for the process being killed mid-sub-task."""


class ToyAgent(Agent):
    """Each sub-task: search the task, cite the first hit, ask the LLM for the point."""

    name: ClassVar[str] = "toy"
    default_tier: ClassVar[ModelTier] = ModelTier.CHEAP
    prompt_version: ClassVar[str] = "toy-v1"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM, CallKind.SEARCH})

    def __init__(
        self,
        tasks: list[str] | None = None,
        *,
        crash_on: str | None = None,
        follow_ups: dict[str, list[str]] | None = None,
        fetch_on: str | None = None,
        unsourced_on: str | None = None,
        boom_on: str | None = None,
    ) -> None:
        self.tasks = tasks or ["q1", "q2", "q3"]
        self.crash_on = crash_on
        self.follow_ups = follow_ups or {}
        self.fetch_on = fetch_on
        self.unsourced_on = unsourced_on
        self.boom_on = boom_on
        self.ran: list[str] = []
        self.prompts: list[str] = []

    def plan(self, ctx: AgentContext) -> list[str]:
        return list(self.tasks)

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        self.ran.append(task)
        if task == self.crash_on:
            raise Crash
        if task == self.boom_on:
            raise RuntimeError("bug in agent")
        if task == self.fetch_on:
            await ctx.tools.fetch("https://example.com/")  # not a declared tool
        hits = await ctx.tools.search(task, k=1)
        prompt = f"{ctx.memory.render()}\n\nTask: {task}"
        self.prompts.append(prompt)
        result = await ctx.tools.llm(prompt=prompt, output_type=Point, max_tokens=1000)
        # A fresh Source object every time: the runtime must dedupe it by URL.
        source = Source(
            url=hits.results[0].url,
            title="hit",
            source_type=SourceType.TRADE_PRESS,
            credibility_tier=Level.MEDIUM,
            excerpt=hits.results[0].snippet,
        )
        claim = Claim(
            statement=result.output.point,
            type=ClaimType.FACT,
            source_ids=[] if task == self.unsourced_on else [source.id],
            confidence=Level.MEDIUM,
            confidence_reason="stated by the source",
        )
        return SubTaskResult(
            sources=[source],
            claims=[claim],
            note=f"{task}: {result.output.point}",
            follow_up_tasks=self.follow_ups.get(task, []),
        )


class OverridingCompactor(ToyAgent):
    async def compact(self, memory: AgentMemory, ctx: AgentContext) -> str:
        return f"COMPACTED({len(memory.notes)} notes)"


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def make_agent_run(sessions: sessionmaker[Session]) -> tuple[UUID, UUID]:
    run = ResearchRun()
    stage = Stage(run_id=run.id, name=StageName.DISCOVERY)
    agent_run = AgentRun(stage_id=stage.id, agent_name="toy", model_tier=ModelTier.CHEAP)
    with sessions() as session:
        RunRepository(session).add(run)
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        session.commit()
    return run.id, agent_run.id


def points(n: int) -> list[Any]:
    return [reply(json.dumps({"point": f"point {i}"})) for i in range(1, n + 1)]


def runtime(
    sessions: sessionmaker[Session],
    llm_script: list[Any],
    *,
    threshold: int = 8_000,
) -> tuple[AgentRuntime, FakeLLMProvider, FakeSearchProvider]:
    llm = FakeLLMProvider(script=llm_script)
    search = FakeSearchProvider(
        default=[
            SearchResult(url="https://www.achrnews.com/a", snippet="HVAC shops book by phone")
        ],
        cost_per_call_usd=0.01,
    )
    sink = DbTraceSink(sessions)
    rt = AgentRuntime(
        sessions,
        gateway=LLMGateway(llm, ROUTING, sink),
        sink=sink,
        search=search,
        search_cost_estimate_usd=0.01,
        compaction_threshold_tokens=threshold,
    )
    return rt, llm, search


def go(
    rt: AgentRuntime,
    agent: Agent,
    agent_run_id: UUID,
    *,
    budget: ResearchBudget = BIG,
    run_budget: RunBudget | None = None,
    **kwargs: Any,
) -> AgentResult:
    return asyncio.run(
        rt.run(
            agent, agent_run_id, budget=budget, run_budget=run_budget or RunBudget(20.0), **kwargs
        )
    )


def load(sessions: sessionmaker[Session], agent_run_id: UUID) -> AgentRun:
    with sessions() as session:
        found = AgentRunRepository(session).get(agent_run_id)
    assert found is not None
    return found


def count(sessions: sessionmaker[Session], table: Any) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(table)) or 0


def test_runs_all_subtasks_and_commits_evidence_and_cost(sessions: sessionmaker[Session]) -> None:
    run_id, agent_run_id = make_agent_run(sessions)
    rt, _, search = runtime(sessions, points(3))
    agent = ToyAgent()

    result = go(rt, agent, agent_run_id)

    assert result.status is AgentRunStatus.COMPLETED
    assert (result.tasks_done, result.tasks_failed, result.tasks_pending) == (3, 0, 0)
    assert result.claims_saved == 3
    stored = load(sessions, agent_run_id)
    assert stored.work_ledger.done == ["q1", "q2", "q3"]
    assert (stored.prompt_version, stored.started_at is not None, stored.ended_at is not None) == (
        "toy-v1",
        True,
        True,
    )
    assert "q3: point 3" in stored.summary
    assert stored.tokens_in == 300 and stored.tokens_out == 150

    # One source row despite three fresh Source objects for the same URL; claims cite it.
    assert count(sessions, models.SourceRow) == 1
    assert count(sessions, models.ClaimSourceRow) == 3

    with sessions() as session:
        traces = TraceRepository(session)
        assert len(traces.list_for_run(run_id)) == 6  # 3 searches + 3 LLM calls
        total = traces.total_cost_for_run(run_id)
        run = RunRepository(session).get(run_id)
    assert result.cost_usd == pytest.approx(total)
    assert run is not None and run.cost_to_date_usd == pytest.approx(total)
    assert len(search.calls) == 3


def test_prompts_carry_the_running_memory(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(3))
    agent = ToyAgent()
    go(rt, agent, agent_run_id)
    assert "q1: point 1" in agent.prompts[1]
    assert "q2: point 2" in agent.prompts[2]


def test_resume_after_a_crash_continues_from_the_ledger(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(1))
    with pytest.raises(Crash):
        go(rt, ToyAgent(crash_on="q2"), agent_run_id)

    interrupted = load(sessions, agent_run_id)
    assert interrupted.status is AgentRunStatus.RUNNING
    assert interrupted.work_ledger.done == ["q1"]
    assert interrupted.work_ledger.pending == ["q2", "q3"]
    assert count(sessions, models.ClaimRow) == 1  # q1's claim survived the crash

    rt2, _, _ = runtime(sessions, points(2))
    agent = ToyAgent()
    result = go(rt2, agent, agent_run_id)

    assert agent.ran == ["q2", "q3"]  # q1 is not redone
    assert "q1: point 1" in agent.prompts[0]  # rehydrated from the persisted summary
    assert result.status is AgentRunStatus.COMPLETED
    assert load(sessions, agent_run_id).work_ledger.done == ["q1", "q2", "q3"]
    assert count(sessions, models.ClaimRow) == 3


def test_agent_research_budget_ends_the_agent_with_what_it_has(
    sessions: sessionmaker[Session],
) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, llm, _ = runtime(sessions, points(3))

    done = go(rt, ToyAgent(), agent_run_id, budget=ResearchBudget(max_cost_usd=5, max_llm_calls=1))

    assert done.status is AgentRunStatus.DEGRADED and not done.budget_exhausted
    assert "research budget exhausted" in (done.error or "") and "llm limit" in (done.error or "")
    assert (done.tasks_done, done.tasks_pending) == (1, 2)
    assert len(llm.requests) == 1  # the blocked call was never made
    assert load(sessions, agent_run_id).ended_at is not None

    agent = ToyAgent()
    again = go(rt, agent, agent_run_id, budget=ResearchBudget(max_cost_usd=5, max_llm_calls=10))
    assert again.status is AgentRunStatus.DEGRADED and agent.ran == []  # final


def test_run_budget_halt_resumes_after_top_up(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(3))

    halted = go(rt, ToyAgent(), agent_run_id, run_budget=RunBudget(ceiling_usd=0.012))
    assert halted.status is AgentRunStatus.HALTED_BUDGET and halted.budget_exhausted
    assert load(sessions, agent_run_id).ended_at is None

    agent = ToyAgent()
    resumed = go(rt, agent, agent_run_id, run_budget=RunBudget(ceiling_usd=20))
    assert resumed.status is AgentRunStatus.COMPLETED
    assert agent.ran == ["q1", "q2", "q3"]  # q1 never committed: the LLM call was refused


def test_run_budget_blocks_calls_it_cannot_afford(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, llm, search = runtime(sessions, points(3))

    result = go(rt, ToyAgent(), agent_run_id, run_budget=RunBudget(ceiling_usd=0.012))

    # The search fits ($0.01), the LLM pre-flight estimate doesn't.
    assert result.status is AgentRunStatus.HALTED_BUDGET
    assert "run budget" in (result.error or "")
    assert len(search.calls) == 1 and llm.requests == []


def test_failed_subtask_degrades_but_the_agent_carries_on(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    # q1 ok; q2 invalid three times (1 call + 2 re-asks); q3 ok.
    rt, _, _ = runtime(sessions, [*points(1), reply("{}"), reply("{}"), reply("{}"), *points(1)])

    result = go(rt, ToyAgent(), agent_run_id)

    assert result.status is AgentRunStatus.DEGRADED
    assert (result.tasks_done, result.tasks_failed) == (2, 1)
    stored = load(sessions, agent_run_id)
    assert stored.work_ledger.failed == ["q2"]
    assert "sub-task q2 failed" in stored.summary
    assert result.cost_usd > 0  # the failed attempts were still charged


def test_unexpected_errors_fail_the_agent_run_without_raising(
    sessions: sessionmaker[Session],
) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(3))

    result = go(rt, ToyAgent(boom_on="q2"), agent_run_id)

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "RuntimeError: bug in agent"
    assert load(sessions, agent_run_id).work_ledger.done == ["q1"]


def test_undeclared_tools_are_refused(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(3))
    result = go(rt, ToyAgent(fetch_on="q1"), agent_run_id)
    assert result.status is AgentRunStatus.FAILED
    assert "may not use fetch" in (result.error or "")


def test_compaction_folds_memory_and_persists_the_summary(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, llm, _ = runtime(sessions, points(3), threshold=5)
    agent = OverridingCompactor()

    result = go(rt, agent, agent_run_id)

    assert result.status is AgentRunStatus.COMPLETED
    assert result.compactions >= 2
    # After compaction the next prompt starts from the compact state, not raw notes.
    assert "COMPACTED(1 notes)" in agent.prompts[1]
    assert "q1: point 1" not in agent.prompts[1]
    assert load(sessions, agent_run_id).summary.startswith("COMPACTED")
    assert all(isinstance(r, CompletionRequest) for r in llm.requests)


def test_unsourced_facts_are_demoted_on_commit(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(3))

    result = go(rt, ToyAgent(unsourced_on="q2"), agent_run_id)

    assert result.demoted_claims == 1
    with sessions() as session:
        claims = ClaimRepository(session).get_many(
            [row.id for row in session.scalars(select(models.ClaimRow))]
        )
    assert sorted(c.type.value for c in claims.values()) == ["assumption", "fact", "fact"]


def test_follow_up_tasks_are_added_once(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(4))
    agent = ToyAgent(["q1", "q2"], follow_ups={"q1": ["q3", "q2", "q3"]})

    go(rt, agent, agent_run_id)

    assert agent.ran == ["q1", "q2", "q3"]


def test_cancellation_is_checked_between_subtasks(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(3))
    agent = ToyAgent()

    result = go(rt, agent, agent_run_id, should_stop=lambda: len(agent.ran) >= 1)

    assert result.status is AgentRunStatus.CANCELLED
    assert (result.tasks_done, result.tasks_pending) == (1, 2)


def test_finished_agent_runs_are_not_rerun(sessions: sessionmaker[Session]) -> None:
    _, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, points(3))
    go(rt, ToyAgent(), agent_run_id)

    agent = ToyAgent()
    again = go(rt, agent, agent_run_id)

    assert again.status is AgentRunStatus.COMPLETED
    assert agent.ran == []


class LeadAgent(Agent):
    """One sub-task yielding a sourced lead and a lead whose only citation was never
    handed to the runtime (so it resolves to nothing once stored)."""

    name: ClassVar[str] = "leads"
    default_tier: ClassVar[ModelTier] = ModelTier.CHEAP

    def plan(self, ctx: AgentContext) -> list[str]:
        return ["only"]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        kept = Source(
            url="https://www.reddit.com/r/HVAC/comments/1",
            title="thread",
            source_type=SourceType.FORUM,
            credibility_tier=Level.LOW,
            excerpt="dispatching by phone wastes hours",
        )
        ghost = kept.model_copy(update={"id": uuid4(), "url": "https://ghost.example/"})

        def lead(title: str, source: Source) -> Lead:
            def claim(text: str) -> Claim:
                return Claim(
                    statement=text,
                    type=ClaimType.FACT,
                    source_ids=[source.id],
                    confidence=Level.MEDIUM,
                    confidence_reason="stated in the thread",
                )

            return Lead(
                run_id=ctx.run_id,
                title=title,
                business_shape=BusinessShape.SAAS,
                named_customer=claim("HVAC shops with 2-5 trucks"),
                specific_pain=claim("dispatching by phone"),
                why_now=claim("new refrigerant rules"),
                signal_source=SignalSource.UNMET_NEED,
            )

        return SubTaskResult(sources=[kept], leads=[lead("real", kept), lead("ghost", ghost)])


def test_a_lead_without_a_stored_source_is_dropped_on_commit(
    sessions: sessionmaker[Session],
) -> None:
    run_id, agent_run_id = make_agent_run(sessions)
    rt, _, _ = runtime(sessions, [])

    result = go(rt, LeadAgent(), agent_run_id)

    assert result.status is AgentRunStatus.COMPLETED
    assert (result.leads_saved, result.leads_rejected) == (1, 1)
    with sessions() as session:
        [lead] = LeadRepository(session).list_for_run(run_id)
    assert lead.title == "real" and lead.source_ids
    stored = load(sessions, agent_run_id)
    assert stored.output_counts == {"leads": 1, "leads_rejected": 1, "sources": 1}
    assert "dropped unsourced lead 'ghost'" in stored.summary
