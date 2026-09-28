"""Verdict logic: hard gates, evidence caps and the composite (specs.md §17).

Pure functions, no I/O. The Judge scores the dimensions; this module holds the
non-compensatory rules the score cannot override — the profile's capital and
time-to-revenue hard gates and the red-team's fatal flaw — and the evidence caps that keep
a dimension from scoring high on absent evidence.
"""

from opp.config.profiles import composite as weighted_composite
from opp.config.profiles import effective_weights, get_profile
from opp.domain.enums import Dimension, EvidenceBasis, Override
from opp.domain.evidence import Claim
from opp.domain.opportunity import Opportunity
from opp.domain.run import RunConfig

# A dimension may score no higher than this given what its supporting claims rest on
# (specs.md §17.4: no dimension scores high on unknown/low-confidence evidence).
EVIDENCE_CAPS = {
    EvidenceBasis.SOURCED: 5,
    EvidenceBasis.INFERRED: 3,
    EvidenceBasis.INSUFFICIENT: 2,
}

# Units the time-to-revenue gate understands, in days. An unrecognised unit is not compared
# (a hard Kill is never forced on an ambiguous figure).
_DAYS_PER_UNIT = {
    "day": 1.0,
    "days": 1.0,
    "week": 7.0,
    "weeks": 7.0,
    "month": 30.0,
    "months": 30.0,
    "year": 365.0,
    "years": 365.0,
}


def effective_gates(run_config: RunConfig) -> tuple[float, float]:
    """The (max capital USD, max time-to-revenue days) for this run: the profile's, unless
    the run overrode them (specs.md §3.3)."""
    profile = get_profile(run_config.profile_ref)
    override = run_config.hard_gates_override
    return (
        override.max_capital_usd or profile.max_capital_usd,
        override.max_time_to_revenue_days or profile.max_time_to_revenue_days,
    )


def hard_gate_overrides(opportunity: Opportunity, run_config: RunConfig) -> list[Override]:
    """The hard gates this opportunity's economics breach (specs.md §17.4). Each forces a
    Kill regardless of the composite. Uses the strategist's best estimate of capital and
    time-to-revenue; an absent or unit-ambiguous figure is not gated (never fabricate a
    breach)."""
    max_capital, max_days = effective_gates(run_config)
    metrics = opportunity.economics.metrics
    overrides: list[Override] = []
    capital = metrics.capital_required
    if capital is not None and capital.value is not None and capital.value > max_capital:
        overrides.append(Override.CAPITAL_GATE)
    days = _time_to_revenue_days(metrics.time_to_revenue)
    if days is not None and days > max_days:
        overrides.append(Override.TIME_TO_REVENUE_GATE)
    return overrides


def all_overrides(opportunity: Opportunity, run_config: RunConfig) -> list[Override]:
    """Every non-compensatory override in force: the breached hard gates plus a red-team
    fatal flaw. Any of them forces a Kill (specs.md §17.4)."""
    overrides = hard_gate_overrides(opportunity, run_config)
    if opportunity.red_team.fatal_flaw:
        overrides.append(Override.FATAL_FLAW)
    return overrides


def cap_score(score: int, basis: EvidenceBasis, *, has_support: bool) -> int:
    """Hold a dimension score to its evidence: a 'sourced' claim that cites nothing real is
    treated as merely inferred (specs.md §17.4)."""
    if basis is EvidenceBasis.SOURCED and not has_support:
        basis = EvidenceBasis.INFERRED
    return min(score, EVIDENCE_CAPS[basis])


def composite(scores: dict[Dimension, int], run_config: RunConfig) -> float:
    """The profile-weighted composite, for ranking only (specs.md §17.5)."""
    return weighted_composite(scores, effective_weights(run_config))


def _time_to_revenue_days(claim: Claim | None) -> float | None:
    if claim is None or claim.value is None or claim.unit is None:
        return None
    factor = _DAYS_PER_UNIT.get(claim.unit.strip().lower())
    return claim.value * factor if factor is not None else None
