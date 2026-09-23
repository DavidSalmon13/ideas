from uuid import UUID, uuid4

from opp.domain.enums import BusinessShape, ClaimType, Level, SignalSource, SourceType
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead


def source(url: str = "https://example.com/a", tier: Level = Level.HIGH) -> Source:
    return Source(
        url=url,
        title="A page",
        source_type=SourceType.INDUSTRY_REPORT,
        credibility_tier=tier,
        excerpt="the quoted evidence",
    )


def claim(
    type_: ClaimType = ClaimType.FACT,
    *,
    sources: list[Source] | None = None,
    source_ids: list[UUID] | None = None,
    confidence: Level = Level.HIGH,
    value: float | None = None,
    statement: str = "A statement",
) -> Claim:
    ids = source_ids if source_ids is not None else [s.id for s in sources or []]
    return Claim(
        statement=statement,
        type=type_,
        source_ids=ids,
        confidence=confidence,
        confidence_reason="Because",
        value=value,
    )


def lead(customer: str, pain: str, shape: BusinessShape = BusinessShape.SAAS) -> Lead:
    return Lead(
        run_id=uuid4(),
        title=f"{customer}: {pain}",
        business_shape=shape,
        named_customer=claim(ClaimType.ESTIMATE, statement=customer),
        specific_pain=claim(ClaimType.ESTIMATE, statement=pain),
        why_now=claim(ClaimType.ESTIMATE, statement="A recent rule change"),
        signal_source=SignalSource.UNMET_NEED,
    )
