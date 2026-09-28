"""Run-level budget rules: pre-flight, per-stage pre-checks, what's left to finish."""

from uuid import uuid4

import pytest

from opp.config.settings import DEFAULT_STAGE_MINIMUMS_USD, Settings
from opp.domain.enums import StageName, StageStatus
from opp.domain.run import Stage
from opp.orchestration.budget import (
    BudgetPreflightError,
    minimum_run_cost,
    minimum_to_finish,
    precheck,
    preflight,
)

MINIMUMS = {StageName.DISCOVERY: 3.0, StageName.SCREENING: 0.5, StageName.DEEP_RESEARCH: 5.0}


def test_default_minimums_fit_the_default_ceiling_with_a_reserve() -> None:
    total = minimum_run_cost(DEFAULT_STAGE_MINIMUMS_USD)
    assert total == pytest.approx(15.5)
    assert 20.0 - total >= 2.0  # specs.md §21.2 keeps ~$2 in reserve
    preflight(20.0, DEFAULT_STAGE_MINIMUMS_USD)


def test_preflight_refuses_a_ceiling_below_the_least_a_run_costs() -> None:
    with pytest.raises(BudgetPreflightError, match=r"\$8.00 ceiling is below the \$8.50"):
        preflight(8.0, MINIMUMS)
    preflight(8.5, MINIMUMS)
    preflight(0.01, {})  # no minimums configured: nothing to refuse


def test_precheck_blocks_a_stage_the_budget_cannot_afford() -> None:
    assert (
        precheck(StageName.DEEP_RESEARCH, remaining_usd=5.0, ceiling_usd=20, minimums=MINIMUMS)
        is None
    )
    reason = precheck(
        StageName.DEEP_RESEARCH, remaining_usd=4.99, ceiling_usd=20, minimums=MINIMUMS
    )
    assert reason == (
        "budget pre-check: the deep_research stage needs at least $5.00 and only $4.99 "
        "is left of the $20.00 ceiling"
    )
    # A stage without a minimum is never blocked.
    assert precheck(StageName.STRATEGY, remaining_usd=0, ceiling_usd=20, minimums=MINIMUMS) is None


def test_a_resumed_stage_only_needs_the_rest_of_its_minimum() -> None:
    kwargs = {"remaining_usd": 1.0, "ceiling_usd": 20.0, "minimums": MINIMUMS}
    assert precheck(StageName.DISCOVERY, **kwargs) is not None  # type: ignore[arg-type]
    assert precheck(StageName.DISCOVERY, spent_in_stage=2.5, **kwargs) is None  # type: ignore[arg-type]


def test_minimum_to_finish_skips_completed_stages_and_credits_unfinished_ones() -> None:
    run_id = uuid4()
    done = Stage(run_id=run_id, name=StageName.DISCOVERY, status=StageStatus.COMPLETED)
    halfway = Stage(run_id=run_id, name=StageName.SCREENING, cost_usd=0.2)
    assert minimum_to_finish([], MINIMUMS) == pytest.approx(8.5)
    assert minimum_to_finish([done, halfway], MINIMUMS) == pytest.approx(0.3 + 5.0)
    # Only the latest attempt counts (a kickback re-opens a completed stage).
    again = Stage(run_id=run_id, name=StageName.DISCOVERY, attempt=2, cost_usd=1.0)
    assert minimum_to_finish([done, again, halfway], MINIMUMS) == pytest.approx(2.0 + 5.3)


def test_stage_minimums_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPP_STAGE_MINIMUMS_USD", '{"deep_research": 6.5}')
    minimums = Settings().stage_minimums_usd
    assert minimums[StageName.DEEP_RESEARCH] == 6.5
    assert minimums[StageName.DISCOVERY] == DEFAULT_STAGE_MINIMUMS_USD[StageName.DISCOVERY]
    monkeypatch.setenv("OPP_STAGE_MINIMUMS_USD", '{"discovery": -1}')
    with pytest.raises(ValueError, match="negative"):
        Settings()
