"""The AI-quality gate (plan.md §21, §24 Phase 8 accept). Runs each golden through the
funnel offline and asserts the hard checks: ~0 hallucinated facts, generic decoys killed at
triage, planted fatal flaws forcing Kill, and — on a healthy run — scenario economics with
load-bearing assumptions mapped to experiments and both small-team questions answered. The
report rubric is tracked (asserted only against total breakage), not a hard pass/fail."""

import pytest

from evals.goldens import GOLDENS, Golden
from evals.harness import GoldenResult, run_golden


@pytest.fixture(scope="module", params=GOLDENS, ids=lambda g: g.name)
def result(request: pytest.FixtureRequest) -> GoldenResult:
    golden: Golden = request.param
    return run_golden(golden)


def test_reaches_the_expected_outcome(result: GoldenResult) -> None:
    assert result.status is result.golden.expect_status
    assert result.verdict is result.golden.expect_verdict


def test_no_hallucinated_facts(result: GoldenResult) -> None:
    """Hard gate: every `fact` the run produced resolves to a stored source (unsourced ones
    are auto-demoted by the validator, so this must be exactly zero)."""
    assert result.hallucinated_facts == 0
    assert result.hallucination_rate == 0.0


def test_stays_under_budget(result: GoldenResult) -> None:
    assert result.cost_usd <= result.golden.config.budget_ceiling_usd


def test_anti_generic_kills(result: GoldenResult) -> None:
    if result.golden.expect_anti_generic_kills:
        assert result.anti_generic_kills >= 1
        assert result.killed_with_reason >= result.anti_generic_kills
    if not result.golden.expect_opportunity:
        # Nothing generic was allowed to fabricate its way to an opportunity.
        assert result.verdict is None


def test_fatal_flaw_detection(result: GoldenResult) -> None:
    if result.golden.expect_fatal_flaw:
        assert result.fatal_flaw is True
        assert result.verdict is not None and result.verdict.value == "kill"


def test_economics_and_small_team_on_a_full_decision(result: GoldenResult) -> None:
    if result.golden.expect_opportunity:
        assert result.economics_ok is True
        assert result.small_team_ok is True


def test_report_rubric_tracked(result: GoldenResult) -> None:
    if result.golden.expect_opportunity:
        assert result.rubric_score is not None
        # Tracked, not a hard bar: only a badly broken report scores this low.
        assert result.rubric_score >= 0.7
