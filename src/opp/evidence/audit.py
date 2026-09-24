"""Evidence audit: re-checks a run's persisted evidence against the invariants
(specs.md §25.1-§25.3, plan.md §14 "a test-enforced invariant").

It reads what is actually in the DB, independently of the code that wrote it:

- every lead cites at least one source, and every cited source exists;
- every ``fact`` claim cites at least one existing source;
- each lead claim is in the claims index, and its claim->source links are there too.

An empty result means the run is clean. Used by tests and by ``opp check``.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opp.db import models
from opp.db.repositories import ClaimRepository, LeadRepository, SourceRepository
from opp.domain.enums import ClaimType


@dataclass(frozen=True)
class Violation:
    subject: str
    problem: str

    def __str__(self) -> str:
        return f"{self.subject}: {self.problem}"


def audit_run(session: Session, run_id: UUID) -> list[Violation]:
    leads = LeadRepository(session).list_for_run(run_id)
    claims = ClaimRepository(session)
    all_source_ids = {sid for lead in leads for claim in lead.claims() for sid in claim.source_ids}
    stored_sources = SourceRepository(session).get_many(all_source_ids)
    links: dict[UUID, set[UUID]] = {}
    for claim_id, source_id in session.execute(
        select(models.ClaimSourceRow.claim_id, models.ClaimSourceRow.source_id)
    ):
        links.setdefault(claim_id, set()).add(source_id)

    violations: list[Violation] = []
    for lead in leads:
        subject = f"lead {lead.id} ({lead.title!r})"
        if not lead.source_ids:
            violations.append(Violation(subject, "cites no source"))
        for claim in lead.claims():
            missing = [sid for sid in claim.source_ids if sid not in stored_sources]
            if missing:
                violations.append(
                    Violation(subject, f"claim {claim.id} cites {len(missing)} unknown source(s)")
                )
            if claim.type is ClaimType.FACT and not claim.source_ids:
                violations.append(Violation(subject, f"fact claim {claim.id} has no source"))
            if claims.get(claim.id) is None:
                violations.append(Violation(subject, f"claim {claim.id} missing from the index"))
            elif set(claim.source_ids) - links.get(claim.id, set()):
                violations.append(Violation(subject, f"claim {claim.id} has unlinked sources"))
    return violations
