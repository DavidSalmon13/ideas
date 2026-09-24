import pytest

from opp.agents.budget import AgentBudget, BudgetExceededError, CallKind, ResearchBudget, RunBudget
from opp.agents.memory import AgentMemory, fold


def test_run_budget_is_shared_and_never_negative() -> None:
    run = RunBudget(ceiling_usd=1.0)
    a = AgentBudget(limits=ResearchBudget(max_cost_usd=1.0), run=run)
    b = AgentBudget(limits=ResearchBudget(max_cost_usd=1.0), run=run)
    a.charge(CallKind.LLM, 0.7)
    b.charge(CallKind.LLM, 0.5)
    assert run.spent_usd == pytest.approx(1.2)
    assert run.remaining_usd == 0.0


def test_agent_budget_checks_calls_cost_and_run() -> None:
    run = RunBudget(ceiling_usd=10.0)
    budget = AgentBudget(limits=ResearchBudget(max_cost_usd=0.5, max_searches=1), run=run)

    budget.check(CallKind.SEARCH, 0.01)
    budget.charge(CallKind.SEARCH, 0.01)
    with pytest.raises(BudgetExceededError, match="search limit") as calls:
        budget.check(CallKind.SEARCH, 0.01)
    assert calls.value.scope == "agent"

    with pytest.raises(BudgetExceededError, match="agent budget"):
        budget.check(CallKind.LLM, 0.6)

    run.charge(9.8)
    with pytest.raises(BudgetExceededError, match="run budget") as run_scope:
        budget.check(CallKind.LLM, 0.3)
    assert run_scope.value.scope == "run"


def test_memory_renders_summary_and_notes() -> None:
    memory = AgentMemory(summary="found 2 leads")
    memory.remember("  checked reddit  ")
    memory.remember("   ")
    assert memory.notes == ["checked reddit"]
    assert (
        memory.render()
        == "Summary of work so far:\nfound 2 leads\n\nRecent notes:\n- checked reddit"
    )
    assert memory.durable() == "found 2 leads\n- checked reddit"
    assert memory.estimated_tokens() > 0


def test_fold_keeps_recent_work_within_the_cap() -> None:
    notes = [f"note {i} " + "x" * 40 for i in range(100)]
    folded = fold("old summary", notes, max_chars=500)
    assert len(folded) <= 500
    assert folded.startswith("[…earlier work condensed…]")
    assert "note 99" in folded
    assert "old summary" not in folded
    assert fold("short", ["a"]) == "short\n- a"
