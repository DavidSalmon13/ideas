from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from opp.db import models
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    ClaimRepository,
    DedupRepository,
    EvaluationRepository,
    ExperimentRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    ReportRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
    TraceRepository,
    url_hash,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    AgentRunStatus,
    BusinessShape,
    ClaimType,
    CompetitorType,
    DedupStatus,
    Dimension,
    EvidenceStrength,
    ExperimentMethod,
    FailedGate,
    KillSubject,
    LeadStatus,
    Level,
    ModelTier,
    OpportunityStatus,
    RunStatus,
    SignalSource,
    SourceType,
    StageName,
    TraceKind,
    Verdict,
)
from opp.domain.evaluation import DimensionScore, Evaluation
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Kill, Lead, UnsourcedLeadError
from opp.domain.opportunity import Competitor, Experiment, Opportunity
from opp.domain.report import Report, Trace
from opp.domain.run import AgentRun, ResearchRun, Stage


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'repo.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as db:
        yield db
    engine.dispose()


def _claim(statement: str = "a claim", type_: ClaimType = ClaimType.ASSUMPTION) -> Claim:
    return Claim(
        statement=statement,
        type=type_,
        confidence=Level.LOW,
        confidence_reason="thin evidence",
    )


def _lead(run_id, **kwargs) -> Lead:  # type: ignore[no-untyped-def]
    return Lead(
        run_id=run_id,
        title="HVAC scheduling pain",
        business_shape=BusinessShape.SAAS,
        named_customer=_claim("independent HVAC contractors"),
        # The repository refuses leads that cite nothing; this one cites a source id.
        specific_pain=_claim("hate current scheduling tools").model_copy(
            update={"source_ids": [uuid4()]}
        ),
        why_now=_claim("new state licensing API in 2025"),
        signal_source=SignalSource.UNMET_NEED,
        **kwargs,
    )


def test_run_round_trip_and_status_query(session: Session) -> None:
    repo = RunRepository(session)
    run = ResearchRun()
    repo.add(run)
    session.commit()

    loaded = repo.get(run.id)
    assert loaded is not None
    assert loaded.id == run.id
    assert loaded.status is RunStatus.CREATED

    assert [r.id for r in repo.list_by_status(RunStatus.CREATED)] == [run.id]
    assert [r.id for r in repo.list_resumable()] == [run.id]


def test_run_upsert_updates_projection(session: Session) -> None:
    repo = RunRepository(session)
    run = ResearchRun()
    repo.add(run)
    session.commit()

    run.status = RunStatus.DISCOVERING
    run.cost_to_date_usd = 3.5
    run.current_stage = StageName.DISCOVERY
    repo.upsert(run)
    session.commit()

    row = session.get(models.RunRow, run.id)
    assert row is not None
    assert row.status == RunStatus.DISCOVERING.value
    assert row.cost_to_date_usd == 3.5
    assert row.current_stage == StageName.DISCOVERY.value
    assert repo.list_by_status(RunStatus.CREATED) == []


def test_completed_run_not_resumable(session: Session) -> None:
    repo = RunRepository(session)
    run = ResearchRun(status=RunStatus.COMPLETED)
    repo.add(run)
    halted = ResearchRun(status=RunStatus.HALTED_BUDGET)
    repo.add(halted)
    session.commit()

    resumable = {r.id for r in repo.list_resumable()}
    assert resumable == {halted.id}


def test_lead_queries_by_run_and_status_and_dedup(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    leads = LeadRepository(session)
    kept = _lead(run.id, status=LeadStatus.KEPT, dedup_hash="abc")
    killed = _lead(run.id, status=LeadStatus.KILLED)
    leads.add(kept)
    leads.add(killed)
    session.commit()

    assert {lead.id for lead in leads.list_for_run(run.id)} == {kept.id, killed.id}
    assert [lead.id for lead in leads.list_for_run(run.id, status=LeadStatus.KEPT)] == [kept.id]
    assert [lead.id for lead in leads.get_by_dedup_hash("abc")] == [kept.id]


def test_stage_and_agent_run_round_trip(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    stage = Stage(run_id=run.id, name=StageName.DISCOVERY)
    StageRepository(session).add(stage)
    agent = AgentRun(
        stage_id=stage.id,
        agent_name="discovery.unmet_need",
        model_tier=ModelTier.CHEAP,
        status=AgentRunStatus.RUNNING,
    )
    AgentRunRepository(session).add(agent)
    session.commit()

    assert [s.id for s in StageRepository(session).list_for_run(run.id)] == [stage.id]
    got = AgentRunRepository(session).list_for_stage(stage.id)
    assert [a.id for a in got] == [agent.id]
    assert got[0].status is AgentRunStatus.RUNNING


def test_source_dedup_by_url_hash(session: Session) -> None:
    repo = SourceRepository(session)
    source = Source(
        url="https://example.com/pricing",
        title="Pricing",
        source_type=SourceType.PRICING_PAGE,
        credibility_tier=Level.MEDIUM,
        excerpt="Plans from $49/mo",
    )
    repo.add(source)
    session.commit()

    assert repo.get_by_url("https://example.com/pricing").id == source.id  # type: ignore[union-attr]
    assert repo.get_by_url_hash(url_hash(source.url)).id == source.id  # type: ignore[union-attr]
    assert repo.get_by_url("https://example.com/other") is None


def test_claim_persists_with_source_join(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    opp = Opportunity(
        run_id=run.id, title="HVAC scheduler", business_shape=BusinessShape.SAAS, profile_ref="x"
    )
    OpportunityRepository(session).add(opp)
    source = Source(
        url="https://forum.example.com/thread",
        title="Complaint thread",
        source_type=SourceType.FORUM,
        credibility_tier=Level.LOW,
        excerpt="scheduling is a nightmare",
    )
    SourceRepository(session).add(source)
    claim = Claim(
        statement="contractors dislike current schedulers",
        type=ClaimType.FACT,
        confidence=Level.MEDIUM,
        confidence_reason="multiple forum posts",
        evidence_strength=EvidenceStrength.MODERATE,
        source_ids=[source.id],
    )
    ClaimRepository(session).add(claim, opportunity_id=opp.id)
    session.commit()

    got = ClaimRepository(session).list_for_opportunity(opp.id)
    assert [c.id for c in got] == [claim.id]
    assert got[0].source_ids == [source.id]

    join_rows = session.query(models.ClaimSourceRow).all()
    assert [(r.claim_id, r.source_id) for r in join_rows] == [(claim.id, source.id)]


def test_opportunity_projects_evaluation_and_filters(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    repo = OpportunityRepository(session)
    evaluated = Opportunity(
        run_id=run.id,
        title="Evaluated",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        status=OpportunityStatus.NEW,
        competition=[Competitor(name="Acme", type=CompetitorType.DIRECT)],
        evaluation=Evaluation(
            dimension_scores=[
                DimensionScore(dimension=Dimension.GROSS_MARGIN, score=4, justification="high")
            ],
            weights_ref="bootstrap",
            composite=3.8,
            verdict=Verdict.PROMISING_BUT_UNPROVEN,
            justification="hinges on CAC",
        ),
    )
    plain = Opportunity(
        run_id=run.id, title="Plain", business_shape=BusinessShape.SERVICE, profile_ref="bootstrap"
    )
    repo.add(evaluated)
    repo.add(plain)
    session.commit()

    row = session.get(models.OpportunityRow, evaluated.id)
    assert row is not None
    assert row.composite == 3.8
    assert row.verdict == Verdict.PROMISING_BUT_UNPROVEN.value

    by_verdict = repo.list(verdict=Verdict.PROMISING_BUT_UNPROVEN)
    assert [o.id for o in by_verdict] == [evaluated.id]
    loaded = repo.get(evaluated.id)
    assert loaded is not None
    assert loaded.competition[0].name == "Acme"
    assert len(repo.list(run_id=run.id)) == 2


def test_evaluation_and_experiment_repositories(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    opp = Opportunity(
        run_id=run.id, title="opp", business_shape=BusinessShape.SAAS, profile_ref="bootstrap"
    )
    OpportunityRepository(session).add(opp)
    evaluation = Evaluation(
        dimension_scores=[
            DimensionScore(dimension=Dimension.REVENUE_POTENTIAL, score=3, justification="ok")
        ],
        weights_ref="bootstrap",
        composite=3.0,
        verdict=Verdict.PARK,
        justification="weak",
    )
    EvaluationRepository(session).add(evaluation, opportunity_id=opp.id)

    exp_repo = ExperimentRepository(session)
    second = Experiment(
        hypothesis="people pay",
        leverage=Level.HIGH,
        method=ExperimentMethod.PRESALE,
        success_criteria="5 presales",
        priority=2,
    )
    first = Experiment(
        hypothesis="pain is real",
        leverage=Level.HIGH,
        method=ExperimentMethod.INTERVIEW,
        success_criteria="8/10 confirm",
        priority=1,
    )
    exp_repo.add(second, opportunity_id=opp.id)
    exp_repo.add(first, opportunity_id=opp.id)
    session.commit()

    got_eval = EvaluationRepository(session).get_for_opportunity(opp.id)
    assert got_eval is not None
    assert got_eval.verdict is Verdict.PARK
    ordered = exp_repo.list_for_opportunity(opp.id)
    assert [e.priority for e in ordered] == [1, 2]


def test_kill_and_report_and_trace(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    lead = _lead(run.id)
    LeadRepository(session).add(lead)

    kill = Kill(
        subject_kind=KillSubject.LEAD,
        subject_id=lead.id,
        stage=StageName.SCREENING.value,
        reason="fails why-now test",
        failed_gate=FailedGate.WHY_NOW,
    )
    KillRepository(session).add(kill)

    report = Report(run_id=run.id, incomplete=False, brief={"verdict": "park"})
    ReportRepository(session).add(report)

    traces = TraceRepository(session)
    traces.add(Trace(run_id=run.id, kind=TraceKind.SEARCH, cost_usd=0.4))
    traces.add(Trace(run_id=run.id, kind=TraceKind.LLM, cost_usd=1.1))
    session.commit()

    assert [k.id for k in KillRepository(session).list_for_subject(lead.id)] == [kill.id]
    assert [r.id for r in ReportRepository(session).list_for_run(run.id)] == [report.id]
    assert TraceRepository(session).total_cost_for_run(run.id) == pytest.approx(1.5)


def test_trace_cost_defaults_to_zero(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    session.commit()
    assert TraceRepository(session).total_cost_for_run(run.id) == 0.0


def test_dedup_index_upsert(session: Session) -> None:
    repo = DedupRepository(session)
    assert repo.get("hash-1") is None

    repo.upsert("hash-1", DedupStatus.ACTIVE)
    session.commit()
    entry = repo.get("hash-1")
    assert entry is not None
    assert entry.status is DedupStatus.ACTIVE

    repo.upsert("hash-1", DedupStatus.DISCARDED)
    session.commit()
    assert repo.get("hash-1").status is DedupStatus.DISCARDED  # type: ignore[union-attr]
    assert [e.dedup_hash for e in repo.list_by_status(DedupStatus.DISCARDED)] == ["hash-1"]
    assert repo.list_by_status(DedupStatus.ACTIVE) == []


def test_unsourced_leads_are_never_written(session: Session) -> None:
    run = ResearchRun()
    RunRepository(session).add(run)
    unsourced = _lead(run.id).model_copy(update={"specific_pain": _claim("no source")})

    with pytest.raises(UnsourcedLeadError):
        LeadRepository(session).add(unsourced)
    with pytest.raises(UnsourcedLeadError):
        LeadRepository(session).upsert(unsourced)
