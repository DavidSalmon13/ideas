from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from opp.db import models
from opp.db.base import Base
from opp.db.repositories import ClaimRepository, LeadRepository, RunRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import BusinessShape, ClaimType, DedupStatus, Level
from opp.domain.opportunity import Opportunity
from opp.domain.run import ResearchRun
from opp.evidence.store import EvidenceStore
from opp.evidence.validator import IssueCode
from tests.evidence.factories import claim, lead, source


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as db:
        yield db
    engine.dispose()


def test_same_page_is_stored_once(session: Session) -> None:
    store = EvidenceStore(session)
    first = store.add_source(source("https://example.com/pricing"))

    second = store.add_source(source("https://www.example.com/pricing/?utm_source=x"))
    session.commit()

    assert second.id == first.id
    assert len(session.scalars(select(models.SourceRow)).all()) == 1


def test_unsourced_fact_is_stored_as_assumption(session: Session) -> None:
    """Phase 1 acceptance (plan.md §24)."""
    store = EvidenceStore(session)
    c = claim(ClaimType.FACT)

    issues = store.add_claims([c])
    session.commit()

    stored = ClaimRepository(session).get(c.id)
    assert stored is not None
    assert stored.type is ClaimType.ASSUMPTION
    assert [issue.code for issue in issues] == [IssueCode.UNSOURCED_FACT_DEMOTED]


def test_sourced_claim_is_stored_with_its_source_links(session: Session) -> None:
    store = EvidenceStore(session)
    s = store.add_source(source())
    c = claim(ClaimType.FACT, sources=[s], confidence=Level.MEDIUM)

    store.add_claims([c])
    session.commit()

    links = session.scalars(select(models.ClaimSourceRow)).all()
    assert [(link.claim_id, link.source_id) for link in links] == [(c.id, s.id)]
    stored = ClaimRepository(session).get(c.id)
    assert stored is not None and stored.type is ClaimType.FACT


def test_dangling_source_is_dropped_before_storing(session: Session) -> None:
    store = EvidenceStore(session)
    c = claim(ClaimType.ESTIMATE, source_ids=[uuid4()])

    store.add_claims([c])
    session.commit()

    assert session.scalars(select(models.ClaimSourceRow)).all() == []


def test_validate_opportunity_corrects_claims_and_reports_loads(session: Session) -> None:
    store = EvidenceStore(session, assumption_load_threshold=0.4)
    s = store.add_source(source())
    opportunity = Opportunity(
        run_id=uuid4(), title="x", business_shape=BusinessShape.SAAS, profile_ref="bootstrap"
    )
    opportunity.market.size = claim(ClaimType.FACT, value=4000)
    opportunity.market.growth = claim(ClaimType.FACT, sources=[s], confidence=Level.MEDIUM)

    result = store.validate_opportunity(opportunity)

    size = opportunity.market.size
    assert size.type is ClaimType.ASSUMPTION
    assert size.confidence is Level.MEDIUM
    assert {issue.code for issue in result.issues} == {
        IssueCode.UNSOURCED_FACT_DEMOTED,
        IssueCode.SINGLE_SOURCE_CAPPED,
    }
    assert result.flagged_sections == ["market"]


def test_lead_dedup_across_runs(session: Session) -> None:
    store = EvidenceStore(session)
    run = RunRepository(session).add(ResearchRun())
    first = lead("Independent physio clinics", "No-shows cost revenue")
    first.run_id = run.id

    check = store.check_lead(first)
    LeadRepository(session).add(first)
    session.commit()

    assert not check.is_duplicate
    assert first.dedup_hash == check.dedup_hash

    again = store.check_lead(lead("independent physio clinic", "no shows cost revenue"))
    assert again.is_duplicate
    assert again.duplicate_of is not None and again.duplicate_of.lead_id == first.id


def test_killed_leads_are_never_resurfaced(session: Session) -> None:
    store = EvidenceStore(session)
    killed = lead("Dental clinics", "Insurance claims are rejected")
    check = store.check_lead(killed)
    store.retire(check.dedup_hash, DedupStatus.KILLED)
    session.commit()

    later = store.check_lead(lead("dental clinic", "insurance claims rejected"))

    assert later.is_duplicate
    assert later.duplicate_of is not None
    assert later.duplicate_of.status is DedupStatus.KILLED
