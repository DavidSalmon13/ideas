from uuid import uuid4

import pytest
from pydantic import ValidationError

from opp.domain import Claim, Source
from opp.domain.enums import ClaimType, Level, SourceType, ValueStatus


def make_claim(**overrides: object) -> Claim:
    fields: dict[str, object] = {
        "statement": "Dental clinics pay ~$300/month for scheduling software",
        "type": ClaimType.ESTIMATE,
        "confidence": Level.MEDIUM,
        "confidence_reason": "Two vendor pricing pages agree",
    }
    fields.update(overrides)
    return Claim.model_validate(fields)


def test_claim_defaults_and_ids() -> None:
    first, second = make_claim(), make_claim()

    assert first.id != second.id
    assert first.source_ids == []
    assert first.corroboration_count == 0
    assert first.observed_at.tzinfo is not None


@pytest.mark.parametrize("reason", ["", "   "])
def test_claim_requires_confidence_reason(reason: str) -> None:
    with pytest.raises(ValidationError):
        make_claim(confidence_reason=reason)


def test_claim_requires_statement() -> None:
    with pytest.raises(ValidationError):
        make_claim(statement=" ")


def test_unsourced_fact_is_accepted_for_the_validator_to_demote() -> None:
    claim = make_claim(type=ClaimType.FACT)

    assert claim.type is ClaimType.FACT
    assert not claim.is_sourced


@pytest.mark.parametrize(
    ("claim_type", "status"),
    [
        (ClaimType.FACT, ValueStatus.VERIFIED),
        (ClaimType.ESTIMATE, ValueStatus.ESTIMATED),
        (ClaimType.ASSUMPTION, ValueStatus.ASSUMED),
        (ClaimType.UNKNOWN, ValueStatus.UNKNOWN),
    ],
)
def test_value_status_tag(claim_type: ClaimType, status: ValueStatus) -> None:
    assert make_claim(type=claim_type).value_status is status


def test_value_range_accepted() -> None:
    claim = make_claim(value=300, value_low=200, value_high=400, unit="USD/month")

    assert claim.value == 300


@pytest.mark.parametrize(
    "values",
    [
        {"value_low": 500, "value_high": 400},
        {"value": 100, "value_low": 200},
        {"value": 500, "value_high": 400},
    ],
)
def test_value_range_rejected(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        make_claim(**values)


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        make_claim(made_up_field=1)


def test_assignment_is_validated() -> None:
    claim = make_claim()

    with pytest.raises(ValidationError):
        claim.confidence_reason = ""


def test_source_accepts_http_urls() -> None:
    source = Source(
        url="https://example.com/pricing",
        title="Pricing",
        source_type=SourceType.PRICING_PAGE,
        credibility_tier=Level.MEDIUM,
        excerpt="$299/month",
    )

    assert source.accessed_date is not None


@pytest.mark.parametrize("url", ["ftp://example.com/x", "example.com/x", "file:///etc/passwd"])
def test_source_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        Source(
            url=url,
            title="x",
            source_type=SourceType.OTHER,
            credibility_tier=Level.LOW,
            excerpt="x",
        )


def test_claim_links_sources_by_id() -> None:
    source_id = uuid4()

    claim = make_claim(type=ClaimType.FACT, source_ids=[source_id])

    assert claim.is_sourced
    assert claim.source_ids == [source_id]
