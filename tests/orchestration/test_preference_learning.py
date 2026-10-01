"""V1 preference learning + exploration allowance (plan.md §35 Phase 9, intent.md §19.2).

Checkpoint vetoes/re-picks and discards accumulate into a cross-run profile; discovery and
triage read it as soft steering; a fraction of discovery deliberately ignores it. Driven
through the orchestrator on fake stages — no network."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.agents.discovery import DiscoveryAgent
from opp.db.base import Base
from opp.db.repositories import PreferenceRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    CheckpointAction,
    PreferenceOrigin,
    PreferenceSentiment,
    SignalSource,
)
from opp.domain.run import RunConfig
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import DiscoveryStage, StageServices, load_learned_preferences
from tests.orchestration.test_checkpoint import at_checkpoint, funnel
from tests.orchestration.test_orchestrator import orchestrator


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'preferences.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _signals(sessions: sessionmaker[Session]) -> list:
    with sessions() as session:
        return PreferenceRepository(session).list_recent(50)


def test_a_veto_records_a_dislike_preference(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *funnel(sessions))
    run_id = at_checkpoint(sessions, orch)

    asyncio.run(
        orch.checkpoint(run_id, CheckpointAction.VETO, reason="market too small even for me")
    )

    [signal] = _signals(sessions)
    assert signal.origin is PreferenceOrigin.CHECKPOINT_VETO
    assert signal.sentiment is PreferenceSentiment.DISLIKE
    assert signal.reason == "market too small even for me"
    assert signal.run_id == run_id


def test_a_re_pick_records_a_neutral_preference(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *funnel(sessions))
    run_id = at_checkpoint(sessions, orch)
    runner_up = orch.shortlist(run_id)[1]

    asyncio.run(
        orch.checkpoint(
            run_id,
            CheckpointAction.PICK,
            opportunity_id=runner_up.id,
            reason="loved the automation angle",
        )
    )

    [signal] = _signals(sessions)
    assert signal.origin is PreferenceOrigin.CHECKPOINT_PICK
    assert signal.sentiment is PreferenceSentiment.NEUTRAL
    assert signal.opportunity_id == runner_up.id


def test_an_approve_records_nothing(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *funnel(sessions))
    run_id = at_checkpoint(sessions, orch)
    asyncio.run(orch.checkpoint(run_id, CheckpointAction.APPROVE))
    assert _signals(sessions) == []


def test_record_preference_is_a_no_op_when_learning_is_off(sessions: sessionmaker[Session]) -> None:
    services = StageServices(
        sessions=sessions,
        gateway=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        sink=lambda trace: None,
        search_cost_estimate_usd=0.0,
        discovery_budget=ResearchBudget(),
        preference_learning=False,
    )
    orch = Orchestrator(services, {})
    orch.record_preference("too much manual sales", PreferenceOrigin.DISCARD)
    assert _signals(sessions) == []


def test_discovery_reads_the_learned_profile_as_steering(sessions: sessionmaker[Session]) -> None:
    # Seed a run + veto so a learned profile exists, then read it back the way a stage does.
    orch = orchestrator(sessions, *funnel(sessions))
    run_id = at_checkpoint(sessions, orch)
    asyncio.run(orch.checkpoint(run_id, CheckpointAction.VETO, reason="too much manual sales"))

    services = StageServices(
        sessions=sessions,
        gateway=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        sink=lambda trace: None,
        search_cost_estimate_usd=0.0,
        discovery_budget=ResearchBudget(),
    )
    learned = load_learned_preferences(services)
    assert "too much manual sales" in learned

    # A normal discovery agent folds the profile into its prompt; an exploration agent doesn't.
    steered = DiscoveryAgent(SignalSource.UNMET_NEED, target_leads=5, learned_preferences=learned)
    explorer = DiscoveryAgent(SignalSource.UNMET_NEED, target_leads=5, learned_preferences="")
    assert "too much manual sales" in steered._preferences(RunConfig())
    assert explorer._preferences(RunConfig()) == "none"


def test_learning_off_yields_no_steering(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *funnel(sessions))
    run_id = at_checkpoint(sessions, orch)
    asyncio.run(orch.checkpoint(run_id, CheckpointAction.VETO, reason="too much manual sales"))
    services = StageServices(
        sessions=sessions,
        gateway=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        sink=lambda trace: None,
        search_cost_estimate_usd=0.0,
        discovery_budget=ResearchBudget(),
        preference_learning=False,
    )
    assert load_learned_preferences(services) == ""


def _stage(sessions: sessionmaker[Session], *, fraction: float) -> DiscoveryStage:
    services = StageServices(
        sessions=sessions,
        gateway=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        sink=lambda trace: None,
        search_cost_estimate_usd=0.0,
        discovery_budget=ResearchBudget(),
        exploration_fraction=fraction,
    )
    return DiscoveryStage(services)


def test_exploration_allowance_splits_sources_deterministically(
    sessions: sessionmaker[Session],
) -> None:
    stage = _stage(sessions, fraction=0.2)
    sources = list(SignalSource)
    assert len(sources) == 10

    # With a learned profile, ~20% of ten sources (2) explore; the rest keep the profile.
    exploring = stage._exploration_sources(sources, learned="avoid manual sales")
    assert len(exploring) == 2

    # No learned profile → nothing to ignore, so no exploration.
    assert stage._exploration_sources(sources, learned="") == frozenset()

    # A single source always uses the profile (never leaves the run with zero steering).
    assert stage._exploration_sources(sources[:1], learned="avoid manual sales") == frozenset()


def test_exploration_never_swallows_the_whole_run(sessions: sessionmaker[Session]) -> None:
    stage = _stage(sessions, fraction=1.0)  # even at 100%, keep at least one steered source
    two = [SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS]
    exploring = stage._exploration_sources(two, learned="avoid manual sales")
    assert len(exploring) == 1  # capped at n-1
