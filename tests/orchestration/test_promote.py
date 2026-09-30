"""V1 promote (plan.md §35 Phase 9, specs.md §2.11, §4.3): a shortlisted runner-up is
copied into a new deep-research run that skips discovery, triage and shallow validation and
carries it through to a categorical verdict. The source run's evidence is left untouched."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.db.base import Base
from opp.db.repositories import (
    ClaimRepository,
    OpportunityRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    BusinessShape,
    CheckpointAction,
    CheckpointMode,
    OpportunityStatus,
    RunStatus,
    StageName,
    StageStatus,
)
from opp.domain.opportunity import Opportunity
from opp.domain.run import RunConfig
from opp.orchestration.budget import BudgetPreflightError
from opp.orchestration.orchestrator import Orchestrator, PromoteError
from tests.agents.fakes import FullFunnelLLM
from tests.orchestration.test_deep_research import orchestrator
from tests.orchestration.test_screening_stages import funnel_leads, seeded_run


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'promote.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def completed_run_with_shortlist(
    sessions: sessionmaker[Session],
) -> tuple[Orchestrator, list[Opportunity]]:
    """Run the whole funnel autonomously, leaving a completed run whose shortlist has the
    deep-researched winner (rank 1) and a shallow-only runner-up (rank 2)."""
    run_id = seeded_run(
        sessions,
        list(funnel_leads().values()),
        RunConfig(checkpoint_mode=CheckpointMode.AUTONOMOUS),
    )
    orch = orchestrator(sessions, FullFunnelLLM())
    outcome = asyncio.run(orch.advance(run_id))
    assert outcome.status is RunStatus.COMPLETED
    shortlist = orch.shortlist(run_id)
    assert len(shortlist) >= 2  # a winner and at least one runner-up
    return orch, shortlist


def test_promote_deep_researches_a_runner_up_in_a_fresh_run(
    sessions: sessionmaker[Session],
) -> None:
    orch, shortlist = completed_run_with_shortlist(sessions)
    winner, runner_up = shortlist[0], shortlist[1]
    assert runner_up.evaluation is None  # a runner-up was only shallow-scored

    run = orch.promote(runner_up.id)

    # A new run that starts at deep research, scoped to a copy of the runner-up.
    assert run.status is RunStatus.DEEP_RESEARCH
    assert run.current_stage is StageName.DEEP_RESEARCH
    assert run.checkpoint is not None
    assert run.checkpoint.action is CheckpointAction.PROMOTE
    clone_id = run.selected_opportunity_id
    assert clone_id is not None and clone_id != runner_up.id
    assert run.id != winner.run_id  # a brand new run, not the source run

    outcome = asyncio.run(orch.advance(run.id))

    # It skipped the cheap funnel and ran deep research → synthesis → strategy → red-team
    # → judge to a verdict, without ever discovering or triaging.
    assert outcome.status is RunStatus.COMPLETED
    with sessions() as session:
        clone = OpportunityRepository(session).get(clone_id)
        source = OpportunityRepository(session).get(runner_up.id)
        stages = {s.name: s for s in StageRepository(session).list_for_run(run.id)}
    assert clone is not None and clone.evaluation is not None
    assert len(clone.evaluation.dimension_scores) == 16
    assert clone.research.customer and clone.synthesis is not None
    assert clone.economics.scenarios.base.outputs and clone.validation_backlog

    # The pre-deep stages are recorded as skipped; deep research and on actually ran.
    assert stages[StageName.DISCOVERY].status is StageStatus.SKIPPED
    assert stages[StageName.SCREENING].status is StageStatus.SKIPPED
    assert stages[StageName.SHALLOW_VALIDATION].status is StageStatus.SKIPPED
    assert stages[StageName.DEEP_RESEARCH].status is StageStatus.COMPLETED
    assert stages[StageName.EVALUATION].status is StageStatus.COMPLETED

    # The source opportunity is marked promoted but otherwise untouched (still no verdict).
    assert source is not None
    assert source.status is OpportunityStatus.PROMOTED
    assert source.evaluation is None


def test_promote_copies_evidence_without_touching_the_source(
    sessions: sessionmaker[Session],
) -> None:
    orch, shortlist = completed_run_with_shortlist(sessions)
    runner_up = shortlist[1]
    run = orch.promote(runner_up.id)
    clone_id = run.selected_opportunity_id
    assert clone_id is not None

    with sessions() as session:
        claims = ClaimRepository(session)
        source_ids = {c.id for c in claims.list_for_opportunity(runner_up.id)}
        clone_ids = {c.id for c in claims.list_for_opportunity(clone_id)}
    # The runner-up's shallow findings are copied under fresh claim ids: the source run's
    # evidence stays intact and disjoint from the promoted run's.
    assert source_ids and clone_ids
    assert source_ids.isdisjoint(clone_ids)


def test_promote_rejects_an_unknown_opportunity(sessions: sessionmaker[Session]) -> None:
    orch, _ = completed_run_with_shortlist(sessions)
    with pytest.raises(PromoteError, match="no opportunity"):
        orch.promote(uuid4())


def test_promote_rejects_an_opportunity_that_was_never_scored(
    sessions: sessionmaker[Session],
) -> None:
    orch, _ = completed_run_with_shortlist(sessions)
    run = orch.create_run(RunConfig())
    unscored = Opportunity(
        run_id=run.id,
        title="Never scored",
        business_shape=BusinessShape.SERVICE,
        profile_ref="bootstrap",
    )
    with sessions() as session:
        OpportunityRepository(session).add(unscored)
        session.commit()
    with pytest.raises(PromoteError, match="never shallow-scored"):
        orch.promote(unscored.id)


def test_promote_refuses_a_budget_that_cannot_cover_deep_research(
    sessions: sessionmaker[Session],
) -> None:
    orch, shortlist = completed_run_with_shortlist(sessions)
    with pytest.raises(BudgetPreflightError):
        orch.promote(shortlist[1].id, budget_ceiling_usd=0.01)
