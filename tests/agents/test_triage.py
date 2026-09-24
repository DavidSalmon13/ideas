"""Triage: the three hard gates, the generic blocklist's higher bar, dedup, ranking."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.runtime import AgentRuntime
from opp.agents.triage import PriorMatch, TriageAgent, source_strength
from opp.db import models
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    DedupRepository,
    KillRepository,
    LeadRepository,
    RunRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    AgentRunStatus,
    BusinessShape,
    ClaimType,
    DedupStatus,
    FailedGate,
    LeadStatus,
    Level,
    ModelTier,
    SignalSource,
    SourceType,
    StageName,
)
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.run import AgentRun, ResearchRun, Stage
from opp.evidence.dedup import lead_dedup_hash
from opp.evidence.store import EvidenceStore
from opp.llm.fake import FakeLLMProvider, reply
from opp.llm.gateway import LLMGateway
from opp.llm.types import CompletionRequest
from opp.observability.traces import DbTraceSink
from tests.agents.test_runtime import ROUTING

BUDGET = ResearchBudget(max_cost_usd=1.0, max_llm_calls=10, max_searches=0, max_fetches=0)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'triage.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def source(url: str, tier: Level, publisher: str | None = None) -> Source:
    return Source(
        url=url,
        title=url,
        source_type=SourceType.TRADE_PRESS,
        credibility_tier=tier,
        publisher=publisher,
        excerpt=f"excerpt of {url}",
    )


FORUM = source("https://www.reddit.com/r/HVAC/1", Level.LOW)
FORUM_2 = source("https://www.reddit.com/r/Dentistry/2", Level.LOW)
GOV = source("https://www.epa.gov/section608", Level.HIGH)
REPORT = source("https://www.gartner.com/property-tax", Level.HIGH, publisher="Gartner")
COUNTY = source("https://www.cookcountyassessor.gov/appeals", Level.HIGH)


def claim(text: str, *sources: Source, type_: ClaimType = ClaimType.FACT) -> Claim:
    return Claim(
        statement=text,
        type=type_,
        source_ids=[s.id for s in sources],
        confidence=Level.MEDIUM,
        confidence_reason="stated in the source",
    )


def lead(
    run_id: UUID,
    title: str,
    customer: Claim,
    pain: Claim,
    why_now: Claim,
    *,
    wedge: str = "",
) -> Lead:
    return Lead(
        run_id=run_id,
        title=title,
        business_shape=BusinessShape.SAAS,
        named_customer=customer,
        specific_pain=pain,
        why_now=why_now,
        candidate_wedge=wedge,
        signal_source=SignalSource.UNMET_NEED,
    )


def build_leads(run_id: UUID) -> dict[str, Lead]:
    rule = claim("EPA refrigerant tracking rules take effect January 2026", GOV)
    return {
        "good": lead(
            run_id,
            "Dispatch board for small HVAC shops",
            claim("independent HVAC contractors with 2-5 trucks", FORUM),
            claim("dispatching technicians by phone wastes hours every week", FORUM),
            rule,
            wedge="phone-to-calendar dispatch board",
        ),
        "twin": lead(  # a rewording of "good" backed by weaker evidence
            run_id,
            "HVAC dispatch helper",
            claim("independent HVAC contractors with 2-5 trucks", FORUM),
            claim("dispatching technicians by phone wastes hours", FORUM),
            claim("new refrigerant tracking rules from January 2026", FORUM),
        ),
        "vague_customer": lead(
            run_id,
            "Invoicing for everyone",
            claim("small businesses", FORUM),
            claim("chasing unpaid invoices takes hours", FORUM),
            rule,
        ),
        "unsourced_why_now": lead(
            run_id,
            "Dental no-show reminders",
            claim("solo dental practices", FORUM_2),
            claim("no-shows cost chairs every week", FORUM_2),
            claim("patients expect texts now", type_=ClaimType.ASSUMPTION),
        ),
        "trend_why_now": lead(
            run_id,
            "Farm equipment rental matching",
            claim("family-run dairy farms in Wisconsin", FORUM),
            claim("renting a spreader for a week takes a dozen calls", FORUM),
            claim("AI is getting better at matching supply and demand", FORUM),
        ),
        "generic_weak": lead(
            run_id,
            "AI chatbot for HVAC contractors",
            claim("residential HVAC contractors", FORUM),
            claim("missed after-hours calls from homeowners", FORUM),
            claim("refrigerant rule changes from January 2026", FORUM),
            wedge="an AI chatbot for HVAC contractors' websites",
        ),
        "generic_strong": lead(
            run_id,
            "AI assistant for property-tax appeal filings",
            claim("independent property-tax consultants in Cook County", REPORT, COUNTY),
            claim("each appeal packet takes 3 hours of manual comparables work", REPORT),
            claim("Cook County moved appeals to a mandatory e-filing portal in 2025", COUNTY),
            wedge="an AI assistant for comparables packets",
        ),
        "judged_vague": lead(
            run_id,
            "Scheduling for clinics",
            claim("clinics with front desks", FORUM_2),
            claim("double bookings every week", FORUM_2),
            rule,
        ),
        "seen_before": lead(
            run_id,
            "Pool route optimisation",
            claim("pool cleaning companies with 3-10 routes in Phoenix", FORUM),
            claim("routes are planned by hand every Monday", FORUM),
            rule,
        ),
    }


def judge(request: CompletionRequest) -> Any:
    """Specific for everything except the clinic lead; the property-tax lead has
    specificity 5, everything else 4; small-team fit 4 throughout."""
    prompt = request.messages[-1].content
    judgements = []
    for line in prompt.splitlines():
        if not line.startswith("Lead "):
            continue
        number = int(line.split(":")[0].removeprefix("Lead "))
        clinic = "Scheduling for clinics" in line
        tax = "property-tax" in line
        judgements.append(
            {
                "lead": number,
                "customer_specific": not clinic,
                "why_now_specific": True,
                "specificity": 5 if tax else 4,
                "smallteam_fit": 4,
                "why_unsolved": "",
                "wedge": "",
                "reason": "clinics is too broad" if clinic else "specific enough",
            }
        )
    return reply(json.dumps({"judgements": judgements}), model=request.model)


def setup(sessions: sessionmaker[Session]) -> tuple[UUID, UUID, dict[str, Lead]]:
    run = ResearchRun()
    stage = Stage(run_id=run.id, name=StageName.SCREENING)
    agent_run = AgentRun(stage_id=stage.id, agent_name="triage", model_tier=ModelTier.CHEAP)
    leads = build_leads(run.id)
    with sessions() as session:
        RunRepository(session).add(run)
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        store = EvidenceStore(session)
        for s in (FORUM, FORUM_2, GOV, REPORT, COUNTY):
            store.add_source(s)
        for item in leads.values():
            store.save_lead(item)
        session.commit()
    return run.id, agent_run.id, leads


def triage(
    sessions: sessionmaker[Session],
    run_id: UUID,
    agent_run_id: UUID,
    llm: FakeLLMProvider,
    prior: dict[UUID, PriorMatch] | None = None,
) -> AgentRunStatus:
    sink = DbTraceSink(sessions)
    runtime = AgentRuntime(sessions, gateway=LLMGateway(llm, ROUTING, sink), sink=sink)
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run_id)
        sources = EvidenceStore(session).sources_for(c for le in leads for c in le.claims())
    result = asyncio.run(
        runtime.run(
            TriageAgent(leads, sources, prior, batch_size=4),
            agent_run_id,
            budget=BUDGET,
            run_budget=RunBudget(20.0),
        )
    )
    return result.status


def stored(sessions: sessionmaker[Session], run_id: UUID) -> dict[str, Lead]:
    with sessions() as session:
        return {le.title: le for le in LeadRepository(session).list_for_run(run_id)}


def kill_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(models.KillRow)) or 0


def kill_of(sessions: sessionmaker[Session], lead_id: UUID) -> tuple[FailedGate | None, str]:
    with sessions() as session:
        [kill] = KillRepository(session).list_for_subject(lead_id)
    assert kill.stage == "screening"
    return kill.failed_gate, kill.reason


def test_triage_kills_generic_and_unspecific_leads_with_reasons(
    sessions: sessionmaker[Session],
) -> None:
    run_id, agent_run_id, leads = setup(sessions)
    prior = {leads["seen_before"].id: PriorMatch(DedupStatus.KILLED, lead_id=leads["good"].id)}
    llm = FakeLLMProvider(script=[judge, judge])

    assert triage(sessions, run_id, agent_run_id, llm, prior) is AgentRunStatus.COMPLETED

    after = {name: stored(sessions, run_id)[item.title] for name, item in leads.items()}
    assert {n for n, le in after.items() if le.status is LeadStatus.KEPT} == {
        "good",
        "generic_strong",
    }
    expected = {
        "vague_customer": (FailedGate.NAMED_CUSTOMER, "is not a specific segment"),
        "unsourced_why_now": (FailedGate.WHY_NOW, "why now is not a sourced fact (assumption)"),
        "trend_why_now": (FailedGate.WHY_NOW, "is a general trend"),
        "generic_weak": (FailedGate.GENERIC_PATTERN, "'AI chatbot for X'"),
        "judged_vague": (FailedGate.NAMED_CUSTOMER, "clinics is too broad"),
        "twin": (FailedGate.DUPLICATE, f"duplicate of lead {leads['good'].id}"),
        "seen_before": (FailedGate.DUPLICATE, "from an earlier run (killed)"),
    }
    for name, (gate, text) in expected.items():
        failed_gate, reason = kill_of(sessions, leads[name].id)
        assert failed_gate is gate, name
        assert text in reason, (name, reason)
    assert after["twin"].status is LeadStatus.DUPLICATE
    assert after["generic_weak"].status is LeadStatus.KILLED

    # The generic lead with strong evidence clears the higher bar, flagged.
    strong = after["generic_strong"].triage
    assert strong is not None and strong.generic_flag
    assert strong.generic_pattern == "AI chatbot for X" and strong.source_strength >= 4
    # Ranked by cheap score: property tax (5+5+4)/3 beats HVAC (5+4+4)/3.
    good = after["good"].triage
    assert good is not None and good.wedge == "phone-to-calendar dispatch board"
    assert (strong.rank, good.rank) == (1, 2)

    # Fates are in the cross-run index: survivors active, gate kills killed.
    with sessions() as session:
        index = DedupRepository(session)
        assert index.get(lead_dedup_hash(after["good"])).status is DedupStatus.ACTIVE  # type: ignore[union-attr]
        assert index.get(lead_dedup_hash(after["generic_weak"])).status is DedupStatus.KILLED  # type: ignore[union-attr]

    # Mechanical gates cost nothing: only the four leads that passed them reach the
    # model (one batch of 4), with source excerpts inside the untrusted block.
    assert len(llm.requests) == 1
    prompts = "\n".join(r.messages[-1].content for r in llm.requests)
    assert "Invoicing for everyone" not in prompts and "Dental" not in prompts
    assert "<<<DATA" in prompts and "excerpt of https://www.epa.gov/section608" in prompts


def test_triage_resumes_without_repeating_kills(sessions: sessionmaker[Session]) -> None:
    run_id, agent_run_id, _ = setup(sessions)

    class Crash(BaseException):
        pass

    def crash(request: CompletionRequest) -> Any:
        raise Crash

    with pytest.raises(Crash):
        triage(sessions, run_id, agent_run_id, FakeLLMProvider(script=[crash]))
    with sessions() as session:
        ledger = AgentRunRepository(session).get(agent_run_id).work_ledger  # type: ignore[union-attr]
        kills_before = kill_count(session)
    assert ledger.done == ["screen"] and ledger.pending[-1] == "rank"
    assert kills_before == 4  # three mechanical kills + the within-run duplicate, committed

    llm = FakeLLMProvider(script=[judge, judge])
    assert triage(sessions, run_id, agent_run_id, llm) is AgentRunStatus.COMPLETED
    with sessions() as session:
        assert kill_count(session) == 6  # + the generic decoy and the clinic lead, once each
    kept = [le for le in stored(sessions, run_id).values() if le.status is LeadStatus.KEPT]
    assert len(kept) == 3  # no prior-match this time, so the pool lead survives too


def test_source_strength_counts_independent_sources() -> None:
    run_id = ResearchRun().id
    leads = build_leads(run_id)
    sources = {s.id: s for s in (FORUM, GOV, REPORT, COUNTY)}
    assert source_strength(leads["good"], sources) == 5  # forum + epa: high, 2 independent
    assert source_strength(leads["generic_weak"], sources) == 2  # forum only
    assert source_strength(leads["generic_strong"], sources) == 5
    assert source_strength(leads["unsourced_why_now"], {}) == 1
