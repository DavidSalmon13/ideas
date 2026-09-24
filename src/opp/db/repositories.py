"""Repositories: the only way agents and the orchestrator touch the DB (plan.md §31).

Each repository maps a Pydantic domain object to its row and back. Reads reconstruct the
domain object from the ``data`` column; writes also fill the projected index columns.
Repositories never commit — the caller owns the transaction.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from opp.db import models
from opp.db.models import DocRow
from opp.domain.base import new_id, utcnow
from opp.domain.enums import (
    TERMINAL_RUN_STATUSES,
    DedupStatus,
    LeadStatus,
    OpportunityStatus,
    RunStatus,
    Verdict,
)
from opp.domain.evaluation import Evaluation
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Kill, Lead
from opp.domain.opportunity import Experiment, Opportunity
from opp.domain.report import Report, Trace
from opp.domain.run import AgentRun, ResearchRun, Stage
from opp.evidence.dedup import url_hash as url_hash  # re-exported for existing callers


class ReadRepository[TDomain: BaseModel, TRow: DocRow]:
    """Read side shared by every document-backed repository."""

    _row_type: type[TRow]
    _domain_type: type[TDomain]

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, id_: UUID) -> TDomain | None:
        row = self._session.get(self._row_type, id_)
        return self._domain_type.model_validate(row.data) if row is not None else None

    def get_many(self, ids: Iterable[UUID]) -> dict[UUID, TDomain]:
        wanted = set(ids)
        if not wanted:
            return {}
        rows = self._session.scalars(select(self._row_type).where(self._row_type.id.in_(wanted)))
        return {row.id: self._domain_type.model_validate(row.data) for row in rows}

    def _all(self, stmt: Select[tuple[TRow]]) -> list[TDomain]:
        return [self._domain_type.model_validate(row.data) for row in self._session.scalars(stmt)]


class DocRepository[TDomain: BaseModel, TRow: DocRow](ReadRepository[TDomain, TRow]):
    """A standalone aggregate: written whole, so it owns ``add``/``upsert``. Child
    records (claims, evaluations, experiments) instead extend ``ReadRepository`` and
    take their owner id on a custom ``add``."""

    def _row(self, obj: TDomain) -> TRow:
        raise NotImplementedError

    def add(self, obj: TDomain) -> TDomain:
        self._session.add(self._row(obj))
        return obj

    def upsert(self, obj: TDomain) -> TDomain:
        self._session.merge(self._row(obj))
        return obj


class RunRepository(DocRepository[ResearchRun, models.RunRow]):
    _row_type = models.RunRow
    _domain_type = ResearchRun

    def _row(self, obj: ResearchRun) -> models.RunRow:
        return models.RunRow(
            id=obj.id,
            status=obj.status.value,
            profile_ref=obj.run_config.profile_ref,
            budget_ceiling_usd=obj.budget_ceiling_usd,
            cost_to_date_usd=obj.cost_to_date_usd,
            current_stage=obj.current_stage.value if obj.current_stage else None,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
            data=obj.model_dump(mode="json"),
        )

    def list_by_status(self, status: RunStatus) -> list[ResearchRun]:
        return self._all(select(models.RunRow).where(models.RunRow.status == status.value))

    def list_resumable(self) -> list[ResearchRun]:
        """Non-terminal runs the recovery scan should continue (plan.md §10)."""
        terminal = [s.value for s in TERMINAL_RUN_STATUSES]
        return self._all(select(models.RunRow).where(models.RunRow.status.notin_(terminal)))


class StageRepository(DocRepository[Stage, models.StageRow]):
    _row_type = models.StageRow
    _domain_type = Stage

    def _row(self, obj: Stage) -> models.StageRow:
        return models.StageRow(
            id=obj.id,
            run_id=obj.run_id,
            name=obj.name.value,
            status=obj.status.value,
            attempt=obj.attempt,
            kickback_count=obj.kickback_count,
            cost_usd=obj.cost_usd,
            started_at=obj.started_at,
            ended_at=obj.ended_at,
            data=obj.model_dump(mode="json"),
        )

    def list_for_run(self, run_id: UUID) -> list[Stage]:
        return self._all(select(models.StageRow).where(models.StageRow.run_id == run_id))


class AgentRunRepository(DocRepository[AgentRun, models.AgentRunRow]):
    _row_type = models.AgentRunRow
    _domain_type = AgentRun

    def _row(self, obj: AgentRun) -> models.AgentRunRow:
        return models.AgentRunRow(
            id=obj.id,
            stage_id=obj.stage_id,
            agent_name=obj.agent_name,
            status=obj.status.value,
            model_tier=obj.model_tier.value,
            prompt_version=obj.prompt_version,
            cost_usd=obj.cost_usd,
            error=obj.error,
            started_at=obj.started_at,
            ended_at=obj.ended_at,
            data=obj.model_dump(mode="json"),
        )

    def list_for_stage(self, stage_id: UUID) -> list[AgentRun]:
        return self._all(select(models.AgentRunRow).where(models.AgentRunRow.stage_id == stage_id))


class LeadRepository(DocRepository[Lead, models.LeadRow]):
    _row_type = models.LeadRow
    _domain_type = Lead

    def _row(self, obj: Lead) -> models.LeadRow:
        return models.LeadRow(
            id=obj.id,
            run_id=obj.run_id,
            business_shape=obj.business_shape.value,
            signal_source=obj.signal_source.value,
            dedup_hash=obj.dedup_hash,
            status=obj.status.value,
            discovered_at=obj.discovered_at,
            data=obj.model_dump(mode="json"),
        )

    def list_for_run(self, run_id: UUID, status: LeadStatus | None = None) -> list[Lead]:
        stmt = select(models.LeadRow).where(models.LeadRow.run_id == run_id)
        if status is not None:
            stmt = stmt.where(models.LeadRow.status == status.value)
        return self._all(stmt)

    def get_by_dedup_hash(self, dedup_hash: str) -> list[Lead]:
        return self._all(select(models.LeadRow).where(models.LeadRow.dedup_hash == dedup_hash))


class OpportunityRepository(DocRepository[Opportunity, models.OpportunityRow]):
    _row_type = models.OpportunityRow
    _domain_type = Opportunity

    def _row(self, obj: Opportunity) -> models.OpportunityRow:
        evaluation = obj.evaluation
        return models.OpportunityRow(
            id=obj.id,
            run_id=obj.run_id,
            lead_id=obj.lead_id,
            status=obj.status.value,
            business_shape=obj.business_shape.value,
            composite=evaluation.composite if evaluation else None,
            verdict=evaluation.verdict.value if evaluation else None,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
            data=obj.model_dump(mode="json"),
        )

    def list(
        self,
        *,
        run_id: UUID | None = None,
        status: OpportunityStatus | None = None,
        verdict: Verdict | None = None,
    ) -> list[Opportunity]:
        stmt = select(models.OpportunityRow)
        if run_id is not None:
            stmt = stmt.where(models.OpportunityRow.run_id == run_id)
        if status is not None:
            stmt = stmt.where(models.OpportunityRow.status == status.value)
        if verdict is not None:
            stmt = stmt.where(models.OpportunityRow.verdict == verdict.value)
        return self._all(stmt)


class SourceRepository(DocRepository[Source, models.SourceRow]):
    _row_type = models.SourceRow
    _domain_type = Source

    def _row(self, obj: Source) -> models.SourceRow:
        return models.SourceRow(
            id=obj.id,
            url=obj.url,
            url_hash=url_hash(obj.url),
            source_type=obj.source_type.value,
            credibility_tier=obj.credibility_tier.value,
            published_date=obj.published_date,
            accessed_date=obj.accessed_date,
            data=obj.model_dump(mode="json"),
        )

    def get_by_url_hash(self, hash_: str) -> Source | None:
        row = self._session.scalars(
            select(models.SourceRow).where(models.SourceRow.url_hash == hash_)
        ).first()
        return Source.model_validate(row.data) if row is not None else None

    def get_by_url(self, url: str) -> Source | None:
        return self.get_by_url_hash(url_hash(url))


class ClaimRepository(ReadRepository[Claim, models.ClaimRow]):
    """Claims carry an owner (a lead or an opportunity) supplied on write, since the
    domain Claim is reused in both places and doesn't know its own owner."""

    _row_type = models.ClaimRow
    _domain_type = Claim

    def add(
        self,
        claim: Claim,
        *,
        opportunity_id: UUID | None = None,
        lead_id: UUID | None = None,
    ) -> Claim:
        row = models.ClaimRow(id=claim.id, opportunity_id=opportunity_id, lead_id=lead_id)
        self._fill(row, claim)
        self._session.add(row)
        self._add_links(claim)
        return claim

    def upsert(
        self,
        claim: Claim,
        *,
        opportunity_id: UUID | None = None,
        lead_id: UUID | None = None,
    ) -> Claim:
        """Insert or update a claim. A claim can belong to both a lead and the opportunity
        built from it, so an owner id that isn't passed is left as it was."""
        row = self._session.get(models.ClaimRow, claim.id)
        if row is None:
            return self.add(claim, opportunity_id=opportunity_id, lead_id=lead_id)
        if opportunity_id is not None:
            row.opportunity_id = opportunity_id
        if lead_id is not None:
            row.lead_id = lead_id
        self._fill(row, claim)
        self._session.execute(
            delete(models.ClaimSourceRow).where(models.ClaimSourceRow.claim_id == claim.id)
        )
        self._add_links(claim)
        return claim

    def prune_opportunity(self, opportunity_id: UUID, keep: Iterable[UUID]) -> None:
        """Detach claims the opportunity no longer holds; delete those nothing else owns."""
        kept = set(keep)
        rows = self._session.scalars(
            select(models.ClaimRow).where(models.ClaimRow.opportunity_id == opportunity_id)
        )
        for row in list(rows):
            if row.id in kept:
                continue
            if row.lead_id is not None:
                row.opportunity_id = None
            else:
                self._session.execute(
                    delete(models.ClaimSourceRow).where(models.ClaimSourceRow.claim_id == row.id)
                )
                self._session.delete(row)

    @staticmethod
    def _fill(row: models.ClaimRow, claim: Claim) -> None:
        row.type = claim.type.value
        row.confidence = claim.confidence.value
        row.value = claim.value
        row.unit = claim.unit
        row.agent = claim.agent
        row.stage = claim.stage
        row.observed_at = claim.observed_at
        row.data = claim.model_dump(mode="json")

    def _add_links(self, claim: Claim) -> None:
        for source_id in dict.fromkeys(claim.source_ids):
            self._session.add(
                models.ClaimSourceRow(id=new_id(), claim_id=claim.id, source_id=source_id)
            )

    def list_for_opportunity(self, opportunity_id: UUID) -> list[Claim]:
        return self._all(
            select(models.ClaimRow).where(models.ClaimRow.opportunity_id == opportunity_id)
        )

    def list_for_lead(self, lead_id: UUID) -> list[Claim]:
        return self._all(select(models.ClaimRow).where(models.ClaimRow.lead_id == lead_id))


class EvaluationRepository(ReadRepository[Evaluation, models.EvaluationRow]):
    _row_type = models.EvaluationRow
    _domain_type = Evaluation

    def add(self, evaluation: Evaluation, *, opportunity_id: UUID) -> Evaluation:
        self._session.add(
            models.EvaluationRow(
                id=evaluation.id,
                opportunity_id=opportunity_id,
                composite=evaluation.composite,
                verdict=evaluation.verdict.value,
                weights_ref=evaluation.weights_ref,
                data=evaluation.model_dump(mode="json"),
            )
        )
        return evaluation

    def get_for_opportunity(self, opportunity_id: UUID) -> Evaluation | None:
        row = self._session.scalars(
            select(models.EvaluationRow).where(
                models.EvaluationRow.opportunity_id == opportunity_id
            )
        ).first()
        return Evaluation.model_validate(row.data) if row is not None else None


class ExperimentRepository(ReadRepository[Experiment, models.ExperimentRow]):
    _row_type = models.ExperimentRow
    _domain_type = Experiment

    def add(self, experiment: Experiment, *, opportunity_id: UUID | None = None) -> Experiment:
        self._session.add(
            models.ExperimentRow(
                id=experiment.id,
                opportunity_id=opportunity_id,
                method=experiment.method.value,
                priority=experiment.priority,
                assumption_claim_id=experiment.assumption_claim_id,
                data=experiment.model_dump(mode="json"),
            )
        )
        return experiment

    def list_for_opportunity(self, opportunity_id: UUID) -> list[Experiment]:
        return self._all(
            select(models.ExperimentRow)
            .where(models.ExperimentRow.opportunity_id == opportunity_id)
            .order_by(models.ExperimentRow.priority)
        )


class ReportRepository(DocRepository[Report, models.ReportRow]):
    _row_type = models.ReportRow
    _domain_type = Report

    def _row(self, obj: Report) -> models.ReportRow:
        return models.ReportRow(
            id=obj.id,
            run_id=obj.run_id,
            opportunity_id=obj.opportunity_id,
            incomplete=obj.incomplete,
            created_at=obj.created_at,
            data=obj.model_dump(mode="json"),
        )

    def list_for_run(self, run_id: UUID) -> list[Report]:
        return self._all(select(models.ReportRow).where(models.ReportRow.run_id == run_id))


class KillRepository(DocRepository[Kill, models.KillRow]):
    _row_type = models.KillRow
    _domain_type = Kill

    def _row(self, obj: Kill) -> models.KillRow:
        return models.KillRow(
            id=obj.id,
            subject_kind=obj.subject_kind.value,
            subject_id=obj.subject_id,
            stage=obj.stage,
            failed_gate=obj.failed_gate.value if obj.failed_gate else None,
            killed_at=obj.killed_at,
            data=obj.model_dump(mode="json"),
        )

    def list_for_subject(self, subject_id: UUID) -> list[Kill]:
        return self._all(select(models.KillRow).where(models.KillRow.subject_id == subject_id))


class TraceRepository(DocRepository[Trace, models.TraceRow]):
    _row_type = models.TraceRow
    _domain_type = Trace

    def _row(self, obj: Trace) -> models.TraceRow:
        return models.TraceRow(
            id=obj.id,
            run_id=obj.run_id,
            stage=obj.stage.value if obj.stage else None,
            agent=obj.agent,
            kind=obj.kind.value,
            model=obj.model,
            tokens_in=obj.tokens_in,
            tokens_out=obj.tokens_out,
            cost_usd=obj.cost_usd,
            latency_ms=obj.latency_ms,
            ok=obj.ok,
            created_at=obj.created_at,
            data=obj.model_dump(mode="json"),
        )

    def list_for_run(self, run_id: UUID) -> list[Trace]:
        return self._all(select(models.TraceRow).where(models.TraceRow.run_id == run_id))

    def total_cost_for_run(self, run_id: UUID) -> float:
        total = self._session.scalar(
            select(func.coalesce(func.sum(models.TraceRow.cost_usd), 0.0)).where(
                models.TraceRow.run_id == run_id
            )
        )
        return float(total or 0.0)


@dataclass(frozen=True)
class DedupEntry:
    dedup_hash: str
    status: DedupStatus
    opportunity_id: UUID | None
    lead_id: UUID | None


class DedupRepository:
    """The dedup index table, keyed by an already-computed hash. The hashes come from
    ``opp.evidence.dedup``; the Evidence Store decides what gets indexed."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, dedup_hash: str) -> DedupEntry | None:
        row = self._session.scalars(
            select(models.DedupIndexRow).where(models.DedupIndexRow.dedup_hash == dedup_hash)
        ).first()
        if row is None:
            return None
        return DedupEntry(
            dedup_hash=row.dedup_hash,
            status=DedupStatus(row.status),
            opportunity_id=row.opportunity_id,
            lead_id=row.lead_id,
        )

    def upsert(
        self,
        dedup_hash: str,
        status: DedupStatus,
        *,
        opportunity_id: UUID | None = None,
        lead_id: UUID | None = None,
    ) -> DedupEntry:
        now = utcnow()
        row = self._session.scalars(
            select(models.DedupIndexRow).where(models.DedupIndexRow.dedup_hash == dedup_hash)
        ).first()
        if row is None:
            row = models.DedupIndexRow(
                id=new_id(),
                dedup_hash=dedup_hash,
                status=status.value,
                opportunity_id=opportunity_id,
                lead_id=lead_id,
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            row.status = status.value
            if opportunity_id is not None:
                row.opportunity_id = opportunity_id
            if lead_id is not None:
                row.lead_id = lead_id
            row.updated_at = now
        return DedupEntry(
            dedup_hash=dedup_hash,
            status=status,
            opportunity_id=opportunity_id if opportunity_id is not None else row.opportunity_id,
            lead_id=lead_id if lead_id is not None else row.lead_id,
        )

    def list_by_status(self, status: DedupStatus) -> Sequence[DedupEntry]:
        rows = self._session.scalars(
            select(models.DedupIndexRow).where(models.DedupIndexRow.status == status.value)
        )
        return [
            DedupEntry(
                dedup_hash=row.dedup_hash,
                status=DedupStatus(row.status),
                opportunity_id=row.opportunity_id,
                lead_id=row.lead_id,
            )
            for row in rows
        ]


def _as_utc(moment: datetime) -> datetime:
    """SQLite hands datetimes back naive; they were written in UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class ResearchCacheRepository:
    """Search responses and fetched pages kept for a freshness window (plan.md §17).

    Stores plain JSON; the research layer owns the models. An expired entry reads as a
    miss and is overwritten by the next ``put``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_search(self, cache_key: str, *, now: datetime) -> dict[str, Any] | None:
        return self._fresh(self._search_row(cache_key), now)

    def put_search(
        self,
        cache_key: str,
        data: dict[str, Any],
        *,
        provider: str,
        query: str,
        now: datetime,
        ttl: timedelta,
    ) -> None:
        row = self._search_row(cache_key)
        if row is None:
            row = models.SearchCacheRow(id=new_id(), cache_key=cache_key)
            self._session.add(row)
        row.provider, row.query = provider, query
        row.data, row.created_at, row.expires_at = data, now, now + ttl

    def get_page(self, url_hash_: str, *, now: datetime) -> dict[str, Any] | None:
        return self._fresh(self._page_row(url_hash_), now)

    def put_page(
        self, url_hash_: str, data: dict[str, Any], *, url: str, now: datetime, ttl: timedelta
    ) -> None:
        row = self._page_row(url_hash_)
        if row is None:
            row = models.PageCacheRow(id=new_id(), url_hash=url_hash_)
            self._session.add(row)
        row.url = url
        row.data, row.created_at, row.expires_at = data, now, now + ttl

    def _search_row(self, cache_key: str) -> models.SearchCacheRow | None:
        return self._session.scalars(
            select(models.SearchCacheRow).where(models.SearchCacheRow.cache_key == cache_key)
        ).first()

    def _page_row(self, url_hash_: str) -> models.PageCacheRow | None:
        return self._session.scalars(
            select(models.PageCacheRow).where(models.PageCacheRow.url_hash == url_hash_)
        ).first()

    @staticmethod
    def _fresh(
        row: models.SearchCacheRow | models.PageCacheRow | None, now: datetime
    ) -> dict[str, Any] | None:
        if row is None or _as_utc(row.expires_at) <= now:
            return None
        return row.data
