"""Evaluation profiles: hard gates + dimension weights (specs.md §17.3, intent.md §13.2).

Adding a profile is configuration only: one more ``Profile`` in ``PROFILES``. Weights are
relative (they're normalised when used); ``RunConfig.weight_overrides`` replaces
individual weights for one run, and ``RunConfig.hard_gates_override`` the gates.
"""

from collections.abc import Iterable, Mapping

from pydantic import Field

from opp.domain.base import DomainModel
from opp.domain.enums import Dimension
from opp.domain.run import RunConfig


class Profile(DomainModel):
    id: str
    name: str
    max_capital_usd: float = Field(gt=0)
    max_time_to_revenue_days: float = Field(gt=0)
    weights: dict[Dimension, float]

    def weights_for(self, run_config: RunConfig) -> dict[Dimension, float]:
        return {**self.weights, **run_config.weight_overrides}


_BASE = dict.fromkeys(Dimension, 1.0)

BOOTSTRAP = Profile(
    id="bootstrap",
    name="Bootstrap / Fast-Cashflow",
    max_capital_usd=2_000,
    max_time_to_revenue_days=120,
    weights={
        **_BASE,
        Dimension.TIME_TO_FIRST_REVENUE: 2.0,
        Dimension.GROSS_MARGIN: 1.5,
        Dimension.AUTOMATION: 1.5,
        Dimension.UPFRONT_CAPITAL: 1.5,
        Dimension.SMALL_TEAM_FEASIBILITY: 1.5,
        Dimension.CUSTOMER_PAIN: 1.5,
        Dimension.WILLINGNESS_TO_PAY: 1.5,
        Dimension.EVIDENCE_QUALITY: 1.5,
        Dimension.MARKET_ATTRACTIVENESS: 0.5,
        Dimension.DEFENSIBILITY: 0.5,
        Dimension.SCALABILITY: 0.5,
    },
)

MICRO = Profile(
    id="micro",
    name="Micro / Same-Month",
    max_capital_usd=1_000,
    max_time_to_revenue_days=30,
    weights={
        **_BASE,
        Dimension.TIME_TO_FIRST_REVENUE: 3.0,
        Dimension.UPFRONT_CAPITAL: 2.0,
        Dimension.GROSS_MARGIN: 2.0,
        Dimension.AUTOMATION: 2.0,
        Dimension.SMALL_TEAM_FEASIBILITY: 2.0,
        Dimension.WILLINGNESS_TO_PAY: 2.0,
        Dimension.CUSTOMER_PAIN: 1.5,
        Dimension.EVIDENCE_QUALITY: 1.5,
        Dimension.MARKET_ATTRACTIVENESS: 0.2,
        Dimension.DEFENSIBILITY: 0.2,
        Dimension.SCALABILITY: 0.2,
    },
)

PROFILES: dict[str, Profile] = {p.id: p for p in (BOOTSTRAP, MICRO)}


class UnknownProfileError(LookupError):
    pass


def get_profile(profile_id: str) -> Profile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise UnknownProfileError(f"no profile {profile_id!r}") from None


def weighted_composite(
    scores: Iterable[tuple[Dimension, int]], weights: Mapping[Dimension, float]
) -> float:
    """Profile-weighted mean of 1-5 scores: for ranking only (specs.md §17.5)."""
    pairs = [(weights.get(dimension, 1.0), score) for dimension, score in scores]
    total = sum(w for w, _ in pairs)
    if total <= 0:
        raise ValueError("no weighted scores")
    return round(sum(w * s for w, s in pairs) / total, 3)
