"""The golden opportunity set (plan.md §21): a small curated benchmark of seeded
scenarios — a healthy real-signal run, a planted fatal flaw, and a field of generic
decoys — each with the outcome the funnel must produce. Scenarios are configurations of
the shared funnel fakes (``tests/agents/fakes.py``), so they run offline and deterministically."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from tests.agents.fakes import FullFunnelLLM

from opp.domain.enums import (
    CheckpointMode,
    RunStatus,
    SignalSource,
    Verdict,
)
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.fake import reply
from opp.llm.types import Completion, CompletionRequest

_QUERY = re.compile(r"Search query used: (.+)")


@dataclass
class GenericDecoyLLM(FullFunnelLLM):
    """Discovery emits only *sourced but generic* leads — they cite real snippets (so the
    "no lead without a source" guard keeps them) yet name a vague customer and a
    trend-not-event why-now, so triage's specificity hard-gates and the generic blocklist
    must kill them. The rest of the funnel is inherited but never reached: nothing survives."""

    async def complete(self, request: CompletionRequest) -> Completion:
        if request.output_type and request.output_type.__name__ == "DiscoveryExtraction":
            self.requests.append(request)
            match = _QUERY.search(request.messages[-1].content)
            query = match.group(1).strip() if match else "?"
            leads = [
                {
                    "title": f"AI chatbot for small businesses ({query})",
                    "business_shape": "saas",
                    "named_customer": "small businesses",
                    "customer_evidence": [1],
                    "pain": "they want an AI assistant",
                    "pain_evidence": [1],
                    "why_now": "AI is getting better",
                    "why_now_evidence": [2],
                    "wedge": "a chatbot",
                    "confidence": "low",
                    "confidence_reason": "a forum mention",
                },
                {
                    "title": f"Uber for local services ({query})",
                    "business_shape": "marketplace",
                    "named_customer": "everyone",
                    "customer_evidence": [1],
                    "pain": "booking is inconvenient",
                    "pain_evidence": [1],
                    "why_now": "the gig economy is growing",
                    "why_now_evidence": [2],
                    "wedge": "a marketplace",
                    "confidence": "low",
                    "confidence_reason": "a hunch",
                },
            ]
            return reply(
                json.dumps({"leads": leads, "note": query}),
                model=request.model,
                input_tokens=800,
                output_tokens=150,
            )
        return await super().complete(request)


@dataclass(frozen=True)
class Golden:
    """One benchmark scenario: how to build its funnel LLM, the run to drive, and the
    outcome the funnel must produce."""

    name: str
    description: str
    make_llm: Callable[[], FullFunnelLLM]
    config: RunConfig
    expect_status: RunStatus
    expect_verdict: Verdict | None
    expect_opportunity: bool
    expect_fatal_flaw: bool
    expect_anti_generic_kills: bool


def _config(mode: CheckpointMode = CheckpointMode.AUTONOMOUS) -> RunConfig:
    return RunConfig(
        discovery_scope=DiscoveryScope(
            industry="HVAC",
            signal_sources=[SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS],
        ),
        lead_targets=LeadTargets(discovery_n=6),
        checkpoint_mode=mode,
    )


GOLDENS: list[Golden] = [
    Golden(
        name="healthy_hvac",
        description="Real, sourced pain among small HVAC shops → a full decision.",
        make_llm=FullFunnelLLM,
        config=_config(),
        expect_status=RunStatus.COMPLETED,
        expect_verdict=Verdict.PROMISING_BUT_UNPROVEN,
        expect_opportunity=True,
        expect_fatal_flaw=False,
        expect_anti_generic_kills=False,
    ),
    Golden(
        name="planted_fatal_flaw",
        description="A strong-looking plan the judge would pass, but the red-team lands a "
        "well-supported fatal flaw → the override forces Kill.",
        # The LLM proposes 'pursue' with top scores; the fatal flaw must override it.
        make_llm=lambda: FullFunnelLLM(fatal=True, judge_score=5, verdict="pursue"),
        config=_config(),
        expect_status=RunStatus.COMPLETED,
        expect_verdict=Verdict.KILL,
        expect_opportunity=True,
        expect_fatal_flaw=True,
        expect_anti_generic_kills=False,
    ),
    Golden(
        name="generic_decoys",
        description="A field of sourced-but-generic leads: triage must kill them all "
        "(named-customer / why-now / generic-pattern) and the run ends honestly, no fabrication.",
        make_llm=GenericDecoyLLM,
        config=_config(),
        expect_status=RunStatus.COMPLETED_PARTIAL,
        expect_verdict=None,
        expect_opportunity=False,
        expect_fatal_flaw=False,
        expect_anti_generic_kills=True,
    ),
]
