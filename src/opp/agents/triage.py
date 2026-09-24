"""TriageAgent: filters leads before any expensive work (specs.md §4.6, §5.2; intent.md §16).

Three sub-task kinds, all resumable:

- ``screen`` (no model call): dedup against the cross-run index and within the run
  (strongest evidence wins), then the mechanical hard gates. Each of the three
  specificity claims must be a sourced ``fact`` (a demoted or unknown claim fails its
  test), and a customer or why-now that is plainly vague ("small businesses", "AI is
  getting better") fails too. Every kill is logged with its gate and reason.
- ``assess:<lead ids>`` (one cheap-tier call per batch): the judgement half of the
  customer and why-now tests, cheap scores, and the scored-but-not-kill tests. A lead
  matching the generic-pattern blocklist survives only with strong sources *and* high
  specificity (the higher bar); otherwise it's killed with the pattern named.
- ``rank`` (no model call): orders the survivors by cheap score.

The agent never touches the DB: its stage handler passes in the run's leads, their
sources and any cross-run duplicate matches, and the runtime commits what it returns.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.domain.enums import (
    ClaimType,
    DedupStatus,
    FailedGate,
    KillSubject,
    LeadStatus,
    Level,
    ModelTier,
)
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Kill, Lead, TriageAssessment
from opp.evidence.dedup import NEAR_DUPLICATE_THRESHOLD, pick_survivor, similarity
from opp.prompts import load_asset, load_prompts, untrusted_block

PROMPT_VERSION = "v1"
BATCH_SIZE = 8
ASSESS_MAX_TOKENS = 4_000
STAGE = "screening"
# Generic-pattern leads need at least this source strength and specificity to survive.
GENERIC_BAR = 4

_TIER_POINTS = {Level.HIGH: 4, Level.MEDIUM: 3, Level.LOW: 2}


class LeadJudgement(BaseModel):
    lead: int = Field(description="the lead's number in the list")
    customer_specific: bool
    why_now_specific: bool
    specificity: int = Field(ge=1, le=5)
    smallteam_fit: int = Field(ge=1, le=5)
    why_unsolved: str = ""
    wedge: str = ""
    reason: str


class TriageBatch(BaseModel):
    judgements: list[LeadJudgement] = Field(default_factory=list)


@dataclass(frozen=True)
class PriorMatch:
    """A cross-run duplicate found by the handler in the dedup index."""

    status: DedupStatus
    lead_id: UUID | None = None
    opportunity_id: UUID | None = None

    def describe(self) -> str:
        ref = f"lead {self.lead_id}" if self.lead_id else f"opportunity {self.opportunity_id}"
        return f"duplicate of {ref} from an earlier run ({self.status.value})"


class TriageAgent(Agent):
    name: ClassVar[str] = "triage"
    default_tier: ClassVar[ModelTier] = ModelTier.CHEAP
    prompt_version: ClassVar[str] = f"triage/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM})

    def __init__(
        self,
        leads: list[Lead],
        sources: Mapping[UUID, Source],
        prior_matches: Mapping[UUID, PriorMatch] | None = None,
        *,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._leads = {lead.id: lead for lead in sorted(leads, key=_discovery_order)}
        self._sources = sources
        self._prior = dict(prior_matches or {})
        self._batch_size = batch_size
        self._prompts = load_prompts("triage", PROMPT_VERSION)
        gates = load_asset("triage", PROMPT_VERSION, "gates.json")
        self._generic = [
            (g["name"], re.compile(g["regex"], re.I)) for g in gates["generic_patterns"]
        ]
        self._vague_customers = {_normalise(c) for c in gates["vague_customers"]}
        self._vague_why_now = [re.compile(p, re.I) for p in gates["vague_why_now"]]

    @property
    def leads(self) -> list[Lead]:
        return list(self._leads.values())

    def plan(self, ctx: AgentContext) -> list[str]:
        # ``screen`` queues the assess batches and then ``rank`` as follow-ups.
        return ["screen"]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        if task == "screen":
            return self._screen()
        if task == "rank":
            return self._rank()
        if task.startswith("assess:"):
            ids = [UUID(part) for part in task.removeprefix("assess:").split(",")]
            return await self._assess([self._leads[i] for i in ids if i in self._leads], ctx)
        raise ValueError(f"unknown triage task {task!r}")

    # --- screen: dedup + mechanical gates ----------------------------------------------

    def _screen(self) -> SubTaskResult:
        result = SubTaskResult()
        passed: list[Lead] = []
        for lead in self._leads.values():
            if lead.status is not LeadStatus.NEW:
                continue  # decided earlier (a re-run after a crash, or an earlier stage)
            if (prior := self._prior.get(lead.id)) is not None:
                self._drop_duplicate(lead, prior.describe(), result)
                continue
            if failures := self._gate_failures(lead):
                self._kill(lead, failures, result)
                continue
            twin = next(
                (p for p in passed if similarity(lead, p) >= NEAR_DUPLICATE_THRESHOLD), None
            )
            if twin is not None:
                pair = pick_survivor(twin, lead, self._sources)
                self._drop_duplicate(pair.drop, f"duplicate of lead {pair.keep.id}", result)
                if pair.keep is lead:
                    passed[passed.index(twin)] = lead
                continue
            passed.append(lead)

        batches = [
            passed[i : i + self._batch_size] for i in range(0, len(passed), self._batch_size)
        ]
        result.follow_up_tasks = [
            "assess:" + ",".join(str(lead.id) for lead in batch) for batch in batches
        ] + ["rank"]
        killed = sum(1 for lead in result.leads if lead.status is LeadStatus.KILLED)
        dupes = len(result.leads) - killed
        result.note = (
            f"screen: {len(passed)} passed the mechanical gates, {killed} killed, "
            f"{dupes} duplicate(s); {len(batches)} batch(es) to assess"
        )
        return result

    def _gate_failures(self, lead: Lead) -> list[tuple[FailedGate, str]]:
        failures: list[tuple[FailedGate, str]] = []
        tests = (
            (FailedGate.NAMED_CUSTOMER, lead.named_customer, "named customer"),
            (FailedGate.SPECIFIC_PAIN, lead.specific_pain, "specific pain"),
            (FailedGate.WHY_NOW, lead.why_now, "why now"),
        )
        for gate, claim, label in tests:
            if not _is_sourced_fact(claim):
                failures.append((gate, f"{label} is not a sourced fact ({claim.type.value})"))
        if not any(g is FailedGate.NAMED_CUSTOMER for g, _ in failures) and (
            _normalise(lead.named_customer.statement) in self._vague_customers
        ):
            failures.append(
                (
                    FailedGate.NAMED_CUSTOMER,
                    f"customer {lead.named_customer.statement!r} is not a specific segment",
                )
            )
        if not any(g is FailedGate.WHY_NOW for g, _ in failures) and any(
            p.search(lead.why_now.statement) for p in self._vague_why_now
        ):
            failures.append(
                (FailedGate.WHY_NOW, f"why-now {lead.why_now.statement!r} is a general trend")
            )
        return failures

    # --- assess: the judgement half + cheap scores -------------------------------------

    async def _assess(self, batch: list[Lead], ctx: AgentContext) -> SubTaskResult:
        batch = [lead for lead in batch if lead.status is LeadStatus.NEW]
        if not batch:
            return SubTaskResult(note="assess: nothing left in this batch")
        excerpts: list[str] = []
        excerpt_no: dict[UUID, int] = {}
        for lead in batch:
            # Claim order, then each claim's own citation order: stable across runs, so
            # prompts (and cassettes) don't depend on random ids.
            for sid in (sid for claim in lead.claims() for sid in claim.source_ids):
                if sid in self._sources and sid not in excerpt_no:
                    source = self._sources[sid]
                    excerpts.append(f"{source.title} ({source.url}): {source.excerpt}")
                    excerpt_no[sid] = len(excerpts)
        reply = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                scope=describe_scope(ctx.run_config),
                leads="\n\n".join(
                    _describe(n, lead, excerpt_no) for n, lead in enumerate(batch, start=1)
                ),
                excerpts=untrusted_block(excerpts) if excerpts else "(none)",
            ),
            output_type=TriageBatch,
            max_tokens=ASSESS_MAX_TOKENS,
        )
        judged = {j.lead: j for j in reply.output.judgements if 1 <= j.lead <= len(batch)}

        result = SubTaskResult()
        kept = 0
        for n, lead in enumerate(batch, start=1):
            judgement = judged.get(n)
            if judgement is None:
                judgement = LeadJudgement(
                    lead=n,
                    customer_specific=True,
                    why_now_specific=True,
                    specificity=2,
                    smallteam_fit=2,
                    reason="not assessed by the model; scored low",
                )
            if self._decide(lead, judgement, result):
                kept += 1
        result.note = f"assess: {kept} of {len(batch)} kept"
        return result

    def _decide(self, lead: Lead, judgement: LeadJudgement, result: SubTaskResult) -> bool:
        failures: list[tuple[FailedGate, str]] = []
        if not judgement.customer_specific:
            failures.append(
                (
                    FailedGate.NAMED_CUSTOMER,
                    f"customer is not a specific segment: {judgement.reason}",
                )
            )
        if not judgement.why_now_specific:
            failures.append(
                (FailedGate.WHY_NOW, f"why-now is not a specific recent change: {judgement.reason}")
            )
        if failures:
            self._kill(lead, failures, result)
            return False

        strength = source_strength(lead, self._sources)
        pattern = self.generic_pattern(lead)
        if pattern is not None and (strength < GENERIC_BAR or judgement.specificity < GENERIC_BAR):
            self._kill(
                lead,
                [
                    (
                        FailedGate.GENERIC_PATTERN,
                        f"matches the generic pattern {pattern!r} without the strong evidence "
                        f"it needs (source strength {strength}, specificity "
                        f"{judgement.specificity}; both must be >= {GENERIC_BAR})",
                    )
                ],
                result,
            )
            return False

        updated = lead.model_copy(deep=True)
        updated.status = LeadStatus.KEPT
        updated.triage = TriageAssessment(
            source_strength=strength,
            specificity=judgement.specificity,
            smallteam_fit=judgement.smallteam_fit,
            cheap_score=round((strength + judgement.specificity + judgement.smallteam_fit) / 3, 2),
            generic_flag=pattern is not None,
            generic_pattern=pattern,
            why_unsolved=judgement.why_unsolved.strip(),
            wedge=judgement.wedge.strip() or lead.candidate_wedge,
            reason=judgement.reason.strip() or "passed triage",
        )
        self._store(updated, result, DedupStatus.ACTIVE)
        return True

    def generic_pattern(self, lead: Lead) -> str | None:
        text = " ".join((lead.title, lead.candidate_wedge, lead.specific_pain.statement))
        return next((name for name, regex in self._generic if regex.search(text)), None)

    # --- rank ---------------------------------------------------------------------------

    def _rank(self) -> SubTaskResult:
        kept = [lead for lead in self._leads.values() if lead.status is LeadStatus.KEPT]
        kept.sort(
            key=lambda lead: (
                -(lead.triage.cheap_score if lead.triage else 0),
                -(lead.triage.source_strength if lead.triage else 0),
                lead.discovered_at,
            )
        )
        result = SubTaskResult(note=f"rank: {len(kept)} survivor(s) ranked")
        for rank, lead in enumerate(kept, start=1):
            if lead.triage is None or lead.triage.rank == rank:
                continue
            updated = lead.model_copy(deep=True)
            assert updated.triage is not None
            updated.triage.rank = rank
            self._store(updated, result)
        return result

    # --- helpers ------------------------------------------------------------------------

    def _kill(
        self, lead: Lead, failures: list[tuple[FailedGate, str]], result: SubTaskResult
    ) -> None:
        updated = lead.model_copy(deep=True)
        updated.status = LeadStatus.KILLED
        result.kills.append(
            Kill(
                subject_kind=KillSubject.LEAD,
                subject_id=lead.id,
                stage=STAGE,
                reason="; ".join(reason for _, reason in failures),
                failed_gate=failures[0][0],
            )
        )
        self._store(updated, result, DedupStatus.KILLED)

    def _drop_duplicate(self, lead: Lead, reason: str, result: SubTaskResult) -> None:
        updated = lead.model_copy(deep=True)
        updated.status = LeadStatus.DUPLICATE
        result.kills.append(
            Kill(
                subject_kind=KillSubject.LEAD,
                subject_id=lead.id,
                stage=STAGE,
                reason=reason,
                failed_gate=FailedGate.DUPLICATE,
            )
        )
        self._store(updated, result)

    def _store(self, lead: Lead, result: SubTaskResult, index: DedupStatus | None = None) -> None:
        self._leads[lead.id] = lead
        result.leads = [other for other in result.leads if other.id != lead.id] + [lead]
        if index is not None:
            result.index[lead.id] = index


def source_strength(lead: Lead, sources: Mapping[UUID, Source]) -> int:
    """1-5 from the best credibility tier behind the lead, plus one per extra independent
    source (different publisher, or host when there's none)."""
    found = [sources[sid] for sid in lead.source_ids if sid in sources]
    if not found:
        return 1
    best = max(_TIER_POINTS[s.credibility_tier] for s in found)
    independent = {s.publisher or _host(s.url) for s in found}
    return min(5, best + len(independent) - 1)


def _is_sourced_fact(claim: Claim) -> bool:
    return claim.type is ClaimType.FACT and bool(claim.source_ids)


def _normalise(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
    words = [w for w in words if w not in {"the", "a", "an", "all", "any", "most", "many"}]
    return " ".join(words)


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")


def _discovery_order(lead: Lead) -> tuple[str, str, str]:
    """Signal source first: each source's agent finds its leads in a fixed order, but
    parallel agents interleave by timing, and prompts (and so cassettes) must not."""
    return (lead.signal_source.value, lead.discovered_at.isoformat(), str(lead.id))


def _describe(n: int, lead: Lead, excerpt_no: Mapping[UUID, int]) -> str:
    def line(label: str, claim: Claim) -> str:
        cites = sorted(excerpt_no[s] for s in claim.source_ids if s in excerpt_no)
        refs = ", ".join(f"[{c}]" for c in cites) or "no source"
        return f"   {label}: {claim.statement} ({claim.type.value}; {refs})"

    return "\n".join(
        [
            f"Lead {n}: {lead.title}  [{lead.business_shape.value}]",
            line("customer", lead.named_customer),
            line("pain", lead.specific_pain),
            line("why now", lead.why_now),
            f"   wedge: {lead.candidate_wedge or '(none given)'}",
        ]
    )
