"""Opportunity profiles: hard gates and dimension weights (intent.md §13.2, specs.md §17.3).

A profile is configuration, not code: adding one is adding a record here. Weights are
relative (they're normalised when used); a run may nudge them with
``RunConfig.weight_overrides`` and tighten or relax gates with ``hard_gates_override``.
"""

from pydantic import Field

from opp.domain.base import DomainModel
from opp.domain.enums import Dimension as D
from opp.domain.run import RunConfig


class Profile(DomainModel):
    id: str
    name: str
    max_capital_usd: float = Field(gt=0)
    max_time_to_revenue_days: float = Field(gt=0)
    weights: dict[D, float]


_NEUTRAL = dict.fromkeys(D, 1.0)

BOOTSTRAP = Profile(
    id="bootstrap",
    name="Bootstrap / Fast-Cashflow",
    max_capital_usd=2_000,
    max_time_to_revenue_days=120,
    weights={
        **_NEUTRAL,
        # Maximise: time to revenue, margin, automation, near-zero capital.
        D.TIME_TO_FIRST_REVENUE: 3.0,
        D.GROSS_MARGIN: 2.5,
        D.AUTOMATION: 2.5,
        D.UPFRONT_CAPITAL: 2.5,
        # Heavily weight (soft): solo feasibility.
        D.SMALL_TEAM_FEASIBILITY: 2.5,
        D.CUSTOMER_PAIN: 2.0,
        D.WILLINGNESS_TO_PAY: 2.0,
        D.EVIDENCE_QUALITY: 2.0,
        D.ASSUMPTION_RISK: 1.5,
        # De-prioritise: huge TAM, long-term moat, growth/scale.
        D.MARKET_ATTRACTIVENESS: 0.5,
        D.DEFENSIBILITY: 0.5,
        D.SCALABILITY: 0.5,
    },
)

MICRO = Profile(
    id="micro",
    name="Micro / Same-Month",
    max_capital_usd=1_000,
    max_time_to_revenue_days=30,
    weights={
        **_NEUTRAL,
        D.TIME_TO_FIRST_REVENUE: 4.0,
        D.UPFRONT_CAPITAL: 3.5,
        D.AUTOMATION: 3.0,
        D.SMALL_TEAM_FEASIBILITY: 3.0,
        D.GROSS_MARGIN: 2.0,
        D.CUSTOMER_PAIN: 2.0,
        D.WILLINGNESS_TO_PAY: 2.0,
        D.EVIDENCE_QUALITY: 2.0,
        D.ASSUMPTION_RISK: 1.5,
        D.REVENUE_POTENTIAL: 0.5,
        D.MARKET_ATTRACTIVENESS: 0.1,
        D.DEFENSIBILITY: 0.1,
        D.SCALABILITY: 0.1,
    },
)

PROFILES: dict[str, Profile] = {p.id: p for p in (BOOTSTRAP, MICRO)}


class UnknownProfileError(LookupError):
    pass


def get_profile(profile_ref: str) -> Profile:
    try:
        return PROFILES[profile_ref]
    except KeyError:
        raise UnknownProfileError(
            f"unknown profile {profile_ref!r}; known: {', '.join(sorted(PROFILES))}"
        ) from None


def effective_weights(run_config: RunConfig) -> dict[D, float]:
    """The run's weights: the profile's, with the run's overrides, normalised to sum to 1."""
    weights = {**get_profile(run_config.profile_ref).weights, **run_config.weight_overrides}
    total = sum(max(w, 0.0) for w in weights.values())
    return {dim: max(weights.get(dim, 0.0), 0.0) / total for dim in D}


def weights_ref(run_config: RunConfig) -> str:
    """How to cite the weights used: the profile, plus a marker when the run nudged them."""
    return run_config.profile_ref + ("+overrides" if run_config.weight_overrides else "")


def composite(scores: dict[D, int], weights: dict[D, float]) -> float:
    """Weighted mean of the scored dimensions (unscored ones don't count)."""
    used = {dim: w for dim, w in weights.items() if dim in scores}
    total = sum(used.values())
    if total <= 0:
        return float(sum(scores.values()) / len(scores)) if scores else 1.0
    return sum(scores[dim] * w for dim, w in used.items()) / total
