import pytest

from opp.config.profiles import (
    BOOTSTRAP,
    MICRO,
    UnknownProfileError,
    composite,
    effective_weights,
    get_profile,
    weights_ref,
)
from opp.domain.enums import Dimension as D
from opp.domain.run import RunConfig


def test_profiles_carry_the_intent_gates() -> None:
    assert (BOOTSTRAP.max_capital_usd, BOOTSTRAP.max_time_to_revenue_days) == (2_000, 120)
    assert (MICRO.max_capital_usd, MICRO.max_time_to_revenue_days) == (1_000, 30)
    assert get_profile("bootstrap") is BOOTSTRAP
    with pytest.raises(UnknownProfileError, match="bootstrap, micro"):
        get_profile("ambitious")


def test_weights_are_normalised_and_follow_the_profile_emphasis() -> None:
    weights = effective_weights(RunConfig())
    assert sum(weights.values()) == pytest.approx(1.0)
    assert set(weights) == set(D)
    assert weights[D.TIME_TO_FIRST_REVENUE] > weights[D.MARKET_ATTRACTIVENESS]
    micro = effective_weights(RunConfig(profile_ref="micro"))
    assert micro[D.SCALABILITY] < weights[D.SCALABILITY]


def test_run_overrides_nudge_weights_and_are_cited() -> None:
    config = RunConfig(weight_overrides={D.DEFENSIBILITY: 10.0})
    assert (
        effective_weights(config)[D.DEFENSIBILITY] > effective_weights(RunConfig())[D.DEFENSIBILITY]
    )
    assert weights_ref(config) == "bootstrap+overrides"
    assert weights_ref(RunConfig()) == "bootstrap"


def test_composite_is_a_weighted_mean_of_scored_dimensions() -> None:
    weights = {D.GROSS_MARGIN: 0.75, D.SCALABILITY: 0.25}
    assert composite({D.GROSS_MARGIN: 5, D.SCALABILITY: 1}, weights) == pytest.approx(4.0)
    assert composite({D.GROSS_MARGIN: 3}, weights) == pytest.approx(3.0)
