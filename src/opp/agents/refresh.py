"""RefreshAgent: re-verify an existing opportunity's stale claims against fresh evidence
(specs.md §2.11, §7.8).

One sub-task per stale claim: a fresh snippet-first search for the claim, then one
medium-tier judgement over those snippets deciding whether the claim still holds. The claim
is updated **in place** on the opportunity (its value, type, confidence, sources and
timestamp), keeping its id so everything that references it stays valid, and the whole
opportunity is written back each sub-task. Nothing is fabricated: a claim the fresh evidence
no longer supports is demoted and its confidence dropped, never kept alive with a stale
figure. Committing per claim means a budget stop or cancel keeps the claims already
re-verified (intent.md §21).
"""

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.domain.base import utcnow
from opp.domain.enums import ClaimType, EvidenceStrength, Level, ModelTier
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Opportunity
from opp.prompts import load_prompts, untrusted_block
from opp.research.search import Freshness, SearchError
from opp.research.sources import source_from_search_result

PROMPT_VERSION = "v1"
RESULTS_PER_QUERY = 6
JUDGEMENT_MAX_TOKENS = 2_000
_REVERIFIABLE = frozenset({ClaimType.FACT, ClaimType.ESTIMATE, ClaimType.ASSUMPTION})


class ClaimUpdate(BaseModel):
    """The refresh contract: whether the claim still holds, and its current shape."""

    status: str = Field(description="confirmed | updated | unsupported")
    statement: str = ""
    kind: ClaimType = ClaimType.FACT
    value: float | None = None
    unit: str | None = None
    snippets: list[int] = Field(default_factory=list)
    confidence: Level = Level.LOW
    confidence_reason: str = ""


def refresh_task(claim_id: UUID) -> str:
    return f"refresh:{claim_id}"


class RefreshAgent(Agent):
    name: ClassVar[str] = "refresh"
    default_tier: ClassVar[ModelTier] = ModelTier.MEDIUM
    prompt_version: ClassVar[str] = f"refresh/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM, CallKind.SEARCH})

    def __init__(
        self,
        opportunity: Opportunity,
        sources: dict[UUID, Source],
        stale_claim_ids: list[UUID],
        *,
        now: datetime | None = None,
    ) -> None:
        self._opp = opportunity
        self._sources = dict(sources)
        self._stale = list(stale_claim_ids)
        self._now = now or utcnow()
        self._new_sources: dict[UUID, Source] = {}
        self._prompts = load_prompts("refresh", PROMPT_VERSION)

    def plan(self, ctx: AgentContext) -> list[str]:
        return [refresh_task(cid) for cid in self._stale]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        claim_id = UUID(task.removeprefix("refresh:"))
        claim = next((c for c in self._opp.claims() if c.id == claim_id), None)
        if claim is None or claim.type not in _REVERIFIABLE:
            return SubTaskResult(note=f"{task}: claim no longer present")

        query = self._query(claim)
        try:
            response = await ctx.tools.search(query, k=RESULTS_PER_QUERY, freshness=Freshness.YEAR)
            snippets = [source_from_search_result(r) for r in response.results]
        except SearchError as exc:
            return SubTaskResult(note=f"{task}: search failed ({exc}); left unchanged")
        if not snippets:
            self._mark_unverified(claim, "no fresh search results")
            return SubTaskResult(
                sources=list(self._new_sources.values()),
                opportunities=[self._opp],
                note=f"{task}: no fresh results; marked unverified",
            )

        result = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                title=self._opp.title,
                age=self._age(claim),
                statement=claim.statement,
                type=claim.type.value,
                confidence=claim.confidence.value,
                value=self._value_text(claim),
                old_evidence=self._old_evidence(claim),
                query=query,
                snippets=untrusted_block(
                    [f"{s.title} ({s.url}): {s.excerpt}" for s in snippets]
                ),
            ),
            output_type=ClaimUpdate,
            max_tokens=JUDGEMENT_MAX_TOKENS,
        )
        note = self._apply(claim, result.output, snippets)
        return SubTaskResult(
            sources=list(self._new_sources.values()),
            opportunities=[self._opp],
            note=f"{task}: {note}",
        )

    def _apply(self, claim: Claim, update: ClaimUpdate, snippets: list[Source]) -> str:
        """Update the claim in place from the judgement; return a one-line note."""
        cited = [snippets[n - 1] for n in dict.fromkeys(update.snippets) if 1 <= n <= len(snippets)]
        for source in cited:
            self._new_sources.setdefault(source.id, source)
        cited_ids = [s.id for s in cited]
        status = update.status.strip().lower()

        if status == "confirmed":
            claim.source_ids = list(dict.fromkeys([*claim.source_ids, *cited_ids]))
            claim.confidence = update.confidence
            claim.corroboration_count = len(claim.source_ids)
            if cited_ids:
                claim.evidence_strength = EvidenceStrength.MODERATE
        elif status == "updated":
            if update.statement.strip():
                claim.statement = update.statement.strip()
            claim.value = update.value
            claim.unit = update.unit
            claim.value_low = claim.value_high = None
            claim.type = update.kind if update.kind in _REVERIFIABLE else ClaimType.ESTIMATE
            claim.source_ids = cited_ids
            claim.confidence = update.confidence
            claim.corroboration_count = len(cited_ids)
        else:  # unsupported / unrecognised: demote honestly, never keep a stale figure
            claim.source_ids = cited_ids
            claim.type = ClaimType.ASSUMPTION if not cited_ids else ClaimType.ESTIMATE
            claim.confidence = Level.LOW
            claim.corroboration_count = len(cited_ids)
            claim.evidence_strength = EvidenceStrength.WEAK
            status = "unsupported"

        reason = update.confidence_reason.strip() or f"re-verified against fresh search ({status})"
        claim.confidence_reason = reason
        claim.observed_at = self._now
        claim.notes = self._note(claim.notes, f"refreshed {self._now.date().isoformat()}: {status}")
        return f"{status} ({claim.type.value}, {claim.confidence.value})"

    def _mark_unverified(self, claim: Claim, why: str) -> None:
        claim.confidence = Level.LOW
        claim.observed_at = self._now
        claim.evidence_strength = EvidenceStrength.WEAK
        claim.confidence_reason = why
        claim.notes = self._note(claim.notes, f"refreshed {self._now.date().isoformat()}: {why}")

    def _query(self, claim: Claim) -> str:
        segment = self._opp.customer.segment.statement if self._opp.customer.segment else ""
        return " ".join(f"{claim.statement} {segment}".split())[:200]

    def _old_evidence(self, claim: Claim) -> str:
        quotes = [
            f"{self._sources[s].url}: {self._sources[s].excerpt}"
            for s in claim.source_ids
            if s in self._sources
        ]
        return untrusted_block(quotes) if quotes else "  (no source; the model's inference)"

    def _age(self, claim: Claim) -> str:
        days = max((self._now - claim.observed_at).days, 0)
        return f"{days} day(s)"

    @staticmethod
    def _value_text(claim: Claim) -> str:
        if claim.value is None:
            return "(not quantitative)"
        return f"{claim.value}{f' {claim.unit}' if claim.unit else ''}"

    @staticmethod
    def _note(existing: str | None, addition: str) -> str:
        return f"{existing}\n{addition}" if existing else addition
