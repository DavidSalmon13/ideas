from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opp.db import models
from opp.db.base import Base
from opp.db.repositories import (
    ClaimRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    RunRepository,
)
from opp.domain.enums import BusinessShape, ClaimType, DedupStatus, FailedGate, LeadStatus, Level
from opp.domain.lead import UnsourcedLeadError
from opp.domain.opportunity import Customer, Market, Opportunity, Problem
from opp.domain.run import ResearchRun
from opp.evidence.store import EvidenceStore
from tests.evidence.conftest import make_claim, make_lead, make_source


def _run(session: Session) -> UUID:
    run = ResearchRun()
    RunRepository(session).add(run)
    return run.id


def _count(session: Session, table: type[Base], **where: object) -> int:
    stmt = select(func.count()).select_from(table)
    for column, value in where.items():
        stmt = stmt.where(getattr(table, column) == value)
    return session.scalar(stmt) or 0


def test_add_source_dedups_by_canonical_url(session: Session) -> None:
    store = EvidenceStore(session)
    first = store.add_source(make_source("https://example.com/pricing"))
    again = store.add_source(make_source("http://www.example.com/pricing/?utm_source=x"))
    other = store.add_source(make_source("https://example.com/about"))
    session.commit()

    assert again.id == first.id
    assert other.id != first.id
    assert _count(session, models.SourceRow) == 2


def test_unsourced_fact_on_a_lead_is_stored_as_assumption(session: Session) -> None:
    store = EvidenceStore(session)
    source = store.add_source(make_source())
    lead = make_lead(run_id=_run(session), sources=[source])
    lead.why_now = make_claim("rules changed recently")  # a "fact" with no source

    saved, report = store.save_lead(lead)
    session.commit()

    assert saved.dedup_hash
    stored = LeadRepository(session).get(lead.id)
    assert stored is not None
    assert stored.why_now.type is ClaimType.ASSUMPTION
    assert stored.named_customer.type is ClaimType.FACT
    assert report.assumption_load["lead"].weak == 1

    claims = {c.id: c for c in ClaimRepository(session).list_for_lead(lead.id)}
    assert claims[lead.why_now.id].type is ClaimType.ASSUMPTION
    assert _count(session, models.ClaimSourceRow) == 2  # customer + pain cite the source


def test_saving_a_lead_twice_does_not_duplicate_claims(session: Session) -> None:
    store = EvidenceStore(session)
    source = store.add_source(make_source())
    lead = make_lead(run_id=_run(session), sources=[source])

    saved, _ = store.save_lead(lead)
    saved.status = LeadStatus.KEPT
    store.save_lead(saved)
    session.commit()

    assert _count(session, models.ClaimRow, lead_id=lead.id) == 3
    assert _count(session, models.ClaimSourceRow) == 3
    assert LeadRepository(session).get(lead.id).status is LeadStatus.KEPT  # type: ignore[union-attr]


def test_find_duplicate_within_candidates(session: Session) -> None:
    store = EvidenceStore(session)
    run_id = _run(session)
    src = [store.add_source(make_source())]
    a, _ = store.save_lead(make_lead(run_id=run_id, sources=src))
    b, _ = store.save_lead(
        make_lead(
            "independent HVAC contractors",
            "scheduling technicians by phone",
            run_id=run_id,
            sources=src,
        )
    )
    unrelated, _ = store.save_lead(
        make_lead("dental clinics", "claim denials", run_id=run_id, sources=src)
    )

    match = store.find_duplicate(b, [a, b, unrelated])
    assert match is not None and match.lead_id == a.id
    assert not match.previously_rejected
    assert store.find_duplicate(unrelated, [a, b, unrelated]) is None

    a.status = LeadStatus.KILLED
    assert store.find_duplicate(b, [a, b]) is None  # killed leads aren't a match


def test_killed_lead_is_never_resurfaced_in_a_later_run(session: Session) -> None:
    store = EvidenceStore(session)
    old, _ = store.save_lead(
        make_lead(run_id=_run(session), sources=[store.add_source(make_source())])
    )
    store.index_lead(old, DedupStatus.KILLED)
    session.commit()

    new = make_lead(
        "Independent HVAC contractor.", "Scheduling technicians by phone & spreadsheets"
    )
    match = store.find_duplicate(new)

    assert match is not None
    assert match.previously_rejected
    assert match.lead_id == old.id
    assert store.find_duplicate(old) is None  # a lead is not a duplicate of itself


def test_resolve_duplicate_keeps_stronger_evidence_and_logs_a_kill(session: Session) -> None:
    store = EvidenceStore(session)
    run_id = _run(session)
    strong_source = store.add_source(make_source(credibility_tier=Level.HIGH))
    forum = store.add_source(
        make_source("https://forum.example.com/t/1", credibility_tier=Level.LOW)
    )
    weak, _ = store.save_lead(make_lead(run_id=run_id, sources=[forum]))
    strong, _ = store.save_lead(make_lead(run_id=run_id, sources=[strong_source]))

    pair = store.resolve_duplicate(weak, strong, stage="screening")
    session.commit()

    assert pair.keep.id == strong.id
    assert LeadRepository(session).get(weak.id).status is LeadStatus.DUPLICATE  # type: ignore[union-attr]
    [kill] = KillRepository(session).list_for_subject(weak.id)
    assert kill.failed_gate is FailedGate.DUPLICATE
    assert str(strong.id) in kill.reason


def _opportunity(run_id: UUID, lead_id: UUID | None, source_id: UUID) -> Opportunity:
    return Opportunity(
        run_id=run_id,
        lead_id=lead_id,
        title="HVAC scheduling",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        problem=Problem(description=make_claim("scheduling by phone and spreadsheet")),
        customer=Customer(segment=make_claim("independent HVAC contractors")),
        market=Market(
            size=make_claim("40,000 firms", value=40_000, source_ids=[source_id]),
        ),
    )


def test_save_opportunity_validates_and_indexes_claims(session: Session) -> None:
    store = EvidenceStore(session)
    source = store.add_source(make_source())
    opp = _opportunity(_run(session), None, source.id)

    saved, report = store.save_opportunity(opp)
    session.commit()

    stored = OpportunityRepository(session).get(opp.id)
    assert stored is not None
    assert stored.problem.description.type is ClaimType.ASSUMPTION  # type: ignore[union-attr]
    size = stored.market.size
    assert size is not None
    assert (size.type, size.confidence) == (ClaimType.FACT, Level.MEDIUM)  # single source
    assert report.flagged_sections == ["problem", "customer"]
    assert len(ClaimRepository(session).list_for_opportunity(opp.id)) == 3

    provenance = store.trace(size.id)
    assert provenance is not None
    assert [s.id for s in provenance.sources] == [source.id]
    assert saved.market.size is not None


def test_save_opportunity_prunes_removed_claims_but_keeps_lead_claims(session: Session) -> None:
    store = EvidenceStore(session)
    run_id = _run(session)
    source = store.add_source(make_source())
    lead, _ = store.save_lead(make_lead(run_id=run_id, sources=[source]))
    opp = _opportunity(run_id, lead.id, source.id)
    opp.why_now = lead.why_now  # the opportunity reuses the lead's claim
    saved, _ = store.save_opportunity(opp)

    removed_id = saved.market.size.id  # type: ignore[union-attr]
    saved.market.size = None
    saved.why_now = None
    store.save_opportunity(saved)
    session.commit()

    assert ClaimRepository(session).get(removed_id) is None
    assert _count(session, models.ClaimSourceRow, claim_id=removed_id) == 0
    shared = session.get(models.ClaimRow, lead.why_now.id)
    assert shared is not None
    assert (shared.lead_id, shared.opportunity_id) == (lead.id, None)


def test_discarded_opportunity_blocks_its_lead_from_coming_back(session: Session) -> None:
    store = EvidenceStore(session)
    run_id = _run(session)
    source = store.add_source(make_source())
    lead, _ = store.save_lead(make_lead(run_id=run_id, sources=[source]))
    opp, _ = store.save_opportunity(_opportunity(run_id, lead.id, source.id))

    store.index_opportunity(opp, DedupStatus.DISCARDED)
    session.commit()

    match = store.find_duplicate(make_lead())
    assert match is not None
    assert match.indexed_status is DedupStatus.DISCARDED
    assert match.opportunity_id == opp.id


def test_validate_does_not_write(session: Session) -> None:
    store = EvidenceStore(session)
    [out], report = store.validate([make_claim()])
    assert out.type is ClaimType.ASSUMPTION
    assert report.issues
    assert _count(session, models.ClaimRow) == 0


def test_a_lead_whose_sources_do_not_resolve_is_refused(session: Session) -> None:
    store = EvidenceStore(session)
    run_id = _run(session)
    ghost = make_source("https://never-stored.example.com/")  # not added to the store

    with pytest.raises(UnsourcedLeadError):
        store.save_lead(make_lead(run_id=run_id, sources=[ghost]))
    with pytest.raises(UnsourcedLeadError):
        store.save_lead(make_lead(run_id=run_id))
    session.commit()
    assert LeadRepository(session).list_for_run(run_id) == []
