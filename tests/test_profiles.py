import pytest

from opp.config.profiles import (
    BOOTSTRAP,
    MICRO,
    UnknownProfileError,
    get_profile,
    weighted_composite,
)
from opp.domain.enums import Dimension
from opp.domain.run import DEFAULT_PROFILE, RunConfig


def test_profiles_cover_every_dimension_and_differ_as_specified() -> None:
    assert get_profile(DEFAULT_PROFILE) is BOOTSTRAP
    for profile in (BOOTSTRAP, MICRO):
        assert set(profile.weights) == set(Dimension)
    # specs.md §17.3: Micro gates tighter and cares even less about market size.
    assert MICRO.max_capital_usd < BOOTSTRAP.max_capital_usd
    assert MICRO.max_time_to_revenue_days < BOOTSTRAP.max_time_to_revenue_days
    assert (
        MICRO.weights[Dimension.MARKET_ATTRACTIVENESS]
        < BOOTSTRAP.weights[Dimension.MARKET_ATTRACTIVENESS]
        < BOOTSTRAP.weights[Dimension.TIME_TO_FIRST_REVENUE]
    )
    with pytest.raises(UnknownProfileError):
        get_profile("ambitious")


def test_weight_overrides_and_composite() -> None:
    config = RunConfig(weight_overrides={Dimension.DEFENSIBILITY: 3.0})
    weights = BOOTSTRAP.weights_for(config)
    assert weights[Dimension.DEFENSIBILITY] == 3.0
    assert weights[Dimension.GROSS_MARGIN] == BOOTSTRAP.weights[Dimension.GROSS_MARGIN]
    composite = weighted_composite(
        [(Dimension.DEFENSIBILITY, 5), (Dimension.GROSS_MARGIN, 2)], weights
    )
    assert composite == pytest.approx((3.0 * 5 + 1.5 * 2) / 4.5, abs=1e-3)
    with pytest.raises(ValueError):
        weighted_composite([], weights)
