"""Phase 4 acceptance for triage + shallow validation, driven by the orchestrator:
generic decoys are killed at triage with reasons, duplicates merged, survivors ranked and
scored, and the run waits at the checkpoint (or ends honestly when nothing survives)."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import (
    DedupRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    RunRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    CheckpointMode,
    DedupStatus,
    FailedGate,
    LeadStatus,
    RunStatus,
    StageName,
)
from opp.domain.lead import Lead
from opp.domain.run import LeadTargets, ResearchRun, RunConfig
from opp.evidence.store import EvidenceStore
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator, RunOutcome
from opp.orchestration.stages import StageServices
from tests.agents.fakes import FunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING
from tests.agents.test_triage import FORUM, SOURCES, lead
from tests.evidence.conftest import make_claim


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'funnel.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session], llm: FunnelLLM | None = None) -> Orchestrator:
    sink = DbTraceSink(sessions)
    return Orchestrator(
        StageServices(
            sessions=sessions,
            gateway=LLMGateway(llm or FunnelLLM(), ROUTING, sink),
            search=SnippetSearch(),
            sink=sink,
            search_cost_estimate_usd=0.008,
            discovery_budget=ResearchBudget(),
        )
    )


def seeded_run(
    sessions: sessionmaker[Session], leads: list[Lead], config: RunConfig | None = None
) -> UUID:
    """A run that discovery has just finished: its leads are stored, triage is next."""
    run = ResearchRun(
        run_config=config or RunConfig(),
        status=RunStatus.SCREENING,
        current_stage=StageName.SCREENING,
    )
    with sessions() as session:
        RunRepository(session).add(run)
        store = EvidenceStore(session)
        for source in SOURCES.values():
            store.add_source(source)
        for item in leads:
            item.run_id = run.id
            store.save_lead(item)
        session.commit()
    return run.id


def decoy(title: str = "AI chatbot for dental front desks") -> Lead:
    return lead(
        title,
        named_customer=make_claim("dental practice front desks", sources=[FORUM]),
        specific_pain=make_claim("front desks miss calls during treatment", sources=[FORUM]),
    )


def funnel_leads() -> dict[str, Lead]:
    dental = lead(
        "No-show recovery for physio clinics",
        named_customer=make_claim("independent physiotherapy clinics", sources=[FORUM]),
        specific_pain=make_claim("no-shows cost clinics a day of revenue a week", sources=[FORUM]),
    )
    return {
        "hvac": lead(),
        "hvac-again": lead("HVAC dispatch helper"),  # same customer + pain: a duplicate
        "physio": dental,
        "decoy": decoy(),  # generic, bar not cleared
        "vague": lead(
            "Scheduling for businesses",
            named_customer=make_claim("small businesses", sources=[FORUM]),
        ),
    }


def statuses(sessions: sessionmaker[Session], run_id: UUID) -> dict[str, Lead]:
    with sessions() as session:
        return {item.title: item for item in LeadRepository(session).list_for_run(run_id)}


def gates(sessions: sessionmaker[Session], item: Lead) -> list[FailedGate | None]:
    with sessions() as session:
        return [k.failed_gate for k in KillRepository(session).list_for_subject(item.id)]


def test_triage_and_shallow_validation_take_the_run_to_the_checkpoint(
    sessions: sessionmaker[Session],
) -> None:
    leads = funnel_leads()
    run_id = seeded_run(sessions, list(leads.values()))

    outcome: RunOutcome = asyncio.run(orchestrator(sessions).advance(run_id))

    assert outcome.status is RunStatus.AWAITING_CHECKPOINT
    after = statuses(sessions, run_id)
    assert after["Dispatch board for small HVAC shops"].status is LeadStatus.KEPT
    assert after["No-show recovery for physio clinics"].status is LeadStatus.KEPT
    assert after["HVAC dispatch helper"].status is LeadStatus.DUPLICATE
    assert gates(sessions, after["HVAC dispatch helper"]) == [FailedGate.DUPLICATE]
    decoy = after["AI chatbot for dental front desks"]
    assert decoy.status is LeadStatus.KILLED
    assert gates(sessions, decoy) == [FailedGate.GENERIC_PATTERN]
    with sessions() as session:
        [kill] = KillRepository(session).list_for_subject(decoy.id)
    assert "generic pattern (AI chatbot/assistant for X)" in kill.reason
    vague = after["Scheduling for businesses"]
    assert gates(sessions, vague) == [FailedGate.NAMED_CUSTOMER]

    # Decided leads are in the cross-run index; duplicates aren't (their survivor is).
    with sessions() as session:
        dedup = DedupRepository(session)
        assert dedup.get(after["Dispatch board for small HVAC shops"].dedup_hash or "")
        killed = dedup.get(decoy.dedup_hash or "")
        assert killed is not None and killed.status is DedupStatus.KILLED
        shortlist = OpportunityRepository(session).list(run_id=run_id)
    # Both survivors were scored and ranked; rank 1 is the deep-research candidate.
    assert sorted(o.shallow.rank or 0 for o in shortlist if o.shallow) == [1, 2]
    assert {o.lead_id for o in shortlist} == {
        after["Dispatch board for small HVAC shops"].id,
        after["No-show recovery for physio clinics"].id,
    }
    assert all(o.screening_claims and o.shallow.composite == 4.0 for o in shortlist)  # type: ignore[union-attr]
    with sessions() as session:
        run = RunRepository(session).get(run_id)
    assert run is not None
    assert [t.to_status for t in run.transitions] == [
        RunStatus.SHALLOW_VALIDATION,
        RunStatus.AWAITING_CHECKPOINT,
    ]


def test_only_the_top_survivors_are_scored(sessions: sessionmaker[Session]) -> None:
    leads = funnel_leads()
    config = RunConfig(
        lead_targets=LeadTargets(shortlist_n=1), checkpoint_mode=CheckpointMode.AUTONOMOUS
    )
    run_id = seeded_run(sessions, list(leads.values()), config)

    outcome = asyncio.run(orchestrator(sessions).advance(run_id))

    # Autonomous: past the checkpoint, through deep research and synthesis, stopping at the
    # strategy stage, which doesn't exist yet. Only the single top survivor was ever scored.
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    with sessions() as session:
        [only] = OpportunityRepository(session).list(run_id=run_id)
        run = RunRepository(session).get(run_id)
    assert only.shallow is not None and only.shallow.rank == 1
    assert run is not None and "strategy stage is not available yet" in (run.transitions[-1].reason)


def test_a_later_run_does_not_resurface_known_leads(sessions: sessionmaker[Session]) -> None:
    first = seeded_run(sessions, [lead(), decoy()])
    asyncio.run(orchestrator(sessions).advance(first))

    second = seeded_run(sessions, [lead("Dispatch board, again"), decoy("Dental AI chatbot again")])
    outcome = asyncio.run(orchestrator(sessions).advance(second))

    after = statuses(sessions, second)
    assert {item.status for item in after.values()} == {LeadStatus.DUPLICATE}
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    with sessions() as session:
        run = RunRepository(session).get(second)
    assert run is not None and run.transitions[-1].reason == "screening: no lead survived triage"


def test_nothing_surviving_ends_the_run_honestly(sessions: sessionmaker[Session]) -> None:
    leads = funnel_leads()
    run_id = seeded_run(sessions, [leads["decoy"], leads["vague"]])

    outcome = asyncio.run(orchestrator(sessions).advance(run_id))

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    with sessions() as session:
        assert OpportunityRepository(session).list(run_id=run_id) == []
