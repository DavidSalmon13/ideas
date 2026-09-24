import pytest

from opp.llm.pricing import UnknownModelError, cost_usd, estimate_max_cost_usd, price_for
from opp.llm.types import Usage


def test_cost_counts_input_output_and_cache_tokens() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_write_tokens=200_000,
        cache_read_tokens=1_000_000,
    )
    # sonnet-5: $2 in / $10 out; writes 1.25x, reads 0.1x input
    expected = 2.0 + 0.2 * 1.25 * 2.0 + 1.0 * 0.1 * 2.0 + 0.1 * 10.0
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(expected)


def test_haiku_is_cheapest() -> None:
    usage = Usage(input_tokens=10_000, output_tokens=2_000)
    assert cost_usd("claude-haiku-4-5", usage) < cost_usd("claude-sonnet-5", usage)
    assert cost_usd("claude-haiku-4-5", usage) == pytest.approx(0.02)


def test_unknown_model_is_refused() -> None:
    with pytest.raises(UnknownModelError):
        price_for("gpt-something")


def test_estimate_assumes_full_output() -> None:
    estimate = estimate_max_cost_usd("claude-haiku-4-5", prompt_chars=4001, max_tokens=1000)
    assert estimate == pytest.approx((1001 * 1.0 + 1000 * 5.0) / 1_000_000)
