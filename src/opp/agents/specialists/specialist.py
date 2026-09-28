"""SpecialistAgent: one deep-research axis for the single selected opportunity (specs.md §5.4).

One sub-task per research question: a snippet-first search shaped by the opportunity, then
one medium-tier extraction over those snippets. Cited findings become ``fact``/``estimate``
claims; the model's own inferences are kept as ``assumption`` claims (transparent, never
dressed up as facts); a question the search can't answer records an ``unknown`` claim, so
"looked, found nothing" is first-class (intent.md §4). Claims are filed loose under the
opportunity, so the four specialists running in parallel never overwrite one another.
"""

from typing import ClassVar

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.agents.specialists.axes import SPECIALIST, Axis
from opp.domain.enums import ClaimType, Level, ModelTier
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Opportunity
from opp.prompts import load_prompts, untrusted_block
from opp.research.search import SearchError
from opp.research.sources import source_from_search_result

PROMPT_VERSION = "v1"
RESULTS_PER_QUERY = 6
MAX_FINDINGS = 4
EXTRACTION_MAX_TOKENS = 6_000
# The model reports these; anything else is treated as an assumption (never a fact).
_CLAIM_TYPES = frozenset({ClaimType.FACT, ClaimType.ESTIMATE, ClaimType.ASSUMPTION})


class SpecialistFinding(BaseModel):
    statement: str
    snippets: list[int] = Field(default_factory=list)
    kind: ClaimType = ClaimType.FACT
    confidence: Level
    confidence_reason: str


class SpecialistExtraction(BaseModel):
    """One axis question answered from the snippets (plan.md §8 specialist claims)."""

    findings: list[SpecialistFinding] = Field(default_factory=list)
    note: str = ""


def question_task(key: str, index: int) -> str:
    return f"{key}:q{index}"


class SpecialistAgent(Agent):
    name: ClassVar[str] = SPECIALIST
    default_tier: ClassVar[ModelTier] = ModelTier.MEDIUM
    prompt_version: ClassVar[str] = f"deep_research/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM, CallKind.SEARCH})

    def __init__(self, axis: Axis, opportunity: Opportunity) -> None:
        self._axis = axis
        self._opp = opportunity
        self._prompts = load_prompts("deep_research", PROMPT_VERSION)

    def plan(self, ctx: AgentContext) -> list[str]:
        return [question_task(self._axis.key, i) for i in range(len(self._axis.questions))]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        question = self._axis.questions[int(task.split(":q")[1])]
        query = self._query(question)
        try:
            response = await ctx.tools.search(query, k=RESULTS_PER_QUERY)
            snippets = [source_from_search_result(r) for r in response.results]
        except SearchError as exc:
            return SubTaskResult(
                claims=[self._unknown(question, f"search failed: {exc}", ctx)],
                opportunity_id=self._opp.id,
                note=f"{task}: search failed ({exc}); recorded unknown",
            )
        if not snippets:
            return SubTaskResult(
                claims=[self._unknown(question, "no search results", ctx)],
                opportunity_id=self._opp.id,
                note=f"{task}: no results for {query!r}; recorded unknown",
            )

        result = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                axis=self._axis.key,
                focus=self._axis.focus,
                scope=describe_scope(ctx.run_config),
                memory=ctx.memory.render() or "(no earlier work in this run)",
                title=self._opp.title,
                customer=self._claim_text(self._opp.customer.segment),
                pain=self._claim_text(self._opp.problem.description),
                why_now=self._claim_text(self._opp.why_now),
                question=question,
                query=query,
                snippets=untrusted_block([f"{s.title} ({s.url}): {s.excerpt}" for s in snippets]),
            ),
            output_type=SpecialistExtraction,
            max_tokens=EXTRACTION_MAX_TOKENS,
        )

        claims, cited = self._claims(result.output.findings, snippets, question, ctx)
        note = f"{task} ({self._axis.key}): {len(claims)} claim(s)"
        if result.output.note:
            note += f". {result.output.note}"
        return SubTaskResult(
            sources=list(cited.values()),
            claims=claims,
            opportunity_id=self._opp.id,
            note=note,
        )

    def _query(self, question: str) -> str:
        context = " ".join(
            part
            for part in (
                self._opp.customer.segment.statement if self._opp.customer.segment else "",
                self._opp.problem.description.statement if self._opp.problem.description else "",
            )
            if part
        )
        return " ".join(f"{question} {context}".split())[:200]

    def _claims(
        self,
        findings: list[SpecialistFinding],
        snippets: list[Source],
        question: str,
        ctx: AgentContext,
    ) -> tuple[list[Claim], dict[int, Source]]:
        claims: list[Claim] = []
        cited: dict[int, Source] = {}
        for finding in findings:
            if not finding.statement.strip() or len(claims) >= MAX_FINDINGS:
                continue
            refs = [n for n in dict.fromkeys(finding.snippets) if 1 <= n <= len(snippets)]
            for n in refs:
                cited[n] = snippets[n - 1]
            kind = finding.kind if finding.kind in _CLAIM_TYPES else ClaimType.FACT
            # No citation means it's the model's own inference: an assumption, not a fact.
            if not refs:
                kind = ClaimType.ASSUMPTION
            claims.append(
                Claim(
                    statement=finding.statement.strip(),
                    type=kind,
                    source_ids=[snippets[n - 1].id for n in refs],
                    confidence=finding.confidence if refs else Level.LOW,
                    confidence_reason=finding.confidence_reason.strip() or "from search snippets",
                    agent=self._axis.agent_name,
                    stage=ctx.stage.value,
                )
            )
        if not claims:
            claims.append(self._unknown(question, "the snippets showed nothing usable", ctx))
        return claims, cited

    def _unknown(self, question: str, why: str, ctx: AgentContext) -> Claim:
        return Claim(
            statement=f"Could not determine: {question}",
            type=ClaimType.UNKNOWN,
            confidence=Level.LOW,
            confidence_reason=why,
            agent=self._axis.agent_name,
            stage=ctx.stage.value,
        )

    @staticmethod
    def _claim_text(claim: Claim | None) -> str:
        return claim.statement if claim is not None else "(not established)"
