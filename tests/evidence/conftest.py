from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from opp.db.base import Base
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import BusinessShape, ClaimType, Level, SignalSource, SourceType
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead


def make_source(url: str = "https://example.com/a", **kwargs: Any) -> Source:
    fields: dict[str, Any] = {
        "title": "A page",
        "source_type": SourceType.TRADE_PRESS,
        "credibility_tier": Level.MEDIUM,
        "excerpt": "the quoted bit",
    }
    fields.update(kwargs)
    return Source(url=url, **fields)


def make_claim(
    statement: str = "a claim",
    type_: ClaimType = ClaimType.FACT,
    *,
    sources: list[Source] | None = None,
    **kwargs: Any,
) -> Claim:
    fields: dict[str, Any] = {
        "confidence": Level.HIGH,
        "confidence_reason": "stated on page",
        "source_ids": [s.id for s in sources or []],
    }
    fields.update(kwargs)
    return Claim(statement=statement, type=type_, **fields)


def make_lead(
    customer: str = "independent HVAC contractors",
    pain: str = "scheduling technicians by phone and spreadsheet",
    *,
    run_id: UUID | None = None,
    sources: list[Source] | None = None,
    **kwargs: Any,
) -> Lead:
    fields: dict[str, Any] = {
        "title": "HVAC scheduling",
        "business_shape": BusinessShape.SAAS,
        "signal_source": SignalSource.UNMET_NEED,
    }
    fields.update(kwargs)
    return Lead(
        run_id=run_id or uuid4(),
        named_customer=make_claim(customer, sources=sources),
        specific_pain=make_claim(pain, sources=sources),
        why_now=make_claim("new EPA refrigerant rules from 2025", sources=sources),
        **fields,
    )


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as db:
        yield db
    engine.dispose()
