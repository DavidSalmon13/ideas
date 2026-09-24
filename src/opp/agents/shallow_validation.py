"""ShallowValidationAgent: a rough full-framework score to pick the one (specs.md §5.3).

One ``score:<lead id>`` sub-task per candidate (the triage top-N), then ``rank``. Scoring
a lead spends one corroborating search and at most one page fetch, then one medium-tier
call that records findings from the new evidence (as claims under the lead) and scores
all sixteen dimensions. Scores are then checked mechanically (specs.md §17.4): a
dimension with no supporting claim, or resting only on assumptions, unknowns or
low-confidence claims, is marked ``insufficient`` and capped at 2, and a dimension the
model skipped scores 1. The profile-weighted composite is for ranking only.
"""

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.config.profiles import Profile, weighted_composite
from opp.domain.enums import (
    ClaimType,
    Dimension,
    EvidenceSufficiency,
    LeadStatus,
    Level,
    ModelTier,
)
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead, ProvisionalScore, ShallowAssessment
from opp.prompts import load_prompts, untrusted_block
from opp.research.extractor import extract
from opp.research.fetcher import FetchError
from opp.research.search import SearchResult
from opp.research.sources import source_from_page, source_from_search_result

PROMPT_VERSION = "v1"
RESULTS_PER_QUERY = 6
PAGE_CHARS = 3_000
SCORE_MAX_TOKENS = 6_000
INSUFFICIENT_CAP = 2
QUERY_WORDS = 18


class Finding(BaseModel):
    statement: str
    evidence: list[int] = Field(default_factory=list)


class DimensionAssessment(BaseModel):
    dimension: Dimension
    score: int = Field(ge=1, le=5)
    rationale: str
    evidence: EvidenceSufficiency
    claims: list[str] = Field(default_factory=list, description="labels: C1-C3, F1...")


class ShallowOutput(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    scores: list[DimensionAssessment] = Field(default_factory=list)
    summary: str = ""


class ShallowValidationAgent(Agent):
    name: ClassVar[str] = "shallow_validation"
    default_tier: ClassVar[ModelTier] = ModelTier.MEDIUM
    prompt_version: ClassVar[str] = f"shallow_validation/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset(
        {CallKind.LLM, CallKind.SEARCH, CallKind.FETCH}
    )

    def __init__(
        self,
        candidates: list[Lead],
        sources: Mapping[UUID, Source],
        profile: Profile,
        weights: Mapping[Dimension, float],
        *,
        fetch_pages: bool = True,
    ) -> None:
        self._leads = {lead.id: lead for lead in candidates}
        self._sources = sources
        self._profile = profile
        self._weights = dict(weights)
        self._fetch_pages = fetch_pages
        self._prompts = load_prompts("shallow_validation", PROMPT_VERSION)

    @property
    def leads(self) -> list[Lead]:
        return list(self._leads.values())

    def plan(self, ctx: AgentContext) -> list[str]:
        return [f"score:{lead_id}" for lead_id in self._leads] + ["rank"]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        if task == "rank":
            return self._rank()
        lead = self._leads.get(UUID(task.removeprefix("score:")))
        if lead is None:
            return SubTaskResult(note=f"{task}: no longer a candidate")
        if lead.shallow is not None:
            return SubTaskResult(note=f"{task}: already scored")
        return await self._score(lead, ctx)

    # --- score one lead -----------------------------------------------------------------

    async def _score(self, lead: Lead, ctx: AgentContext) -> SubTaskResult:
        query = corroboration_query(lead)
        response = await ctx.tools.search(query, k=RESULTS_PER_QUERY)
        known = {self._sources[s].url for s in lead.source_ids if s in self._sources}
        results = [r for r in response.results if r.url not in known]
        evidence: list[Source] = [source_from_search_result(r) for r in results]
        items = [f"{r.title} ({r.url}): {r.snippet}" for r in results]

        page = await self._fetch_first(results, ctx)
        if page is not None:
            source, text = page
            evidence.append(source)
            items.append(f"{source.title} ({source.url}), page text: {text}")

        claims = {f"C{n}": claim for n, claim in enumerate(lead.claims(), start=1)}
        reply = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                scope=describe_scope(ctx.run_config),
                profile=self._profile.name,
                memory=ctx.memory.render() or "(no earlier work in this stage)",
                title=lead.title,
                business_shape=lead.business_shape.value,
                wedge=(lead.triage.wedge if lead.triage else "") or lead.candidate_wedge or "-",
                why_unsolved=(lead.triage.why_unsolved if lead.triage else "") or "not shown",
                claims="\n".join(
                    f"{label}: {c.statement} ({c.type.value}, {c.confidence.value} confidence)"
                    for label, c in claims.items()
                ),
                query=query,
                evidence=untrusted_block(items) if items else "(the search found nothing new)",
            ),
            output_type=ShallowOutput,
            max_tokens=SCORE_MAX_TOKENS,
        )
        out = reply.output

        cited: dict[int, Source] = {}
        findings: list[Claim] = []
        for n, finding in enumerate(out.findings, start=1):
            refs = [i for i in dict.fromkeys(finding.evidence) if 1 <= i <= len(evidence)]
            for i in refs:
                cited[i] = evidence[i - 1]
            claim = Claim(
                statement=finding.statement.strip() or f"finding {n}",
                type=ClaimType.FACT if refs else ClaimType.ASSUMPTION,
                source_ids=[evidence[i - 1].id for i in refs],
                confidence=Level.MEDIUM if refs else Level.LOW,
                confidence_reason="stated in the corroborating evidence"
                if refs
                else "not backed by the new evidence",
                agent=self.name,
                stage=ctx.stage.value,
            )
            claims[f"F{n}"] = claim
            findings.append(claim)

        scores = self.provisional_scores(out.scores, claims)
        updated = lead.model_copy(deep=True)
        updated.status = LeadStatus.SHORTLISTED
        updated.shallow = ShallowAssessment(
            dimension_scores=scores,
            provisional_composite=weighted_composite(
                ((s.dimension, s.score) for s in scores), self._weights
            ),
            weights_ref=self._profile.id,
            summary=out.summary.strip(),
        )
        self._leads[lead.id] = updated
        return SubTaskResult(
            sources=list(cited.values()),
            claims=findings,
            lead_id=lead.id,
            leads=[updated],
            note=(
                f"scored {lead.title!r}: composite {updated.shallow.provisional_composite:.2f}, "
                f"{len(findings)} finding(s)"
            ),
        )

    async def _fetch_first(
        self, results: list[SearchResult], ctx: AgentContext
    ) -> tuple[Source, str] | None:
        """At most one page per lead, and only while the fetch budget lasts."""
        budget = ctx.budget
        if not (self._fetch_pages and results):
            return None
        if budget.calls[CallKind.FETCH] >= budget.limits.limit(CallKind.FETCH):
            return None
        try:
            raw = await ctx.tools.fetch(results[0].url)
        except FetchError as exc:
            ctx.memory.remember(f"fetch of {results[0].url} failed: {exc}")
            return None
        page = extract(raw)
        if page.is_empty:
            return None
        text = " ".join(page.text.split())[:PAGE_CHARS]
        return source_from_page(page, excerpt=text), text

    @staticmethod
    def provisional_scores(
        assessed: list[DimensionAssessment], claims: Mapping[str, Claim]
    ) -> list[ProvisionalScore]:
        by_dimension: dict[Dimension, DimensionAssessment] = {}
        for item in assessed:
            by_dimension.setdefault(item.dimension, item)
        scores: list[ProvisionalScore] = []
        for dimension in Dimension:
            found = by_dimension.get(dimension)
            if found is None:
                scores.append(
                    ProvisionalScore(
                        dimension=dimension,
                        score=1,
                        rationale="not assessed: no evidence bearing on it",
                        evidence=EvidenceSufficiency.INSUFFICIENT,
                    )
                )
                continue
            support = [claims[label] for label in dict.fromkeys(found.claims) if label in claims]
            score, sufficiency = found.score, found.evidence
            if not any(_is_solid(c) for c in support):
                sufficiency = EvidenceSufficiency.INSUFFICIENT
            if sufficiency is EvidenceSufficiency.INSUFFICIENT:
                score = min(score, INSUFFICIENT_CAP)
            scores.append(
                ProvisionalScore(
                    dimension=dimension,
                    score=score,
                    rationale=found.rationale.strip() or "no rationale given",
                    evidence=sufficiency,
                    supporting_claim_ids=[c.id for c in support],
                )
            )
        return scores

    # --- rank ---------------------------------------------------------------------------

    def _rank(self) -> SubTaskResult:
        scored = [lead for lead in self._leads.values() if lead.shallow is not None]
        scored.sort(
            key=lambda lead: (
                -(lead.shallow.provisional_composite if lead.shallow else 0),
                lead.triage.rank if lead.triage and lead.triage.rank else 10**6,
            )
        )
        result = SubTaskResult(note=f"rank: {len(scored)} candidate(s) ranked")
        for rank, lead in enumerate(scored, start=1):
            updated = lead.model_copy(deep=True)
            assert updated.shallow is not None
            updated.shallow.rank = rank
            self._leads[lead.id] = updated
            result.leads.append(updated)
        return result


def corroboration_query(lead: Lead) -> str:
    """One query aimed at what the rough score needs most: who else serves this customer
    for this pain, and at what price."""
    words = f"{lead.named_customer.statement} {lead.specific_pain.statement}".split()
    return " ".join([*words[:QUERY_WORDS], "software", "pricing"])


def _is_solid(claim: Claim) -> bool:
    return claim.type in (ClaimType.FACT, ClaimType.ESTIMATE) and claim.confidence is not Level.LOW
