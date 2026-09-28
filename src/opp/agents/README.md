# Agents

Every agent subclasses `Agent` (`base.py`) and is driven by `AgentRuntime` (`runtime.py`).

**Contract**

- Class attributes: `name`, `default_tier` (cheap/medium/premium), `prompt_version`, and
  `tools` — the calls it may make (`CallKind.LLM`, `SEARCH`, `FETCH`). Anything else is refused.
- `plan(ctx)` returns the sub-task ids for a fresh run. Ids must be stable: they are the resume keys.
- `run_subtask(task, ctx)` does one sub-task and returns a `SubTaskResult` (sources, claims,
  leads, a short note, optional follow-up tasks). Never write to the DB; the runtime commits it.
- Build every prompt from `ctx.memory.render()` plus what the sub-task needs. Never keep chat history.
- Call the outside world only through `ctx.tools.llm / search / fetch`. They're budget-checked
  and metered; a call that can't be afforded raises `BudgetExceededError`. Let it propagate.
- LLM/search/fetch errors may propagate too: the runtime records the sub-task as failed and moves on.
- Optionally override `compact(memory, ctx)` for a smarter summary when memory is compacted.

**Prompt versions** are recorded on each agent run (`prompt_version`); bump it whenever the
agent's prompts or output models change.

**Agents so far**

- `discovery.py` — one per signal source; snippet search + cheap extraction; `prompts/discovery/v1`.
- `triage.py` — mechanical specificity gates, generic blocklist, one cheap judgment per batch of 8
  leads; emits updated leads + kills; `prompts/triage/v1`.
- `shallow_validation.py` — one search + one medium-tier 16-dimension score per candidate, capped by
  evidence basis; emits an Opportunity with a `ShallowScore`; `prompts/shallow_validation/v1`.
- `specialists/` — four medium-tier deep-research agents (customer, competition, market, feasibility),
  one snippet search + extraction per axis question; file loose axis claims; `prompts/deep_research/v1`.
- `synthesis.py` — one medium-tier call merging the specialists' claims, linking contradictions and
  assembling the opportunity's per-axis research; `prompts/synthesis/v1`.
- `strategy.py` — one premium-tier call turning the synthesized research into a concrete plan (MVP,
  business model, pricing, distribution, scenario unit-economics with labeled assumptions, small-team
  feasibility, validation backlog); reasons over the research, adds no sources, so every figure is an
  estimate/assumption; `prompts/strategy/v1`.
