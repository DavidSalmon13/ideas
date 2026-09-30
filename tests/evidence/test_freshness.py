"""Claim staleness detection (specs.md §7.8, §26.6): which of an opportunity's claims are
old enough — and of a re-verifiable kind — to be re-checked by Refresh."""

from datetime import timedelta

from opp.domain.base import utcnow
from opp.domain.enums import BusinessShape, ClaimType
from opp.domain.evidence import Claim
from opp.domain.opportunity import Customer, Market, Opportunity, Problem
from opp.evidence.freshness import is_stale, stale_claims
from tests.evidence.conftest import make_claim, make_source

NOW = utcnow()


def _claim(statement: str, *, type_: ClaimType, days_old: float) -> Claim:
    src = make_source(url=f"https://example.com/{statement.replace(' ', '-')}")
    return make_claim(
        statement,
        type_,
        sources=[src] if type_ in (ClaimType.FACT, ClaimType.ESTIMATE) else [],
        observed_at=NOW - timedelta(days=days_old),
    )


def test_is_stale_respects_type_and_age() -> None:
    old_fact = _claim("old fact", type_=ClaimType.FACT, days_old=200)
    fresh_fact = _claim("fresh fact", type_=ClaimType.FACT, days_old=10)
    old_assumption = _claim("old assumption", type_=ClaimType.ASSUMPTION, days_old=200)
    old_unknown = _claim("old unknown", type_=ClaimType.UNKNOWN, days_old=200)

    max_age = timedelta(days=90)
    assert is_stale(old_fact, now=NOW, max_age=max_age)
    assert not is_stale(fresh_fact, now=NOW, max_age=max_age)
    # Assumptions and unknowns rest on no external evidence, so they are never "re-verifiable".
    assert not is_stale(old_assumption, now=NOW, max_age=max_age)
    assert not is_stale(old_unknown, now=NOW, max_age=max_age)


def test_stale_claims_selects_oldest_reverifiable_first_and_caps() -> None:
    opp = Opportunity(
        run_id=make_source().id,  # any uuid; not persisted here
        title="Refrigerant tracker",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        market=Market(size=_claim("market is $50M", type_=ClaimType.FACT, days_old=300)),
        problem=Problem(
            description=_claim("clinics lose a day a week", type_=ClaimType.ESTIMATE, days_old=120)
        ),
        customer=Customer(
            segment=_claim("fresh segment note", type_=ClaimType.FACT, days_old=5),
        ),
    )

    stale = stale_claims(opp, now=NOW, max_age=timedelta(days=90))

    assert [c.statement for c in stale] == ["market is $50M", "clinics lose a day a week"]
    # The fresh customer claim is left alone.
    assert all(c.statement != "fresh segment note" for c in stale)

    # The cap keeps a refresh bounded: only the oldest is taken when limit=1.
    capped = stale_claims(opp, now=NOW, max_age=timedelta(days=90), limit=1)
    assert [c.statement for c in capped] == ["market is $50M"]
