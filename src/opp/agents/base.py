"""The Agent interface, its context and its results (plan.md §6-§7).

An agent's job is a list of named sub-tasks (``plan``); the runtime calls
``run_subtask`` for each one and commits its output atomically. Agents never touch the
DB, never call providers directly and never hold chat history: they get an
``AgentContext`` whose ``AgentTools`` are permission-checked, budget-guarded and metered,
and whose ``AgentMemory`` is the compact state to build prompts from.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel

from opp.agents.budget import AgentBudget, CallKind
from opp.agents.memory import AgentMemory, fold
from opp.domain.enums import AgentRunStatus, ModelTier, StageName
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Kill, Lead
from opp.domain.opportunity import Opportunity
from opp.domain.run import RunConfig
from opp.llm.gateway import CallContext, LLMError, LLMGateway, LLMResult
from opp.llm.pricing import CHARS_PER_TOKEN
from opp.research.fetcher import MeteredFetcher, PageFetcher, RawPage
from opp.research.search import (
    Freshness,
    MeteredSearch,
    SearchError,
    SearchProvider,
    SearchResponse,
    TraceSink,
)

# A single prompt larger than this is a bug in the agent, not something to send.
MAX_PROMPT_TOKENS = 150_000


class ToolNotPermittedError(Exception):
    """The agent used a tool it didn't declare (plan.md §18: tools are allow-listed)."""


class PromptTooLargeError(Exception):
    pass


@dataclass
class SubTaskResult:
    """What one sub-task produced. The runtime validates and commits it atomically.

    Sources may be new objects: the runtime stores each once (deduped by canonical URL)
    and rewrites the claims' source references to the stored ids. Leads and opportunities
    go through the Evidence Store (validated, fingerprinted); kills are logged as given."""

    sources: list[Source] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    leads: list[Lead] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)
    kills: list[Kill] = field(default_factory=list)
    # Loose claims are filed under this opportunity when set.
    opportunity_id: UUID | None = None
    note: str = ""
    follow_up_tasks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentResult:
    agent_run_id: UUID
    status: AgentRunStatus
    tasks_done: int
    tasks_failed: int
    tasks_pending: int
    cost_usd: float
    compactions: int = 0
    claims_saved: int = 0
    leads_saved: int = 0
    # Leads refused by the Evidence Store because no claim cited a stored source.
    leads_rejected: int = 0
    demoted_claims: int = 0
    error: str | None = None

    @property
    def budget_exhausted(self) -> bool:
        return self.status is AgentRunStatus.HALTED_BUDGET


BeforeLLMHook = Callable[[], Awaitable[None]]


class AgentTools:
    """The only way an agent reaches the outside world. Every call is permission-checked,
    pre-checked against the budget (it raises ``BudgetExceededError`` instead of
    overspending), metered as a Trace and charged to the agent and the run."""

    def __init__(
        self,
        *,
        allowed: frozenset[CallKind],
        tier: ModelTier,
        budget: AgentBudget,
        call_context: CallContext,
        gateway: LLMGateway | None,
        search: SearchProvider | None,
        fetcher: PageFetcher | None,
        sink: TraceSink,
        search_cost_estimate_usd: float,
        before_llm: BeforeLLMHook,
    ) -> None:
        self._allowed = allowed
        self._tier = tier
        self._budget = budget
        self._ctx = call_context
        self._gateway = gateway
        self._search = (
            MeteredSearch(
                search,
                sink,
                run_id=call_context.run_id,
                stage=call_context.stage,
                agent=call_context.agent,
            )
            if search is not None
            else None
        )
        self._fetcher = (
            MeteredFetcher(
                fetcher,
                sink,
                run_id=call_context.run_id,
                stage=call_context.stage,
                agent=call_context.agent,
            )
            if fetcher is not None
            else None
        )
        self._search_cost = search_cost_estimate_usd
        # The runtime points this at its compaction check once the context exists.
        self.before_llm = before_llm
        self.tokens_in = 0
        self.tokens_out = 0

    def _permit(self, kind: CallKind) -> None:
        if kind not in self._allowed:
            raise ToolNotPermittedError(f"{self._ctx.agent} may not use {kind.value}")

    async def llm[T: BaseModel](
        self,
        *,
        prompt: str,
        output_type: type[T],
        system: str = "",
        max_tokens: int = 16_000,
        tier: ModelTier | None = None,
    ) -> LLMResult[T]:
        self._permit(CallKind.LLM)
        if self._gateway is None:
            raise ToolNotPermittedError("no LLM gateway configured")
        await self.before_llm()  # compaction check: runs before every model call
        prompt_chars = len(system) + len(prompt)
        if prompt_chars // CHARS_PER_TOKEN > MAX_PROMPT_TOKENS:
            raise PromptTooLargeError(f"prompt of ~{prompt_chars // CHARS_PER_TOKEN} tokens")
        tier = tier or self._tier
        self._budget.check(
            CallKind.LLM,
            self._gateway.estimate_cost(tier, prompt_chars=prompt_chars, max_tokens=max_tokens),
        )
        try:
            result = await self._gateway.structured(
                tier=tier,
                prompt=prompt,
                output_type=output_type,
                context=self._ctx,
                system=system,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            self._budget.charge(CallKind.LLM, exc.cost_usd)
            raise
        self._budget.charge(CallKind.LLM, result.cost_usd)
        self.tokens_in += result.usage.total_input_tokens
        self.tokens_out += result.usage.output_tokens
        return result

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        self._permit(CallKind.SEARCH)
        if self._search is None:
            raise ToolNotPermittedError("no search provider configured")
        self._budget.check(CallKind.SEARCH, self._search_cost)
        try:
            response = await self._search.search(query, k=k, freshness=freshness)
        except SearchError as exc:
            self._budget.charge(CallKind.SEARCH, exc.cost_usd)
            raise
        self._budget.charge(CallKind.SEARCH, response.cost_usd)
        return response

    async def fetch(self, url: str) -> RawPage:
        self._permit(CallKind.FETCH)
        if self._fetcher is None:
            raise ToolNotPermittedError("no fetcher configured")
        self._budget.check(CallKind.FETCH, 0.0)
        try:
            return await self._fetcher.fetch(url)
        finally:
            self._budget.charge(CallKind.FETCH, 0.0)


@dataclass
class AgentContext:
    """Everything an agent may see during a sub-task (plan.md §6)."""

    run_id: UUID
    stage: StageName
    agent_run_id: UUID
    run_config: RunConfig
    memory: AgentMemory
    tools: AgentTools
    budget: AgentBudget
    done_tasks: list[str] = field(default_factory=list)
    # Committed output so far for this agent run, e.g. {"leads": 4, "claims": 12}.
    output_counts: dict[str, int] = field(default_factory=dict)


class Agent(ABC):
    """One reasoning step of the funnel. Subclasses declare their name, tier, prompt
    version and the tools they may use; the runtime handles everything else."""

    name: ClassVar[str]
    default_tier: ClassVar[ModelTier]
    prompt_version: ClassVar[str] = "v1"
    tools: ClassVar[frozenset[CallKind]] = frozenset()

    @abstractmethod
    def plan(self, ctx: AgentContext) -> list[str]:
        """The sub-task ids for a fresh run, in order. Ids must be stable and unique."""

    @abstractmethod
    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        """Do one sub-task. Build prompts from ``ctx.memory.render()``, not from history."""

    async def compact(self, memory: AgentMemory, ctx: AgentContext) -> str:
        """The new summary when memory is compacted. Default: fold notes in, bounded."""
        return fold(memory.summary, memory.notes)
