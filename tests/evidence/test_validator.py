from uuid import uuid4

from opp.domain.enums import BusinessShape, ClaimType, Level
from opp.domain.evidence import Claim
from opp.domain.opportunity import Market, Opportunity, Problem
from opp.evidence.validator import (
    IssueKind,
    assumption_load,
    independent_source_count,
    validate_claims,
    validate_lead,
    validate_opportunity,
)
from tests.evidence.conftest import make_claim, make_lead, make_source


def test_unsourced_fact_is_demoted_to_assumption() -> None:
    claim = make_claim("HVAC firms lose 10 hours a week", confidence=Level.MEDIUM)

    [out], report = validate_claims([claim], {})

    assert out.type is ClaimType.ASSUMPTION
    assert out.id == claim.id
    assert "demoted" in (out.notes or "")
    assert report.claim_ids(IssueKind.DEMOTED_UNSOURCED) == {claim.id}
    assert claim.type is ClaimType.FACT  # input is not mutated


def test_fact_citing_unknown_source_is_treated_as_unsourced() -> None:
    claim = make_claim(source_ids=[uuid4()])

    [out], report = validate_claims([claim], {})

    assert out.source_ids == []
    assert out.type is ClaimType.ASSUMPTION
    assert report.claim_ids(IssueKind.DANGLING_SOURCE) == {claim.id}


def test_sourced_fact_survives_and_other_types_are_left_alone() -> None:
    source = make_source()
    fact = make_claim(sources=[source], confidence=Level.MEDIUM)
    estimate = make_claim(type_=ClaimType.ESTIMATE)
    unknown = make_claim(type_=ClaimType.UNKNOWN, confidence=Level.LOW)

    out, report = validate_claims([fact, estimate, unknown], {source.id: source})

    assert [c.type for c in out] == [ClaimType.FACT, ClaimType.ESTIMATE, ClaimType.UNKNOWN]
    assert out[0].corroboration_count == 1
    assert report.issues == []


def test_quantitative_fact_on_low_tier_sources_only_becomes_estimate() -> None:
    forum = make_source(credibility_tier=Level.LOW)
    claim = make_claim(sources=[forum], value=300, unit="USD/month")

    [out], report = validate_claims([claim], {forum.id: forum})

    assert out.type is ClaimType.ESTIMATE
    assert report.claim_ids(IssueKind.DEMOTED_LOW_TIER) == {claim.id}


def test_qualitative_fact_may_rest_on_a_low_tier_source() -> None:
    forum = make_source(credibility_tier=Level.LOW)
    claim = make_claim("contractors complain about double-booking", sources=[forum])

    [out], _ = validate_claims([claim], {forum.id: forum})

    assert out.type is ClaimType.FACT


def test_single_source_quantitative_fact_is_capped_at_medium() -> None:
    a = make_source("https://a.com/1", publisher="Acme Research")
    b = make_source("https://b.com/1", publisher="acme research ")  # same publisher
    claim = make_claim(sources=[a, b], value=12_000, value_low=10_000)

    [out], report = validate_claims([claim], {a.id: a, b.id: b})

    assert out.type is ClaimType.FACT
    assert out.corroboration_count == 1
    assert out.confidence is Level.MEDIUM
    assert report.claim_ids(IssueKind.CAPPED_SINGLE_SOURCE) == {claim.id}


def test_two_independent_sources_keep_high_confidence() -> None:
    a = make_source("https://www.a.com/1")
    b = make_source("https://b.org/report")
    claim = make_claim(sources=[a, b], value=12_000)

    [out], report = validate_claims([claim], {a.id: a, b.id: b})

    assert out.corroboration_count == 2
    assert out.confidence is Level.HIGH
    assert report.issues == []


def test_independence_uses_host_when_publisher_is_missing() -> None:
    a = make_source("https://www.a.com/1")
    b = make_source("https://a.com/2")
    c = make_source("https://c.com/", publisher="A.com")
    sources = {s.id: s for s in (a, b, c)}
    assert independent_source_count([a.id, b.id, c.id, uuid4()], sources) == 1


def test_conflicts_are_linked_both_ways_and_capped() -> None:
    source = make_source()
    a = make_claim("market is growing", sources=[source])
    b = make_claim("market is shrinking", sources=[source], conflict_ids=[a.id])

    (out_a, out_b), report = validate_claims([a, b], {source.id: source})

    assert out_a.conflict_ids == [b.id]
    assert out_b.conflict_ids == [a.id]
    assert out_a.confidence is out_b.confidence is Level.MEDIUM
    assert report.claim_ids(IssueKind.CONFLICTED) == {a.id, b.id}


def test_validation_is_idempotent() -> None:
    source = make_source()
    claims = [
        make_claim("unsourced"),
        make_claim("single", sources=[source], value=5),
        make_claim("contested", sources=[source], conflict_ids=[uuid4()]),
    ]
    once, _ = validate_claims(claims, {source.id: source})

    twice, report = validate_claims(once, {source.id: source})

    assert [c.model_dump() for c in twice] == [c.model_dump() for c in once]
    # Only the standing conflict is reported again, and it adds no new notes.
    assert {issue.kind for issue in report.issues} == {IssueKind.CONFLICTED}


def test_assumption_load_counts_assumptions_unknowns_and_low_confidence() -> None:
    claims = [
        make_claim(),  # fact, high
        make_claim(type_=ClaimType.ESTIMATE, confidence=Level.MEDIUM),
        make_claim(type_=ClaimType.ASSUMPTION, confidence=Level.MEDIUM),
        make_claim(type_=ClaimType.UNKNOWN, confidence=Level.MEDIUM),
        make_claim(type_=ClaimType.ESTIMATE, confidence=Level.LOW),
    ]
    load = assumption_load(claims)
    assert (load.total, load.weak) == (5, 3)
    assert load.load == 0.6
    assert load.high
    assert assumption_load([]).load == 0.0
    assert not assumption_load([]).high


def test_validate_lead_reports_one_section() -> None:
    source = make_source()
    lead = make_lead(sources=[source])
    lead.why_now = make_claim("no source here")

    out, report = validate_lead(lead, {source.id: source})

    assert out.why_now.type is ClaimType.ASSUMPTION
    assert out.named_customer.type is ClaimType.FACT
    assert report.assumption_load["lead"].weak == 1


def _claim(type_: ClaimType, confidence: Level = Level.MEDIUM) -> Claim:
    return make_claim(type_=type_, confidence=confidence)


def test_validate_opportunity_computes_per_section_load_and_flags_weak_sections() -> None:
    source = make_source()
    opp = Opportunity(
        run_id=uuid4(),
        title="HVAC scheduling",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        problem=Problem(
            description=make_claim("double bookings", sources=[source]),
            frequency=_claim(ClaimType.ESTIMATE),
        ),
        market=Market(
            size=make_claim("40,000 firms", value=40_000),  # unsourced -> assumption
            growth=_claim(ClaimType.UNKNOWN, Level.LOW),
        ),
    )

    out, report = validate_opportunity(opp, {source.id: source})

    assert out.market.size is not None and out.market.size.type is ClaimType.ASSUMPTION
    assert set(report.assumption_load) == {"problem", "market"}
    assert report.assumption_load["problem"].load == 0.0
    assert report.assumption_load["market"].load == 1.0
    assert report.flagged_sections == ["market"]
    assert opp.market.size is not None and opp.market.size.type is ClaimType.FACT
