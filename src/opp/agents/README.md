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
