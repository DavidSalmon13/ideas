"""Triage: mechanical specificity gates, the generic blocklist's higher bar, and the cheap
model judgment that can fail a lead but never rescue one."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.runtime import AgentRuntime
from opp.agents.triage import (
    TriageAgent,
    clears_generic_bar,
    gate_failures,
    generic_pattern,
    rank_score,
    source_strength,
)
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    KillRepository,
    LeadRepository,
    RunRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import ClaimType, FailedGate, LeadStatus, Level, ModelTier, StageName
from opp.domain.lead import CheapScores, Lead
from opp.domain.run import AgentRun, ResearchRun, Stage
from opp.evidence.store import EvidenceStore
from opp.llm.fake import reply
from opp.llm.gateway import LLMGateway
from opp.llm.types import Completion, CompletionRequest, ProviderError
from opp.observability.traces import DbTraceSink
from tests.agents.test_discovery import ROUTING
from tests.evidence.conftest import make_claim, make_lead, make_source

FORUM = make_source("https://www.reddit.com/r/HVAC/1", credibility_tier=Level.LOW)
RULE = make_source("https://www.epa.gov/rule", credibility_tier=Level.HIGH, publisher="EPA")
REPORT = make_source("https://www.ibisworld.com/x", credibility_tier=Level.HIGH, publisher="IBIS")


def lead(title: str = "Dispatch board for small HVAC shops", **kwargs: Any) -> Lead:
    base = make_lead(title=title, sources=[FORUM])
    base.why_now = make_claim("EPA refrigerant tracking rule from January 2026", sources=[RULE])
    return base.model_copy(update=kwargs)


SOURCES = {s.id: s for s in (FORUM, RULE, REPORT)}
CUSTOMER, PAIN, WHY_NOW = FailedGate.NAMED_CUSTOMER, FailedGate.SPECIFIC_PAIN, FailedGate.WHY_NOW


# --- rules ------------------------------------------------------------------------------


def test_a_specific_sourced_lead_passes_the_gates() -> None:
    assert gate_failures(lead()) == []


@pytest.mark.parametrize(
    ("change", "gate"),
    [
        ({"named_customer": make_claim("small businesses", sources=[FORUM])}, CUSTOMER),
        ({"named_customer": make_claim("Developers and startups", sources=[FORUM])}, CUSTOMER),
        ({"named_customer": make_claim("HVAC contractors")}, CUSTOMER),  # unsourced
        ({"specific_pain": make_claim("they hate paperwork", ClaimType.ASSUMPTION)}, PAIN),
        ({"why_now": make_claim("no recent change found", ClaimType.UNKNOWN)}, WHY_NOW),
        ({"why_now": make_claim("AI is getting better every month", sources=[RULE])}, WHY_NOW),
        ({"why_now": make_claim("the rise of AI in trades", sources=[RULE])}, WHY_NOW),
    ],
)  # fmt: skip
def test_each_gate_fails_on_its_own(change: dict[str, Any], gate: FailedGate) -> None:
    assert [g for g, _ in gate_failures(lead(**change))] == [gate]


def test_generic_patterns_are_flagged_and_need_the_higher_bar() -> None:
    decoy = lead("AI chatbot for dentists")
    assert generic_pattern(decoy) == "AI chatbot/assistant for X"
    assert generic_pattern(lead(candidate_wedge="an Uber for plumbers")) == "Uber for X"
    assert generic_pattern(lead()) is None

    assert not clears_generic_bar(decoy, SOURCES)  # one low-tier forum post
    strong = decoy.model_copy(
        update={"specific_pain": make_claim("front desks lose calls", sources=[RULE, REPORT])}
    )
    assert clears_generic_bar(strong, SOURCES)
    same_publisher = make_source(
        "https://www.epa.gov/other", credibility_tier=Level.HIGH, publisher="EPA"
    )
    echo = decoy.model_copy(
        update={"specific_pain": make_claim("x", sources=[RULE, same_publisher])}
    )
    assert not clears_generic_bar(echo, {**SOURCES, same_publisher.id: same_publisher})


def test_cheap_scores() -> None:
    assert source_strength(lead(), SOURCES) == 4  # forum (0.5) + regulator (2) -> 1 + 2
    assert (
        source_strength(
            lead(
                named_customer=make_claim("x"),
                specific_pain=make_claim("y"),
                why_now=make_claim("z"),
            ),
            SOURCES,
        )
        == 1
    )
    scores = CheapScores(source_strength=3, specificity=4, smallteam_fit=5)
    assert rank_score(scores, wedge_specific=False, why_unsolved="") == 4.0
    assert rank_score(scores, wedge_specific=True, why_unsolved="incumbents ignore them") == 4.5


# --- the agent --------------------------------------------------------------------------


@dataclass
class JudgeLLM:
    """Passes every lead except those whose title says 'vague'; can be made to fail."""

    name: str = "judge"
    requests: list[CompletionRequest] = field(default_factory=list)
    down: bool = False

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self.down:
            raise ProviderError("overloaded", retryable=False)
        prompt = request.messages[-1].content
        judgments = []
        for line in prompt.splitlines():
            if line.startswith("Lead ") and ":" in line:
                number = int(line.split()[1].rstrip(":"))
                vague = "vague" in line.lower()
                judgments.append(
                    {
                        "lead": number,
                        "named_customer_specific": not vague,
                        "specific_pain_documented": True,
                        "why_now_specific": True,
                        "why_unsolved": "" if vague else "incumbents sell to big fleets",
                        "wedge_specific": not vague,
                        "specificity": 2 if vague else 5,
                        "smallteam_fit": 4,
                        "reason": "too broad a segment" if vague else "narrow and sourced",
                    }
                )
        return reply(json.dumps({"judgments": judgments}), model=request.model)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'triage.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def run_triage(
    sessions: sessionmaker[Session], leads: list[Lead], llm: JudgeLLM
) -> tuple[ResearchRun, dict[str, Lead]]:
    run = ResearchRun()
    stage = Stage(run_id=run.id, name=StageName.SCREENING)
    agent_run = AgentRun(stage_id=stage.id, agent_name="triage", model_tier=ModelTier.CHEAP)
    with sessions() as session:
        RunRepository(session).add(run)
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        store = EvidenceStore(session)
        for source in SOURCES.values():
            store.add_source(source)
        for item in leads:
            item.run_id = run.id
            store.save_lead(item)
        session.commit()
        stored = {lead.id: lead for lead in LeadRepository(session).list_for_run(run.id)}
    sink = DbTraceSink(sessions)
    runtime = AgentRuntime(sessions, gateway=LLMGateway(llm, ROUTING, sink), sink=sink)
    asyncio.run(
        runtime.run(
            TriageAgent(stored, SOURCES),
            agent_run.id,
            budget=ResearchBudget(max_cost_usd=1.0),
            run_budget=RunBudget(20),
        )
    )
    with sessions() as session:
        after = {lead.title: lead for lead in LeadRepository(session).list_for_run(run.id)}
    return run, after


def kills_for(sessions: sessionmaker[Session], item: Lead) -> list[FailedGate | None]:
    with sessions() as session:
        return [k.failed_gate for k in KillRepository(session).list_for_subject(item.id)]


def test_triage_keeps_specific_leads_and_kills_generic_ones_with_reasons(
    sessions: sessionmaker[Session],
) -> None:
    llm = JudgeLLM()
    leads = [
        lead(),
        lead("AI chatbot for small businesses", named_customer=make_claim("small businesses")),
        lead("AI chatbot for dental front desks"),
        lead("Vague scheduling helper"),
    ]  # fmt: skip
    _, after = run_triage(sessions, leads, llm)

    good = after["Dispatch board for small HVAC shops"]
    assert good.status is LeadStatus.KEPT
    assert good.triage is not None and good.triage.cheap_scores is not None
    assert good.triage.cheap_scores.specificity == 5
    assert good.triage.rank_score == pytest.approx((4 + 5 + 4) / 3 + 0.5, abs=1e-3)

    both = after["AI chatbot for small businesses"]
    assert both.status is LeadStatus.KILLED
    assert both.triage is not None and set(both.triage.failed_gates) == {
        FailedGate.NAMED_CUSTOMER,
        FailedGate.GENERIC_PATTERN,
    }
    assert "the customer claim is not sourced" in both.triage.reason
    assert kills_for(sessions, both) == [FailedGate.NAMED_CUSTOMER]

    decoy = after["AI chatbot for dental front desks"]  # specific, but generic w/o the bar
    assert decoy.status is LeadStatus.KILLED
    assert kills_for(sessions, decoy) == [FailedGate.GENERIC_PATTERN]

    vague = after["Vague scheduling helper"]  # passes the rules, fails the model
    assert vague.status is LeadStatus.KILLED
    assert kills_for(sessions, vague) == [FailedGate.NAMED_CUSTOMER]
    assert "too broad a segment" in (vague.triage.reason if vague.triage else "")

    # Rule-killed leads never reach the model: it can fail leads, not rescue them.
    prompt = llm.requests[0].messages[-1].content
    assert "AI chatbot" not in prompt
    assert "Dispatch board" in prompt and "Vague scheduling helper" in prompt


def test_rules_decide_alone_when_the_model_is_down(sessions: sessionmaker[Session]) -> None:
    _, after = run_triage(sessions, [lead(), lead("Vague scheduling helper")], JudgeLLM(down=True))

    for item in after.values():
        assert item.status is LeadStatus.KEPT
        assert item.triage is not None and item.triage.cheap_scores is not None
        assert item.triage.cheap_scores.specificity == 1  # unjudged ranks last
        assert "rules alone" in item.triage.reason
