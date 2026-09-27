"""TriageAgent: the cheap filter between discovery and any expensive work (specs.md §5.2,
intent.md §16).

Each sub-task screens one batch of leads:

1. **Specificity hard gates, mechanically.** Named customer, specific pain and why-now
   must each be a sourced ``fact``/``estimate`` claim (not an assumption or unknown), and
   the customer / why-now must not be one of the known-vague forms ("small businesses",
   "AI is getting better").
2. **Generic-pattern blocklist.** "AI chatbot for X", "Uber for X", ... are flagged and
   must clear a higher bar (specs.md §26.5 (a)): the pain backed by at least two
   independent high-credibility sources. A flagged lead that doesn't is killed.
3. **One cheap LLM judgment** per batch for what rules can't see: is the customer really
   narrow, is the pain really documented, is the why-now really specific, plus why-unsolved,
   wedge and 1-5 specificity / small-team-fit scores. The model can only fail a lead, never
   rescue one the rules killed. If the model is unavailable, the rules decide alone.

Every killed lead gets a Kill record with its failed gate(s). Kept leads carry a
``TriageAssessment`` whose ``rank_score`` orders them for shallow validation.
"""

import re
from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.domain.enums import (
    ClaimType,
    FailedGate,
    KillSubject,
    LeadStatus,
    Level,
    ModelTier,
    StageName,
)
from opp.domain.evidence import Claim, Source
from opp.domain.lead import CheapScores, Kill, Lead, TriageAssessment
from opp.evidence.validator import independent_source_count
from opp.llm.gateway import LLMError
from opp.prompts import load_prompts, untrusted_block

PROMPT_VERSION = "v1"
BATCH_SIZE = 8
JUDGMENT_MAX_TOKENS = 4_000

_SOURCED_TYPES = frozenset({ClaimType.FACT, ClaimType.ESTIMATE})
# A customer made only of these words names nobody in particular.
_VAGUE_CUSTOMER_TEXT = """
a an the and or of all any every in on for with who small medium large sized size
smb smbs sme smes local online modern busy business businesses company companies
firm firms organization organizations organisation organisations enterprise
enterprises startup startups team teams people person everyone anyone individual
individuals user users consumer consumers customer customers client clients buyer
buyers owner owners professional professionals worker workers developer developers
creator creators freelancer freelancers entrepreneur entrepreneurs brand brands
shop shops store stores
"""
_VAGUE_CUSTOMER_WORDS = frozenset(_VAGUE_CUSTOMER_TEXT.split())
_VAGUE_WHY_NOW = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ai|a\.i\.|llms?|technology|tech|models?)\s+(is|are)\s+(getting|becoming)\b",
        r"\b(rise|growth|advances?|progress|boom)\s+(of|in)\s+(ai|a\.i\.|llms?|technology)\b",
        r"\bdigital transformation\b",
        r"\bgrowing (demand|interest|need)\b",
        r"\bmore and more\b",
    )
]
GENERIC_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "AI chatbot/assistant for X": r"\b(ai|gpt|llm)[- ]?(powered |based )?"
        r"(chat ?bot|assistant|copilot|agent)s? for\b",
        "Uber for X": r"\buber for\b",
        "Airbnb for X": r"\bairbnb for\b",
        "productivity app": r"\bproductivity (app|tool|platform)\b",
        "app for small businesses": r"\b(app|platform|tool|software) for (small|all) businesses\b",
        "all-in-one platform": r"\ball[- ]in[- ]one (platform|app|solution|suite)\b",
        "social network for X": r"\bsocial (network|platform) for\b",
    }.items()
}
_TIER_POINTS = {Level.HIGH: 2.0, Level.MEDIUM: 1.0, Level.LOW: 0.5}


class LeadJudgment(BaseModel):
    lead: int
    named_customer_specific: bool
    specific_pain_documented: bool
    why_now_specific: bool
    why_unsolved: str = ""
    wedge_specific: bool = False
    specificity: int = Field(ge=1, le=5)
    smallteam_fit: int = Field(ge=1, le=5)
    reason: str


class TriageJudgments(BaseModel):
    """The cheap-tier contract (plan.md §8 TriageDecision, per batch)."""

    judgments: list[LeadJudgment] = Field(default_factory=list)


# --- the mechanical rules (pure) --------------------------------------------------------


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _sourced(claim: Claim) -> bool:
    return claim.type in _SOURCED_TYPES and bool(claim.source_ids)


def gate_failures(lead: Lead) -> list[tuple[FailedGate, str]]:
    """The specificity hard gates (intent.md §16) that fail on the lead's own claims."""
    failures: list[tuple[FailedGate, str]] = []
    customer, pain, why_now = lead.named_customer, lead.specific_pain, lead.why_now
    if not _sourced(customer):
        failures.append((FailedGate.NAMED_CUSTOMER, "the customer claim is not sourced"))
    elif not set(_words(customer.statement)) - _VAGUE_CUSTOMER_WORDS:
        failures.append(
            (FailedGate.NAMED_CUSTOMER, f"{customer.statement!r} names no specific segment")
        )
    if not _sourced(pain):
        failures.append((FailedGate.SPECIFIC_PAIN, "the pain is not backed by a source"))
    if not _sourced(why_now):
        failures.append((FailedGate.WHY_NOW, "no sourced recent change (why now)"))
    elif any(p.search(why_now.statement) for p in _VAGUE_WHY_NOW):
        failures.append((FailedGate.WHY_NOW, f"{why_now.statement!r} is a vague trend"))
    return failures


def generic_pattern(lead: Lead) -> str | None:
    """The blocklisted generic pattern the lead matches, if any (intent.md §16)."""
    text = " ".join(
        (
            lead.title,
            lead.candidate_wedge,
            lead.named_customer.statement,
            lead.specific_pain.statement,
        )
    )
    return next((name for name, p in GENERIC_PATTERNS.items() if p.search(text)), None)


def clears_generic_bar(lead: Lead, sources: Mapping[UUID, Source]) -> bool:
    """specs.md §26.5 (a): the pain backed by ≥2 independent high-credibility sources."""
    strong = [
        sid
        for sid in lead.specific_pain.source_ids
        if sid in sources and sources[sid].credibility_tier is Level.HIGH
    ]
    return independent_source_count(strong, sources) >= 2


def source_strength(lead: Lead, sources: Mapping[UUID, Source]) -> int:
    """1-5 from the credibility of the distinct sources behind the lead's claims."""
    points = sum(
        _TIER_POINTS[sources[sid].credibility_tier] for sid in lead.source_ids if sid in sources
    )
    return max(1, min(5, 1 + int(points + 0.5)))  # round half up


def rank_score(scores: CheapScores, *, wedge_specific: bool, why_unsolved: str) -> float:
    """Orders survivors: mean cheap score, plus a little for the two scored-not-gated tests
    (wedge, why-hasn't-it-been-solved)."""
    base = (scores.source_strength + scores.specificity + scores.smallteam_fit) / 3
    return round(base + 0.25 * wedge_specific + 0.25 * bool(why_unsolved.strip()), 4)


# --- the agent --------------------------------------------------------------------------


def batch_task(lead_ids: list[UUID]) -> str:
    return "batch:" + ",".join(str(i) for i in lead_ids)


def batch_ids(task: str) -> list[UUID]:
    return [UUID(part) for part in task.removeprefix("batch:").split(",") if part]


class TriageAgent(Agent):
    name: ClassVar[str] = "triage"
    default_tier: ClassVar[ModelTier] = ModelTier.CHEAP
    prompt_version: ClassVar[str] = f"triage/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM})

    def __init__(self, leads: Mapping[UUID, Lead], sources: Mapping[UUID, Source]) -> None:
        """``leads``: the run's leads (any status); ``sources``: the sources they cite."""
        self._leads = dict(leads)
        self._sources = dict(sources)
        self._prompts = load_prompts("triage", PROMPT_VERSION)

    def plan(self, ctx: AgentContext) -> list[str]:
        pending = sorted(
            (lead for lead in self._leads.values() if lead.status is LeadStatus.NEW),
            key=lambda lead: (lead.discovered_at, str(lead.id)),
        )
        return [
            batch_task([lead.id for lead in pending[i : i + BATCH_SIZE]])
            for i in range(0, len(pending), BATCH_SIZE)
        ]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        batch = [
            self._leads[i]
            for i in batch_ids(task)
            if i in self._leads and self._leads[i].status is LeadStatus.NEW
        ]
        rule_failures = {lead.id: gate_failures(lead) for lead in batch}
        generic = {lead.id: generic_pattern(lead) for lead in batch}
        for lead in batch:
            if generic[lead.id] and not clears_generic_bar(lead, self._sources):
                rule_failures[lead.id].append(
                    (
                        FailedGate.GENERIC_PATTERN,
                        f"generic pattern ({generic[lead.id]}) without two independent "
                        "high-credibility sources for the pain",
                    )
                )

        to_judge = [lead for lead in batch if not rule_failures[lead.id]]
        judgments: dict[UUID, LeadJudgment] = {}
        note_extra = ""
        if to_judge:
            try:
                judgments = await self._judge(to_judge, ctx)
            except LLMError as exc:  # the rules decide alone; the budget error propagates
                note_extra = f" (model unavailable, rules only: {exc})"

        leads: list[Lead] = []
        kills: list[Kill] = []
        for lead in batch:
            decided, kill = self._decide(
                lead, rule_failures[lead.id], generic[lead.id], judgments.get(lead.id), ctx.stage
            )
            leads.append(decided)
            if kill is not None:
                kills.append(kill)
        kept = sum(lead.status is LeadStatus.KEPT for lead in leads)
        return SubTaskResult(
            leads=leads,
            kills=kills,
            note=f"triaged {len(leads)} lead(s): {kept} kept, {len(kills)} killed{note_extra}",
        )

    async def _judge(self, leads: list[Lead], ctx: AgentContext) -> dict[UUID, LeadJudgment]:
        evidence: list[str] = []
        refs: dict[UUID, int] = {}

        def cite(claim: Claim) -> str:
            numbers = []
            for sid in claim.source_ids:
                if sid not in self._sources:
                    continue
                if sid not in refs:
                    source = self._sources[sid]
                    evidence.append(f"{source.title} ({source.url}): {source.excerpt}")
                    refs[sid] = len(evidence)
                numbers.append(f"E{refs[sid]}")
            return f" [{', '.join(numbers)}]" if numbers else " [no evidence]"

        blocks = []
        for number, lead in enumerate(leads, start=1):
            blocks.append(
                f"Lead {number}: {lead.title} ({lead.business_shape.value})\n"
                f"  customer: {lead.named_customer.statement}{cite(lead.named_customer)}\n"
                f"  pain: {lead.specific_pain.statement}{cite(lead.specific_pain)}\n"
                f"  why now: {lead.why_now.statement}{cite(lead.why_now)}\n"
                f"  wedge: {lead.candidate_wedge or '(none given)'}"
            )
        result = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                scope=describe_scope(ctx.run_config),
                memory=ctx.memory.render() or "(no earlier work in this run)",
                leads="\n\n".join(blocks),
                evidence=untrusted_block(evidence) if evidence else "(none)",
            ),
            output_type=TriageJudgments,
            max_tokens=JUDGMENT_MAX_TOKENS,
        )
        by_number = {j.lead: j for j in result.output.judgments}
        return {
            lead.id: by_number[number]
            for number, lead in enumerate(leads, start=1)
            if number in by_number
        }

    def _decide(
        self,
        lead: Lead,
        failures: list[tuple[FailedGate, str]],
        pattern: str | None,
        judgment: LeadJudgment | None,
        stage: StageName,
    ) -> tuple[Lead, Kill | None]:
        failures = list(failures)
        if not failures and judgment is not None:
            for passed, gate, why in (
                (judgment.named_customer_specific, FailedGate.NAMED_CUSTOMER, "customer too broad"),
                (
                    judgment.specific_pain_documented,
                    FailedGate.SPECIFIC_PAIN,
                    "pain not documented",
                ),
                (judgment.why_now_specific, FailedGate.WHY_NOW, "why-now not specific"),
            ):
                if not passed:
                    failures.append((gate, f"{why}: {judgment.reason}"))

        decided = lead.model_copy(deep=True)
        if failures:
            reason = "; ".join(why for _, why in failures)
            decided.status = LeadStatus.KILLED
            decided.triage = TriageAssessment(
                kept=False,
                failed_gates=[gate for gate, _ in failures],
                generic_pattern=pattern,
                reason=reason,
            )
            kill = Kill(
                subject_kind=KillSubject.LEAD,
                subject_id=lead.id,
                stage=stage.value,
                reason=reason,
                failed_gate=failures[0][0],
            )
            return decided, kill

        # Unjudged (model unavailable or it skipped the lead): rules only, ranked last.
        scores = CheapScores(
            source_strength=source_strength(lead, self._sources),
            specificity=judgment.specificity if judgment else 1,
            smallteam_fit=judgment.smallteam_fit if judgment else 1,
        )
        wedge = judgment.wedge_specific if judgment else False
        unsolved = judgment.why_unsolved if judgment else ""
        decided.status = LeadStatus.KEPT
        decided.triage = TriageAssessment(
            kept=True,
            generic_pattern=pattern,
            cheap_scores=scores,
            wedge_specific=wedge,
            why_unsolved=unsolved.strip(),
            rank_score=rank_score(scores, wedge_specific=wedge, why_unsolved=unsolved),
            reason=(
                judgment.reason
                if judgment
                else "passed the specificity gates on rules alone (no model judgment)"
            ),
        )
        return decided, None
