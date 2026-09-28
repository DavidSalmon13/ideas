"""Phase 4 acceptance, orchestrator side: the checkpoint waits for approve / re-pick / veto;
budget pre-checks halt a run cleanly before a stage it can't afford; every run that stops
short gets an incomplete report; the cost ledger rolls spend up from traces."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.db.base import Base
from opp.db.repositories import ReportRepository, TraceRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    BusinessShape,
    CheckpointAction,
    CheckpointMode,
    ClaimType,
    Dimension,
    EvidenceBasis,
    Level,
    RunStatus,
    StageName,
    TraceKind,
    Verdict,
)
from opp.domain.opportunity import (
    Customer,
    Opportunity,
    Problem,
    ProposedBusiness,
    ProvisionalScore,
    ShallowScore,
)
from opp.domain.report import Report, Trace
from opp.domain.run import RunConfig
from opp.evidence.store import EvidenceStore
from opp.observability.cost import cost_ledger
from opp.orchestration.budget import BudgetPreflightError
from opp.orchestration.orchestrator import CheckpointError, Orchestrator
from opp.orchestration.stages import StageContext, StageResult
from opp.reporting.partial import PartialBrief, render_brief
from tests.evidence.conftest import make_claim, make_source
from tests.orchestration.test_orchestrator import FakeStage, load, orchestrator, stages

TITLES = ["HVAC dispatch board", "Refrigerant log for techs", "Invoice chaser for plumbers"]


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'checkpoint.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def shortlist_writer(sessions: sessionmaker[Session]) -> FakeStage:
    """A shallow-validation stand-in that stores three ranked, sourced opportunities."""

    def write(ctx: StageContext) -> None:
        with sessions() as session:
            store = EvidenceStore(session)
            source = store.add_source(make_source(f"https://forum.example.com/{uuid4()}"))
            for rank, title in enumerate(TITLES, start=1):
                store.save_opportunity(
                    Opportunity(
                        run_id=ctx.run.id,
                        title=title,
                        business_shape=BusinessShape.SAAS,
                        profile_ref="bootstrap",
                        customer=Customer(
                            segment=make_claim("3-truck HVAC shops", sources=[source])
                        ),
                        problem=Problem(
                            description=make_claim("dispatch by phone", sources=[source])
                        ),
                        why_now=make_claim(
                            "a guess about new rules", ClaimType.ASSUMPTION, confidence=Level.LOW
                        ),
                        proposed_business=ProposedBusiness(wedge="phone-to-calendar board"),
                        shallow=ShallowScore(
                            dimension_scores=[
                                ProvisionalScore(
                                    dimension=Dimension.AUTOMATION,
                                    score=4,
                                    rationale="scheduling is rule-based",
                                    evidence=EvidenceBasis.INFERRED,
                                ),
                                ProvisionalScore(
                                    dimension=Dimension.DEFENSIBILITY,
                                    score=1,
                                    rationale="nothing found",
                                    evidence=EvidenceBasis.INSUFFICIENT,
                                ),
                            ],
                            composite=4.0 - rank / 2,
                            weights_ref="bootstrap",
                            rank=rank,
                        ),
                    )
                )
            session.commit()

    return FakeStage(StageName.SHALLOW_VALIDATION, during=write)


def funnel(sessions: sessionmaker[Session], **extra: FakeStage) -> list[FakeStage]:
    return [
        FakeStage(StageName.DISCOVERY),
        FakeStage(StageName.SCREENING),
        shortlist_writer(sessions),
        *extra.values(),
    ]


def at_checkpoint(
    sessions: sessionmaker[Session], orch: Orchestrator, config: RunConfig | None = None
) -> UUID:
    outcome = asyncio.run(orch.start(config or RunConfig()))
    assert outcome.status is RunStatus.AWAITING_CHECKPOINT
    return outcome.run_id


def latest_report(sessions: sessionmaker[Session], run_id: UUID) -> Report:
    with sessions() as session:
        reports = ReportRepository(session).list_for_run(run_id)
    assert reports, "expected a report"
    return max(reports, key=lambda r: r.created_at)


# --- the checkpoint ----------------------------------------------------------------------


def test_approve_sends_the_top_candidate_to_deep_research(sessions: sessionmaker[Session]) -> None:
    deep = FakeStage(StageName.DEEP_RESEARCH)
    orch = orchestrator(sessions, *funnel(sessions, deep=deep))
    run_id = at_checkpoint(sessions, orch)
    top = orch.shortlist(run_id)[0]
    assert top.title == TITLES[0]

    outcome = asyncio.run(orch.checkpoint(run_id, CheckpointAction.APPROVE))

    run = load(sessions, run_id)
    assert run.checkpoint is not None and run.checkpoint.action is CheckpointAction.APPROVE
    assert run.selected_opportunity_id == top.id
    assert len(deep.calls) == 1  # deep research ran after the approval
    assert f"checkpoint: approved '{TITLES[0]}'" in [t.reason for t in run.transitions]
    # Synthesis isn't built in these fakes, so the run stops there, honestly.
    assert outcome.status is RunStatus.COMPLETED_PARTIAL


def test_a_re_pick_needs_a_reason_and_is_recorded(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *funnel(sessions))
    run_id = at_checkpoint(sessions, orch)
    runner_up = orch.shortlist(run_id)[1]

    with pytest.raises(CheckpointError, match="one-line reason"):
        asyncio.run(orch.checkpoint(run_id, CheckpointAction.PICK, opportunity_id=runner_up.id))
    with pytest.raises(CheckpointError, match="not on this run's shortlist"):
        asyncio.run(
            orch.checkpoint(run_id, CheckpointAction.PICK, opportunity_id=uuid4(), reason="x")
        )
    assert load(sessions, run_id).status is RunStatus.AWAITING_CHECKPOINT  # nothing changed

    asyncio.run(
        orch.checkpoint(
            run_id,
            CheckpointAction.PICK,
            opportunity_id=runner_up.id,
            reason="  loved the\nautomation angle ",
        )
    )

    run = load(sessions, run_id)
    assert run.checkpoint is not None
    assert (run.checkpoint.action, run.checkpoint.reason) == (
        CheckpointAction.PICK,
        "loved the automation angle",
    )
    assert run.selected_opportunity_id == runner_up.id
    # The report of where the run stopped carries the pick, not the top-ranked one.
    brief = PartialBrief.model_validate(latest_report(sessions, run_id).brief)
    assert brief.candidate is not None and brief.candidate.opportunity_id == runner_up.id
    assert "loved the automation angle" in brief.candidate.selection


def test_picking_the_top_candidate_is_an_approval(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *funnel(sessions))
    run_id = at_checkpoint(sessions, orch)
    top = orch.shortlist(run_id)[0]

    asyncio.run(orch.checkpoint(run_id, CheckpointAction.PICK, opportunity_id=top.id))

    checkpoint = load(sessions, run_id).checkpoint
    assert checkpoint is not None and checkpoint.action is CheckpointAction.APPROVE


def test_a_veto_ends_the_run_with_an_incomplete_report(sessions: sessionmaker[Session]) -> None:
    deep = FakeStage(StageName.DEEP_RESEARCH)
    orch = orchestrator(sessions, *funnel(sessions, deep=deep))
    run_id = at_checkpoint(sessions, orch)

    with pytest.raises(CheckpointError, match="one-line reason"):
        asyncio.run(orch.checkpoint(run_id, CheckpointAction.VETO))
    outcome = asyncio.run(
        orch.checkpoint(run_id, CheckpointAction.VETO, reason="too much manual sales")
    )

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert deep.calls == []
    report = latest_report(sessions, run_id)
    brief = PartialBrief.model_validate(report.brief)
    assert report.incomplete and report.opportunity_id is None
    assert brief.candidate is None and len(brief.shortlist) == 3
    assert brief.checkpoint_reason == "too much manual sales"
    assert "vetoed at the checkpoint: too much manual sales" in render_brief(report)


def test_checkpoint_decisions_need_a_waiting_run(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *funnel(sessions))
    with pytest.raises(CheckpointError, match="no run"):
        asyncio.run(orch.checkpoint(uuid4(), CheckpointAction.APPROVE))
    run_id = at_checkpoint(sessions, orch)
    asyncio.run(orch.checkpoint(run_id, CheckpointAction.APPROVE))
    with pytest.raises(CheckpointError, match="not waiting at the checkpoint"):
        asyncio.run(orch.checkpoint(run_id, CheckpointAction.APPROVE))


def test_autonomous_runs_auto_select_the_top_candidate(sessions: sessionmaker[Session]) -> None:
    deep = FakeStage(StageName.DEEP_RESEARCH)
    orch = orchestrator(sessions, *funnel(sessions, deep=deep))
    outcome = asyncio.run(orch.start(RunConfig(checkpoint_mode=CheckpointMode.AUTONOMOUS)))

    run = load(sessions, outcome.run_id)
    assert RunStatus.AWAITING_CHECKPOINT not in [t.to_status for t in run.transitions]
    assert run.checkpoint is not None and run.checkpoint.action is CheckpointAction.AUTO
    assert run.selected_opportunity_id == orch.shortlist(outcome.run_id)[0].id
    assert len(deep.calls) == 1


# --- budget pre-checks and HALTED_BUDGET partials ------------------------------------------


def test_pre_flight_refuses_a_run_that_cannot_reach_the_end(
    sessions: sessionmaker[Session],
) -> None:
    orch = orchestrator(sessions, *funnel(sessions), minimums={StageName.DEEP_RESEARCH: 5.0})
    with pytest.raises(BudgetPreflightError):
        orch.create_run(RunConfig(budget_ceiling_usd=4.0))


def test_the_pre_check_halts_before_an_unaffordable_stage_then_a_top_up_resumes(
    sessions: sessionmaker[Session],
) -> None:
    deep = FakeStage(StageName.DEEP_RESEARCH)
    # $0.5 per fake stage; deep research needs $5 but only $4.50 is left after the funnel.
    orch = orchestrator(
        sessions, *funnel(sessions, deep=deep), minimums={StageName.DEEP_RESEARCH: 5.0}
    )
    run_id = at_checkpoint(sessions, orch, RunConfig(budget_ceiling_usd=6.0))

    halted = asyncio.run(orch.checkpoint(run_id, CheckpointAction.APPROVE))

    assert halted.status is RunStatus.HALTED_BUDGET
    assert deep.calls == []  # never started
    assert StageName.DEEP_RESEARCH not in {s.name for s in stages(sessions, run_id)}
    run = load(sessions, run_id)
    assert run.current_stage is StageName.DEEP_RESEARCH
    assert run.transitions[-1].reason == (
        "budget pre-check: the deep_research stage needs at least $5.00 and only $4.50 is "
        "left of the $6.00 ceiling"
    )

    report = latest_report(sessions, run_id)
    brief = PartialBrief.model_validate(report.brief)
    assert report.incomplete and report.halted_at_stage is StageName.DEEP_RESEARCH
    assert brief.headline == "INCOMPLETE — halted at stage deep_research"
    assert brief.verdict is None and brief.verdict_cap is Verdict.PROMISING_BUT_UNPROVEN
    assert brief.candidate is not None and brief.candidate.title == TITLES[0]
    assert report.opportunity_id == run.selected_opportunity_id
    assert any(axis.startswith("customer") for axis in brief.not_examined)
    assert not any(axis.startswith("triage") for axis in brief.not_examined)
    assert brief.still_needed_usd == pytest.approx(5.0)
    assert brief.next_step == f"opp resume {run_id} --budget 6.50"
    # Every claim in the candidate's evidence resolves to a stored source or is marked weak.
    for claim in report.dossier["evidence"]:
        assert claim["sources"] or claim["type"] in ("assumption", "unknown")

    text = render_brief(report)
    assert "INCOMPLETE — halted at stage deep_research" in text
    assert "Verdict: none" in text and "promising-but-unproven" in text
    assert "1 of 3 claims rest on assumptions" in text
    assert "Insufficient evidence: defensibility" in text
    assert "https://forum.example.com/" in text

    resumed = asyncio.run(orch.resume(run_id, budget_ceiling_usd=6.5))
    assert len(deep.calls) == 1
    assert resumed.status is RunStatus.COMPLETED_PARTIAL  # synthesis isn't built here


def test_a_mid_stage_budget_halt_and_a_cancel_both_leave_a_partial(
    sessions: sessionmaker[Session],
) -> None:
    orch = orchestrator(sessions, FakeStage(StageName.DISCOVERY, [StageResult.HALTED_BUDGET]))
    halted = asyncio.run(orch.start(RunConfig()))

    report = latest_report(sessions, halted.run_id)
    brief = PartialBrief.model_validate(report.brief)
    assert brief.run_status is RunStatus.HALTED_BUDGET
    assert brief.halted_at_stage is StageName.DISCOVERY
    assert brief.candidate is None and brief.funnel.leads == 0
    assert brief.next_step == f"opp resume {halted.run_id}"

    orch.cancel(halted.run_id)
    after_cancel = latest_report(sessions, halted.run_id)
    assert after_cancel.id != report.id
    assert PartialBrief.model_validate(after_cancel.brief).run_status is RunStatus.CANCELLED
    assert PartialBrief.model_validate(after_cancel.brief).next_step is None


def test_the_cost_ledger_rolls_up_traces(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions)
    run = orch.create_run(RunConfig(budget_ceiling_usd=10.0))
    with sessions() as session:
        traces = TraceRepository(session)
        for stage, agent, kind, cost, ok in [
            (StageName.DISCOVERY, "discovery:unmet_need", TraceKind.LLM, 0.10, True),
            (StageName.DISCOVERY, "discovery:unmet_need", TraceKind.SEARCH, 0.008, True),
            (StageName.SCREENING, "triage", TraceKind.LLM, 0.05, False),
            (None, None, TraceKind.FETCH, 0.0, True),
        ]:
            traces.add(
                Trace(run_id=run.id, stage=stage, agent=agent, kind=kind, cost_usd=cost, ok=ok)
            )
        session.commit()
        ledger = cost_ledger(session, run)

    assert ledger.spent_usd == pytest.approx(0.158)
    assert ledger.remaining_usd == pytest.approx(9.842)
    assert ledger.by_stage == pytest.approx(
        {"discovery": 0.108, "screening": 0.05, "unattributed": 0.0}
    )
    assert ledger.by_agent["discovery:unmet_need"] == pytest.approx(0.108)
    assert ledger.calls == {"llm": 2, "search": 1, "fetch": 1}
    assert ledger.failed_calls == 1
