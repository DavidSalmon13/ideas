from opp.domain.enums import BusinessShape
from opp.evidence.dedup import lead_dedup_hash, normalize_text
from tests.evidence.factories import lead


def test_normalize_text_ignores_case_order_punctuation_and_plurals() -> None:
    assert normalize_text("Independent physio clinics!") == normalize_text(
        "the clinic, independent PHYSIO"
    )


def test_reworded_lead_is_a_duplicate() -> None:
    first = lead("Independent physio clinics", "No-shows cost them revenue")
    second = lead("independent physio clinic", "no shows cost revenue")

    assert lead_dedup_hash(first) == lead_dedup_hash(second)


def test_different_customer_pain_or_shape_is_not_a_duplicate() -> None:
    base = lead("Independent physio clinics", "No-shows cost revenue")

    assert lead_dedup_hash(base) != lead_dedup_hash(lead("Dental clinics", "No-shows cost revenue"))
    assert lead_dedup_hash(base) != lead_dedup_hash(
        lead("Independent physio clinics", "Insurance paperwork takes hours")
    )
    assert lead_dedup_hash(base) != lead_dedup_hash(
        lead("Independent physio clinics", "No-shows cost revenue", BusinessShape.SERVICE)
    )
