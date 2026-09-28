"""The checkpoint: approve / pick / veto with reasons, autonomous auto-select, cancel."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import (
    DedupRepository,
    OpportunityRepository,
    ReportRepository,
    RunRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    BusinessShape,
    CheckpointAction,
    CheckpointMode,
    DedupStatus,
    Dimension,
    EvidenceBasis,
    OpportunityStatus,
    RunStatus,
    StageName,
)
from opp.domain.opportunity import (
    Customer,
    Opportunity,
    Problem,
    ProvisionalScore,
    ShallowScore,
)
from opp.domain.run import ResearchRun, RunConfig
from opp.evidence.dedup import opportunity_dedup_hash
from opp.orchestration.budget import BudgetPolicy
from opp.orchestration.orchestrator import CheckpointError, Orchestrator
from opp.orchestration.stages import StageHandler, StageServices
from tests.evidence.conftest import make_claim
from tests.orchestration.test_orchestrator import FakeStage

APPROVE, PICK, VETO = CheckpointAction.APPROVE, CheckpointAction.PICK, CheckpointAction.VETO


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'checkpoint.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session], *handlers: FakeStage) -> Orchestrator:
    services = StageServices(
        sessions=sessions,
        gateway=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        sink=lambda trace: None,
        search_cost_estimate_usd=0.0,
        discovery_budget=ResearchBudget(),
    )
    table: dict[StageName, StageHandler] = {h.name: h for h in handlers}
    return Orchestrator(services, table, policy=BudgetPolicy())


def candidate(run_id: UUID, rank: int, customer: str) -> Opportunity:
    return Opportunity(
        run_id=run_id,
        title=f"Candidate {rank}",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        customer=Customer(segment=make_claim(customer)),
        problem=Problem(description=make_claim(f"{customer} lose hours to paperwork")),
        shallow=ShallowScore(
            dimension_scores=[
                ProvisionalScore(
                    dimension=Dimension.CUSTOMER_PAIN,
                    score=5 - rank,
                    rationale="from forum threads",
                    evidence=EvidenceBasis.SOURCED,
                )
            ],
            composite=5.0 - rank,
            weights_ref="bootstrap",
            rank=rank,
        ),
    )


def seeded(
    sessions: sessionmaker[Session],
    status: RunStatus = RunStatus.AWAITING_CHECKPOINT,
    mode: CheckpointMode = CheckpointMode.MANDATORY,
) -> tuple[UUID, list[Opportunity]]:
    run = ResearchRun(
        run_config=RunConfig(checkpoint_mode=mode),
        status=status,
        current_stage=StageName.CHECKPOINT
        if status is RunStatus.AWAITING_CHECKPOINT
        else StageName.SHALLOW_VALIDATION,
    )
    opportunities = [
        candidate(run.id, rank, customer)
        for rank, customer in enumerate(["HVAC contractors", "physio clinics", "vets"], start=1)
    ]
    with sessions() as session:
        RunRepository(session).add(run)
        for opportunity in opportunities:
            OpportunityRepository(session).add(opportunity)
        session.commit()
    return run.id, opportunities


def load(
    sessions: sessionmaker[Session], run_id: UUID
) -> tuple[ResearchRun, dict[str, Opportunity]]:
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        opportunities = {o.title: o for o in OpportunityRepository(session).list(run_id=run_id)}
    assert run is not None
    return run, opportunities


def test_approve_sends_the_top_candidate_to_deep_research(
    sessions: sessionmaker[Session],
) -> None:
    run_id, _ = seeded(sessions)
    outcome = asyncio.run(orchestrator(sessions).checkpoint(run_id, APPROVE))

    # Deep research isn't built yet, so the run ends partial right after the decision.
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    run, opportunities = load(sessions, run_id)
    top = opportunities["Candidate 1"]
    assert run.selected_opportunity_id == top.id
    assert top.status is OpportunityStatus.PROMOTED
    assert opportunities["Candidate 2"].status is OpportunityStatus.NEW  # runner-up kept
    [decision] = run.checkpoint_decisions
    assert (decision.action, decision.opportunity_id) == (APPROVE, top.id)
    assert run.transitions[0].reason == "checkpoint: approve 'Candidate 1'"
    with sessions() as session:
        [report] = ReportRepository(session).list_for_run(run_id)
    assert report.opportunity_id == top.id
    assert report.brief["candidate"]["title"] == "Candidate 1"
    assert [s["selected"] for s in report.brief["shortlist"]] == [True, False, False]


def test_picking_over_the_ranking_needs_a_reason(sessions: sessionmaker[Session]) -> None:
    run_id, opportunities = seeded(sessions)
    second = opportunities[1]
    orch = orchestrator(sessions)

    with pytest.raises(CheckpointError, match="needs a one-line reason"):
        asyncio.run(orch.checkpoint(run_id, PICK, opportunity_id=second.id))
    with pytest.raises(CheckpointError, match="use pick"):
        asyncio.run(orch.checkpoint(run_id, APPROVE, opportunity_id=second.id))
    with pytest.raises(CheckpointError, match="not an open candidate"):
        asyncio.run(orch.checkpoint(run_id, PICK, opportunity_id=uuid4(), reason="x"))

    asyncio.run(
        orch.checkpoint(run_id, PICK, opportunity_id=second.id, reason="  I know physios  ")
    )
    run, _ = load(sessions, run_id)
    assert run.selected_opportunity_id == second.id
    assert run.checkpoint_decisions[-1].reason == "I know physios"


def test_veto_discards_and_proposes_the_next_one(sessions: sessionmaker[Session]) -> None:
    run_id, opportunities = seeded(sessions)
    orch = orchestrator(sessions)

    with pytest.raises(CheckpointError, match="veto needs a one-line reason"):
        asyncio.run(orch.checkpoint(run_id, VETO))
    outcome = asyncio.run(orch.checkpoint(run_id, VETO, reason="I hate HVAC"))

    assert outcome.status is RunStatus.AWAITING_CHECKPOINT
    run, after = load(sessions, run_id)
    assert after["Candidate 1"].status is OpportunityStatus.DISCARDED
    assert run.selected_opportunity_id is None
    assert [o.title for o in orch.shortlist(run_id)] == ["Candidate 2", "Candidate 3"]
    with sessions() as session:  # discarded: never resurfaced by a later run
        fingerprint = opportunity_dedup_hash(opportunities[0])
        assert fingerprint is not None
        entry = DedupRepository(session).get(fingerprint)
    assert entry is not None and entry.status is DedupStatus.DISCARDED

    # Approve now takes the new top candidate.
    asyncio.run(orch.checkpoint(run_id, APPROVE))
    run, _ = load(sessions, run_id)
    assert run.selected_opportunity_id == opportunities[1].id


def test_vetoing_everything_ends_the_run_with_a_partial(sessions: sessionmaker[Session]) -> None:
    run_id, _ = seeded(sessions)
    orch = orchestrator(sessions)
    for _ in range(3):
        outcome = asyncio.run(orch.checkpoint(run_id, VETO, reason="not for me"))

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    run, _ = load(sessions, run_id)
    assert run.transitions[-1].reason == "the operator vetoed every shortlisted opportunity"
    assert [d.action for d in run.checkpoint_decisions] == [VETO] * 3
    with sessions() as session:
        [report] = ReportRepository(session).list_for_run(run_id)
    assert report.brief["candidate"] is None
    assert report.brief["vetoed"] == ["Candidate 1", "Candidate 2", "Candidate 3"]
    with pytest.raises(CheckpointError, match="not at the checkpoint"):
        asyncio.run(orch.checkpoint(run_id, APPROVE))


def test_autonomous_mode_selects_the_top_candidate(sessions: sessionmaker[Session]) -> None:
    run_id, opportunities = seeded(
        sessions, status=RunStatus.SHALLOW_VALIDATION, mode=CheckpointMode.AUTONOMOUS
    )
    shallow = FakeStage(StageName.SHALLOW_VALIDATION, cost=0.0)

    outcome = asyncio.run(orchestrator(sessions, shallow).advance(run_id))

    assert outcome.status is RunStatus.COMPLETED_PARTIAL  # no deep research yet
    run, _ = load(sessions, run_id)
    assert run.selected_opportunity_id == opportunities[0].id
    [decision] = run.checkpoint_decisions
    assert decision.action is CheckpointAction.AUTO and decision.reason == ""


def test_cancelling_at_the_checkpoint_keeps_the_work(sessions: sessionmaker[Session]) -> None:
    run_id, _ = seeded(sessions)
    orchestrator(sessions).cancel(run_id)

    run, opportunities = load(sessions, run_id)
    assert run.status is RunStatus.CANCELLED
    assert len(opportunities) == 3
    with sessions() as session:
        [report] = ReportRepository(session).list_for_run(run_id)
    assert report.brief["status"] == "cancelled"
    assert report.brief["headline"] == "INCOMPLETE — halted at stage checkpoint"
