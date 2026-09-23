from uuid import uuid4

from opp.domain.enums import BusinessShape, ClaimType, Level
from opp.domain.evidence import Source
from opp.domain.opportunity import Opportunity
from opp.evidence.validator import (
    IssueCode,
    assumption_load,
    section_loads,
    validate_claims,
)
from tests.evidence.factories import claim, source


def by_id(*sources: Source) -> dict:  # type: ignore[type-arg]
    return {s.id: s for s in sources}


def codes(issues: list) -> set[IssueCode]:  # type: ignore[type-arg]
    return {issue.code for issue in issues}


def test_unsourced_fact_is_demoted_to_assumption() -> None:
    c = claim(ClaimType.FACT)

    issues = validate_claims([c], {})

    assert c.type is ClaimType.ASSUMPTION
    assert c.notes is not None and "auto-demoted" in c.notes
    assert codes(issues) == {IssueCode.UNSOURCED_FACT_DEMOTED}


def test_fact_citing_only_unknown_sources_is_demoted() -> None:
    c = claim(ClaimType.FACT, source_ids=[uuid4()])

    issues = validate_claims([c], {})

    assert c.source_ids == []
    assert c.type is ClaimType.ASSUMPTION
    assert codes(issues) == {IssueCode.DANGLING_SOURCE, IssueCode.UNSOURCED_FACT_DEMOTED}


def test_sourced_fact_is_kept() -> None:
    s = source()
    c = claim(ClaimType.FACT, sources=[s], confidence=Level.MEDIUM)

    issues = validate_claims([c], by_id(s))

    assert c.type is ClaimType.FACT
    assert c.corroboration_count == 1
    assert issues == []


def test_numeric_fact_on_low_tier_sources_becomes_estimate() -> None:
    forum = source("https://reddit.com/r/x/1", tier=Level.LOW)
    c = claim(ClaimType.FACT, sources=[forum], value=300, confidence=Level.MEDIUM)

    issues = validate_claims([c], by_id(forum))

    assert c.type is ClaimType.ESTIMATE
    assert codes(issues) == {IssueCode.LOW_TIER_FACT_DEMOTED}


def test_non_numeric_fact_on_low_tier_source_is_kept() -> None:
    forum = source("https://reddit.com/r/x/1", tier=Level.LOW)
    c = claim(ClaimType.FACT, sources=[forum], confidence=Level.MEDIUM)

    validate_claims([c], by_id(forum))

    assert c.type is ClaimType.FACT


def test_single_source_numeric_claim_is_capped_at_medium() -> None:
    s = source()
    c = claim(ClaimType.FACT, sources=[s], value=4000)

    issues = validate_claims([c], by_id(s))

    assert c.confidence is Level.MEDIUM
    assert codes(issues) == {IssueCode.SINGLE_SOURCE_CAPPED}


def test_two_pages_on_one_host_are_not_independent() -> None:
    a, b = source("https://example.com/a"), source("https://www.example.com/b")
    c = claim(ClaimType.FACT, sources=[a, b], value=4000)

    validate_claims([c], by_id(a, b))

    assert c.corroboration_count == 1
    assert c.confidence is Level.MEDIUM


def test_corroborated_numeric_claim_keeps_high_confidence() -> None:
    a, b = source("https://example.com/a"), source("https://gov.uk/stats")
    c = claim(ClaimType.FACT, sources=[a, b], value=4000)

    issues = validate_claims([c], by_id(a, b))

    assert c.corroboration_count == 2
    assert c.confidence is Level.HIGH
    assert issues == []


def test_conflicts_are_linked_both_ways_and_capped() -> None:
    a, b = source("https://a.com/x"), source("https://b.com/y")
    first = claim(ClaimType.FACT, sources=[a])
    second = claim(ClaimType.FACT, sources=[b])
    first.conflict_ids = [second.id]

    issues = validate_claims([first, second], by_id(a, b))

    assert second.conflict_ids == [first.id]
    assert first.confidence is Level.MEDIUM
    assert second.confidence is Level.MEDIUM
    assert [i.code for i in issues].count(IssueCode.CONFLICTED) == 2


def test_validation_is_idempotent() -> None:
    s = source()
    c = claim(ClaimType.FACT, sources=[s], value=10)
    validate_claims([c], by_id(s))

    assert validate_claims([c], by_id(s)) == []


def test_assumption_load() -> None:
    claims = [
        claim(ClaimType.ASSUMPTION, confidence=Level.MEDIUM),
        claim(ClaimType.ESTIMATE, confidence=Level.LOW),
        claim(ClaimType.ESTIMATE, confidence=Level.MEDIUM),
        claim(ClaimType.FACT, confidence=Level.HIGH),
    ]

    assert assumption_load(claims) == 0.5
    assert assumption_load([]) is None


def test_section_loads_flag_assumption_heavy_sections() -> None:
    opportunity = Opportunity(
        run_id=uuid4(), title="x", business_shape=BusinessShape.SAAS, profile_ref="bootstrap"
    )
    opportunity.market.size = claim(ClaimType.ASSUMPTION, confidence=Level.MEDIUM)
    opportunity.market.growth = claim(ClaimType.ASSUMPTION, confidence=Level.MEDIUM)
    opportunity.market.structure = claim(ClaimType.ESTIMATE, confidence=Level.MEDIUM)
    opportunity.customer.buyer = claim(ClaimType.ESTIMATE, confidence=Level.MEDIUM)

    loads = {load.section: load for load in section_loads(opportunity, threshold=0.5)}

    assert set(loads) == {"market", "customer"}
    assert loads["market"].weak_claims == 2
    assert loads["market"].flagged
    assert not loads["customer"].flagged
