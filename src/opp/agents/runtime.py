"""The agent runtime: sub-task loop, compaction, budget guard, resume (plan.md §7).

``AgentRuntime.run`` drives one ``AgentRun`` to an end state and never raises into the
orchestrator (plan.md §31):

- **Sub-task loop.** Each sub-task's sources, claims and leads, the ledger move
  (pending -> done), the running summary and the cost so far are committed in one
  transaction, so at most one in-flight sub-task is ever unsaved.
- **Compaction.** Before every LLM call (and every sub-task) the runtime checks the
  working memory's size; past the threshold it folds it into a new summary, persists it,
  and the agent's next prompt is rebuilt from that compact state.
- **Budget guard.** Tool calls are pre-checked (``AgentTools``); when the next call can't
  be afforded the run of this agent stops as ``HALTED_BUDGET`` with the sub-task still
  pending, which is what the orchestrator turns into a stop-and-wait.
- **Resume.** Running an interrupted agent run again rehydrates from its ledger and
  summary and continues with the next pending sub-task.
- **Degrade, don't crash.** A sub-task that fails on an LLM/search/fetch error is recorded
  as failed and the agent carries on (``DEGRADED``); an unexpected error ends the agent
  run as ``FAILED`` with the error recorded.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    AgentTools,
    PromptTooLargeError,
    SubTaskResult,
)
from opp.agents.budget import AgentBudget, BudgetExceededError, ResearchBudget, RunBudget
from opp.agents.memory import AgentMemory
from opp.db.repositories import AgentRunRepository, RunRepository, StageRepository
from opp.domain.base import utcnow
from opp.domain.enums import AgentRunStatus, StageName
from opp.domain.evidence import Claim
from opp.domain.lead import Lead
from opp.domain.run import AgentRun, RunConfig
from opp.evidence.store import EvidenceStore, UnsourcedLeadError
from opp.evidence.validator import IssueKind
from opp.llm.gateway import CallContext, LLMError, LLMGateway
from opp.research.fetcher import FetchError, PageFetcher
from opp.research.search import SearchError, SearchProvider, TraceSink

# Memory above this (estimated tokens) is compacted before the next model call.
COMPACTION_THRESHOLD_TOKENS = 8_000

_FINISHED = frozenset(
    {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.DEGRADED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }
)
# Failures that mean "this sub-task couldn't be done", not "the agent is broken".
_DEGRADE_ERRORS = (LLMError, SearchError, FetchError, PromptTooLargeError)

log = structlog.get_logger(__name__)


@dataclass
class _Tally:
    compactions: int = 0
    claims: int = 0
    leads: int = 0
    rejected_leads: int = 0
    demoted: int = 0


class AgentRuntime:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        gateway: LLMGateway | None,
        sink: TraceSink,
        search: SearchProvider | None = None,
        fetcher: PageFetcher | None = None,
        search_cost_estimate_usd: float = 0.01,
        compaction_threshold_tokens: int = COMPACTION_THRESHOLD_TOKENS,
    ) -> None:
        self._sessions = sessions
        self._gateway = gateway
        self._sink = sink
        self._search = search
        self._fetcher = fetcher
        self._search_cost = search_cost_estimate_usd
        self._threshold = compaction_threshold_tokens

    async def run(
        self,
        agent: Agent,
        agent_run_id: UUID,
        *,
        budget: ResearchBudget,
        run_budget: RunBudget,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> AgentResult:
        agent_run, stage_name, run_id, run_config = self._load(agent_run_id)
        tally = _Tally()
        if agent_run.status in _FINISHED:
            return self._result(agent_run, tally)  # idempotent: nothing left to do

        memory = AgentMemory(summary=agent_run.summary)
        agent_budget = AgentBudget(limits=budget, run=run_budget, spent_usd=agent_run.cost_usd)
        ctx = self._context(agent, agent_run, stage_name, run_id, run_config, memory, agent_budget)

        async def compact_if_needed() -> None:
            if memory.estimated_tokens() <= self._threshold:
                return
            memory.summary = await agent.compact(memory, ctx)
            memory.notes = []
            tally.compactions += 1
            agent_run.summary = memory.summary
            self._save_agent_run(agent_run, ctx, run_budget)
            log.info("agent_compacted", agent=agent.name, summary_chars=len(memory.summary))

        ctx.tools.before_llm = compact_if_needed

        try:
            if not agent_run.work_ledger.pending and not agent_run.work_ledger.done:
                agent_run.work_ledger.pending = list(dict.fromkeys(agent.plan(ctx)))
            agent_run.status = AgentRunStatus.RUNNING
            agent_run.model_tier = agent.default_tier
            agent_run.prompt_version = agent.prompt_version
            agent_run.started_at = agent_run.started_at or utcnow()
            self._save_agent_run(agent_run, ctx, run_budget)

            while agent_run.work_ledger.pending:
                if should_stop():
                    return self._finish(agent_run, ctx, run_budget, tally, AgentRunStatus.CANCELLED)
                task = agent_run.work_ledger.pending[0]
                ctx.done_tasks = list(agent_run.work_ledger.done)
                await compact_if_needed()
                try:
                    output = await agent.run_subtask(task, ctx)
                except BudgetExceededError as exc:
                    agent_run.error = str(exc)
                    log.info("agent_halted_budget", agent=agent.name, task=task, scope=exc.scope)
                    return self._finish(
                        agent_run, ctx, run_budget, tally, AgentRunStatus.HALTED_BUDGET, end=False
                    )
                except _DEGRADE_ERRORS as exc:
                    log.warning("subtask_failed", agent=agent.name, task=task, error=str(exc))
                    memory.remember(f"sub-task {task} failed: {exc}")
                    self._commit(
                        agent_run, ctx, run_budget, task, SubTaskResult(), tally, failed=True
                    )
                    continue
                memory.remember(output.note)
                self._commit(agent_run, ctx, run_budget, task, output, tally)
        except Exception as exc:  # never raise into the orchestrator
            log.exception("agent_failed", agent=agent.name)
            agent_run.error = f"{type(exc).__name__}: {exc}"
            return self._finish(agent_run, ctx, run_budget, tally, AgentRunStatus.FAILED)

        status = (
            AgentRunStatus.DEGRADED if agent_run.work_ledger.failed else AgentRunStatus.COMPLETED
        )
        return self._finish(agent_run, ctx, run_budget, tally, status)

    # --- persistence -------------------------------------------------------------------

    def _load(self, agent_run_id: UUID) -> tuple[AgentRun, StageName, UUID, RunConfig]:
        with self._sessions() as session:
            agent_run = AgentRunRepository(session).get(agent_run_id)
            if agent_run is None:
                raise LookupError(f"agent run {agent_run_id} not found")
            stage = StageRepository(session).get(agent_run.stage_id)
            if stage is None:
                raise LookupError(f"stage {agent_run.stage_id} not found")
            run = RunRepository(session).get(stage.run_id)
            if run is None:
                raise LookupError(f"run {stage.run_id} not found")
        return agent_run, stage.name, run.id, run.run_config

    def _context(
        self,
        agent: Agent,
        agent_run: AgentRun,
        stage: StageName,
        run_id: UUID,
        run_config: RunConfig,
        memory: AgentMemory,
        budget: AgentBudget,
    ) -> AgentContext:
        async def no_op() -> None:
            return None

        tools = AgentTools(
            allowed=agent.tools,
            tier=agent.default_tier,
            budget=budget,
            call_context=CallContext(run_id=run_id, stage=stage, agent=agent.name),
            gateway=self._gateway,
            search=self._search,
            fetcher=self._fetcher,
            sink=self._sink,
            search_cost_estimate_usd=self._search_cost,
            before_llm=no_op,
        )
        return AgentContext(
            run_id=run_id,
            stage=stage,
            agent_run_id=agent_run.id,
            run_config=run_config,
            memory=memory,
            tools=tools,
            budget=budget,
            output_counts=dict(agent_run.output_counts),
        )

    def _commit(
        self,
        agent_run: AgentRun,
        ctx: AgentContext,
        run_budget: RunBudget,
        task: str,
        output: SubTaskResult,
        tally: _Tally,
        *,
        failed: bool = False,
    ) -> None:
        """One transaction: the sub-task's evidence, the ledger move, summary and cost."""
        ledger = agent_run.work_ledger
        with self._sessions() as session:
            store = EvidenceStore(session)
            ids = {source.id: store.add_source(source).id for source in output.sources}
            counts = agent_run.output_counts
            saved_leads = 0
            for lead in output.leads:
                try:
                    _, report = store.save_lead(_remap_lead(lead, ids))
                except UnsourcedLeadError as exc:
                    # No lead without a source: drop it, keep the rest of the sub-task.
                    tally.rejected_leads += 1
                    ctx.memory.remember(f"{task}: dropped an unsourced lead")
                    log.warning("lead_rejected", agent=agent_run.agent_name, error=str(exc))
                    continue
                saved_leads += 1
                tally.leads += 1
                tally.demoted += len(report.claim_ids(IssueKind.DEMOTED_UNSOURCED))
            if output.claims:
                claims, report = store.save_claims(
                    [_remap(claim, ids) for claim in output.claims],
                    opportunity_id=output.opportunity_id,
                )
                tally.claims += len(claims)
                tally.demoted += len(report.claim_ids(IssueKind.DEMOTED_UNSOURCED))
            for key, amount in (
                ("leads", saved_leads),
                ("claims", len(output.claims)),
                ("sources", len(ids)),
            ):
                if amount:
                    counts[key] = counts.get(key, 0) + amount
            ctx.output_counts = dict(counts)

            ledger.pending = [t for t in ledger.pending if t != task]
            if task not in ledger.done:
                ledger.done.append(task)
            if failed and task not in ledger.failed:
                ledger.failed.append(task)
            known = set(ledger.done) | set(ledger.pending)
            ledger.pending.extend(
                t for t in dict.fromkeys(output.follow_up_tasks) if t not in known
            )
            agent_run.summary = ctx.memory.durable()
            self._write_progress(session, agent_run, ctx, run_budget)
            session.commit()

    def _save_agent_run(
        self, agent_run: AgentRun, ctx: AgentContext, run_budget: RunBudget
    ) -> None:
        with self._sessions() as session:
            self._write_progress(session, agent_run, ctx, run_budget)
            session.commit()

    def _write_progress(
        self, session: Session, agent_run: AgentRun, ctx: AgentContext, run_budget: RunBudget
    ) -> None:
        agent_run.cost_usd = ctx.budget.spent_usd
        agent_run.tokens_in += ctx.tools.tokens_in
        agent_run.tokens_out += ctx.tools.tokens_out
        ctx.tools.tokens_in = ctx.tools.tokens_out = 0
        AgentRunRepository(session).upsert(agent_run)
        runs = RunRepository(session)
        run = runs.get(ctx.run_id)
        if run is not None:
            run.cost_to_date_usd = max(run.cost_to_date_usd, run_budget.spent_usd)
            run.updated_at = utcnow()
            runs.upsert(run)

    def _finish(
        self,
        agent_run: AgentRun,
        ctx: AgentContext,
        run_budget: RunBudget,
        tally: _Tally,
        status: AgentRunStatus,
        *,
        end: bool = True,
    ) -> AgentResult:
        agent_run.status = status
        agent_run.summary = ctx.memory.durable()
        if end:
            agent_run.ended_at = utcnow()
        self._save_agent_run(agent_run, ctx, run_budget)
        return self._result(agent_run, tally)

    @staticmethod
    def _result(agent_run: AgentRun, tally: _Tally) -> AgentResult:
        ledger = agent_run.work_ledger
        return AgentResult(
            agent_run_id=agent_run.id,
            status=agent_run.status,
            tasks_done=len(ledger.done) - len(ledger.failed),
            tasks_failed=len(ledger.failed),
            tasks_pending=len(ledger.pending),
            cost_usd=agent_run.cost_usd,
            compactions=tally.compactions,
            claims_saved=tally.claims,
            leads_saved=tally.leads,
            leads_rejected=tally.rejected_leads,
            demoted_claims=tally.demoted,
            error=agent_run.error,
        )


def _remap(claim: Claim, ids: dict[UUID, UUID]) -> Claim:
    if not any(sid in ids for sid in claim.source_ids):
        return claim
    return claim.model_copy(update={"source_ids": [ids.get(s, s) for s in claim.source_ids]})


def _remap_lead(lead: Lead, ids: dict[UUID, UUID]) -> Lead:
    return lead.model_copy(
        update={
            "named_customer": _remap(lead.named_customer, ids),
            "specific_pain": _remap(lead.specific_pain, ids),
            "why_now": _remap(lead.why_now, ids),
        }
    )
