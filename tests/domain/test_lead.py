from uuid import uuid4

import pytest
from pydantic import ValidationError

from opp.domain import Kill, Lead
from opp.domain.enums import BusinessShape, FailedGate, KillSubject, LeadStatus, SignalSource
from tests.domain.test_evidence import make_claim


def make_lead() -> Lead:
    shared, other = uuid4(), uuid4()
    return Lead(
        run_id=uuid4(),
        title="Appointment no-show recovery for physiotherapy clinics",
        business_shape=BusinessShape.SAAS,
        named_customer=make_claim(statement="Independent physio clinics", source_ids=[shared]),
        specific_pain=make_claim(statement="No-shows cost clinics revenue", source_ids=[shared]),
        why_now=make_claim(statement="New insurer rule on cancellations", source_ids=[other]),
        signal_source=SignalSource.UNMET_NEED,
    )


def test_lead_defaults() -> None:
    lead = make_lead()

    assert lead.status is LeadStatus.NEW
    assert len(list(lead.claims())) == 3


def test_lead_source_ids_are_the_union_of_its_claims() -> None:
    lead = make_lead()

    assert len(lead.source_ids) == 2


def test_lead_requires_the_three_specificity_claims() -> None:
    with pytest.raises(ValidationError):
        Lead.model_validate(
            {
                "run_id": uuid4(),
                "title": "Vague idea",
                "business_shape": "saas",
                "signal_source": "unmet_need",
            }
        )


def test_kill_record() -> None:
    kill = Kill(
        subject_kind=KillSubject.LEAD,
        subject_id=uuid4(),
        stage="screening",
        reason="Customer is 'small businesses'",
        failed_gate=FailedGate.NAMED_CUSTOMER,
    )

    assert kill.killed_at.tzinfo is not None
