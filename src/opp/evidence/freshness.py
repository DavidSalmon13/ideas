"""Claim freshness (specs.md §7.8, §26.6).

A material claim goes stale once it is older than a configurable window. Only sourced-kind
claims (``fact``/``estimate``) are re-verifiable against the web — an ``assumption`` is a
labelled inference and an ``unknown`` is already "looked, found nothing", so neither is
something Refresh (specs.md §2.11) can re-check by searching. Pure, no I/O.
"""

from datetime import datetime, timedelta

from opp.domain.enums import ClaimType
from opp.domain.evidence import Claim
from opp.domain.opportunity import Opportunity

# Which claim types Refresh re-verifies: the ones that rest on external evidence.
REVERIFIABLE_TYPES = frozenset({ClaimType.FACT, ClaimType.ESTIMATE})
# Conservative default staleness window; tunable (specs.md §26.6 recommends per-type later).
DEFAULT_STALENESS_DAYS = 90.0
# Cap how many stale claims one refresh re-verifies, so a refresh stays cheap and bounded.
DEFAULT_MAX_REFRESH_CLAIMS = 20


def is_stale(claim: Claim, *, now: datetime, max_age: timedelta) -> bool:
    return claim.type in REVERIFIABLE_TYPES and (now - claim.observed_at) > max_age


def stale_claims(
    opportunity: Opportunity,
    *,
    now: datetime,
    max_age: timedelta,
    limit: int = DEFAULT_MAX_REFRESH_CLAIMS,
) -> list[Claim]:
    """The opportunity's re-verifiable claims older than ``max_age``, oldest first, deduped
    by id and capped at ``limit`` (the oldest are re-verified first)."""
    seen: set[str] = set()
    stale: list[Claim] = []
    for claim in opportunity.claims():
        key = str(claim.id)
        if key in seen or not is_stale(claim, now=now, max_age=max_age):
            continue
        seen.add(key)
        stale.append(claim)
    stale.sort(key=lambda c: c.observed_at)
    return stale[:limit]
