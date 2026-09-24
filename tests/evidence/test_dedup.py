import pytest

from opp.domain.enums import BusinessShape, Level
from opp.evidence.dedup import (
    canonical_url,
    content_tokens,
    fingerprint,
    lead_dedup_hash,
    pick_survivor,
    similarity,
    url_hash,
)
from tests.evidence.conftest import make_lead, make_source


@pytest.mark.parametrize(
    "variant",
    [
        "https://example.com/pricing",
        "http://example.com/pricing",
        "https://www.Example.com/pricing/",
        "https://example.com:443/pricing#plans",
        "https://example.com/pricing?utm_source=x&utm_medium=y",
        "https://example.com//pricing?gclid=abc&fbclid=def",
    ],
)
def test_url_variants_share_one_hash(variant: str) -> None:
    assert url_hash(variant) == url_hash("https://example.com/pricing")


def test_canonical_url_sorts_and_keeps_real_query_params() -> None:
    assert canonical_url("https://a.com/s?q=hvac&page=2&utm_campaign=z") == (
        "https://a.com/s?page=2&q=hvac"
    )
    assert canonical_url("https://a.com") == "https://a.com/"
    assert canonical_url("https://a.com:8443/x") == "https://a.com:8443/x"


def test_different_pages_hash_differently() -> None:
    assert url_hash("https://a.com/x") != url_hash("https://a.com/y")
    assert url_hash("https://a.com/x?id=1") != url_hash("https://a.com/x?id=2")


def test_content_tokens_ignore_case_stopwords_and_plurals() -> None:
    assert content_tokens("The Independent HVAC Contractors") == content_tokens(
        "independent hvac contractor"
    )
    assert content_tokens("agencies") == {"agency"}
    assert content_tokens("business") == {"business"}


def test_fingerprint_is_stable_across_rewording_but_not_across_shape() -> None:
    a = fingerprint("HVAC contractors", "scheduling by phone", BusinessShape.SAAS)
    b = fingerprint("hvac contractor,", "Scheduling, by phone!", BusinessShape.SAAS)
    assert a == b
    assert a != fingerprint("HVAC contractors", "scheduling by phone", BusinessShape.SERVICE)


def test_lead_hash_ignores_why_now_and_title() -> None:
    a = make_lead(title="One")
    b = make_lead(title="Two")
    assert lead_dedup_hash(a) == lead_dedup_hash(b)
    assert lead_dedup_hash(a) != lead_dedup_hash(make_lead(pain="invoicing is slow"))


def test_similarity_catches_near_duplicates() -> None:
    a = make_lead()
    b = make_lead("independent HVAC contractors in Texas", "scheduling technicians by phone")
    c = make_lead("dental clinics", "insurance claim denials")
    assert similarity(a, b) >= 0.6
    assert similarity(a, c) == 0.0
    assert similarity(a, make_lead(business_shape=BusinessShape.SERVICE)) == 0.0


def test_pick_survivor_prefers_stronger_evidence_then_earlier_lead() -> None:
    strong = make_source(credibility_tier=Level.HIGH)
    weak_lead = make_lead()
    strong_lead = make_lead(sources=[strong])
    sources = {strong.id: strong}

    assert pick_survivor(weak_lead, strong_lead, sources).keep is strong_lead
    assert pick_survivor(strong_lead, weak_lead, sources).drop is weak_lead

    first, second = make_lead(), make_lead()
    assert pick_survivor(second, first, {}).keep is first
