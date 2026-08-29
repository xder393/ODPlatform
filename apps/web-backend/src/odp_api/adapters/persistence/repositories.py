"""SQLAlchemy implementations of identity, case, and audit persistence ports."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.models import (
    ActorLineGrantRow,
    ActorRow,
    AuditChainHeadRow,
    AuditLogRow,
    CaseTransitionRow,
    DefectCaseRow,
    InspectionEventRow,
    PasswordCredentialRow,
)
from odp_api.modules.audit.models import (
    AuditChainHead,
    AuditChainSnapshot,
    AuditCommand,
    AuditLog,
)
from odp_api.modules.cases.ports import CaseTransition, StoredCase
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.inspection.models import DefectCase, InspectionEvent


class SqlAlchemyActorRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, actor_id: UUID) -> Actor | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ActorRow).where(
                    ActorRow.actor_id == actor_id, ActorRow.enabled.is_(True)
                )
            )
            return _to_actor(session, row) if row is not None else None

    def get_by_email(self, email: str) -> Actor | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ActorRow).where(
                    ActorRow.email == email, ActorRow.enabled.is_(True)
                )
            )
            return _to_actor(session, row) if row is not None else None


class SqlAlchemyPasswordCredentialRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def password_hash(self, actor_id: UUID) -> str | None:
        with self._session_factory() as session:
            row = session.get(PasswordCredentialRow, actor_id)
            return row.password_hash if row is not None else None


def _to_actor(session: Session, row: ActorRow) -> Actor:
    line_ids = session.scalars(
        select(ActorLineGrantRow.line_id).where(
            ActorLineGrantRow.actor_id == row.actor_id
        )
    )
    return Actor(
        actor_id=row.actor_id,
        organization_id=row.organization_id,
        role=Role(row.role),
        line_ids=frozenset(line_ids),
        email=row.email,
    )


class SqlAlchemyCaseRepository:
    """Tenant-scoped durable case reader used outside a business transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(
        self, organization_id: UUID, updated_after: datetime | None
    ) -> list[StoredCase]:
        with self._session_factory() as session:
            statement = select(DefectCaseRow).where(
                DefectCaseRow.organization_id == organization_id
            )
            if updated_after is not None:
                statement = statement.where(DefectCaseRow.updated_at > updated_after)
            rows = session.scalars(
                statement.order_by(DefectCaseRow.updated_at.desc())
            ).all()
            return [
                stored
                for row in rows
                if (stored := _stored_case_or_none(session, row)) is not None
            ]

    def get(self, case_id: UUID, organization_id: UUID) -> StoredCase | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(DefectCaseRow).where(
                    DefectCaseRow.case_id == case_id,
                    DefectCaseRow.organization_id == organization_id,
                )
            )
            return _stored_case_or_none(session, row) if row is not None else None

    def history(self, case_id: UUID, organization_id: UUID) -> list[CaseTransition]:
        with self._session_factory() as session:
            return _history(session, case_id, organization_id)


class SqlAlchemyAuditRepository:
    """Audit repository that opens an isolated transaction for standalone appends."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def append_under_head_lock(self, command, make_entry) -> AuditLog:
        with self._session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                entry = SqlAlchemyAuditSessionRepository(
                    session
                ).append_under_head_lock(command, make_entry)
                session.commit()
                return entry
            except BaseException:
                session.rollback()
                raise

    def read_consistent_chain(self, organization_id: UUID) -> AuditChainSnapshot:
        with self._session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                return SqlAlchemyAuditSessionRepository(session).read_consistent_chain(
                    organization_id
                )
            finally:
                session.rollback()

    def organization_ids(self) -> tuple[UUID, ...]:
        with self._session_factory() as session:
            return tuple(session.scalars(select(AuditChainHeadRow.organization_id)))


class SqlAlchemyCaseSessionRepository:
    """Case adapter bound to the session owned by ``SqlAlchemyBusinessUnitOfWork``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list(
        self, organization_id: UUID, updated_after: datetime | None
    ) -> list[StoredCase]:
        statement = select(DefectCaseRow).where(
            DefectCaseRow.organization_id == organization_id
        )
        if updated_after is not None:
            statement = statement.where(DefectCaseRow.updated_at > updated_after)
        rows = self._session.scalars(
            statement.order_by(DefectCaseRow.updated_at.desc())
        ).all()
        return [
            stored
            for row in rows
            if (stored := _stored_case_or_none(self._session, row)) is not None
        ]

    def get(self, case_id: UUID, organization_id: UUID) -> StoredCase | None:
        row = self._session.scalar(
            select(DefectCaseRow)
            .where(
                DefectCaseRow.case_id == case_id,
                DefectCaseRow.organization_id == organization_id,
            )
            .with_for_update()
        )
        return _stored_case_or_none(self._session, row) if row is not None else None

    def history(self, case_id: UUID, organization_id: UUID) -> list[CaseTransition]:
        return _history(self._session, case_id, organization_id)

    def save_transition(
        self,
        before: DefectCase,
        after: DefectCase,
        actor_id: UUID,
        occurred_at: datetime,
        correlation_id: UUID | None,
    ) -> StoredCase:
        row = self._session.scalar(
            select(DefectCaseRow)
            .where(
                DefectCaseRow.case_id == before.case_id,
                DefectCaseRow.organization_id == before.organization_id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(before.case_id)
        row.status = after.status
        row.assignee_id = after.assignee_id
        row.last_transition_actor_id = after.last_transition_actor_id
        row.updated_at = occurred_at
        transition = CaseTransitionRow(
            transition_id=uuid4(),
            case_id=after.case_id,
            organization_id=after.organization_id,
            from_status=before.status,
            to_status=after.status,
            actor_id=actor_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
        )
        self._session.add(transition)
        return StoredCase(
            case=after,
            updated_at=occurred_at,
            history=_history(self._session, after.case_id, after.organization_id),
        )


class SqlAlchemyAuditSessionRepository:
    """Audit operations sharing the caller-owned database transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_under_head_lock(self, command: AuditCommand, make_entry) -> AuditLog:
        head = self._locked_head(command.organization_id)
        entry = make_entry(head.last_sequence + 1, head.head_hash)
        self._session.add(
            AuditLogRow(
                audit_id=entry.audit_id,
                organization_id=entry.organization_id,
                sequence=entry.sequence,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                action=entry.action,
                change_summary=entry.change_summary,
                actor_id=entry.actor_id,
                occurred_at=entry.occurred_at,
                correlation_id=entry.correlation_id,
                request_ip=entry.request_ip,
                previous_hash=entry.previous_hash,
                entry_hash=entry.entry_hash,
            )
        )
        head.last_sequence = entry.sequence
        head.head_hash = entry.entry_hash
        return entry

    def read_consistent_chain(self, organization_id: UUID) -> AuditChainSnapshot:
        head = self._session.scalar(
            select(AuditChainHeadRow)
            .where(AuditChainHeadRow.organization_id == organization_id)
            .with_for_update()
        )
        entries = self._session.scalars(
            select(AuditLogRow)
            .where(AuditLogRow.organization_id == organization_id)
            .order_by(AuditLogRow.sequence)
        ).all()
        return AuditChainSnapshot(
            entries=tuple(_to_audit_log(row) for row in entries),
            head=AuditChainHead(
                organization_id=organization_id,
                last_sequence=head.last_sequence if head is not None else 0,
                head_hash=head.head_hash if head is not None else "0" * 64,
            ),
        )

    def organization_ids(self) -> tuple[UUID, ...]:
        return tuple(self._session.scalars(select(AuditChainHeadRow.organization_id)))

    def _locked_head(self, organization_id: UUID) -> AuditChainHeadRow:
        dialect = self._session.get_bind().dialect.name
        if dialect == "postgresql":
            self._session.execute(
                postgresql_insert(AuditChainHeadRow)
                .values(
                    organization_id=organization_id, last_sequence=0, head_hash="0" * 64
                )
                .on_conflict_do_nothing(index_elements=["organization_id"])
            )
            head = self._session.scalar(
                select(AuditChainHeadRow)
                .where(AuditChainHeadRow.organization_id == organization_id)
                .with_for_update()
            )
        else:
            head = self._session.get(AuditChainHeadRow, organization_id)
            if head is None:
                head = AuditChainHeadRow(
                    organization_id=organization_id, last_sequence=0, head_hash="0" * 64
                )
                self._session.add(head)
                self._session.flush()
        assert head is not None
        return head


def _stored_case(session: Session, row: DefectCaseRow) -> StoredCase:
    events = tuple(
        InspectionEvent(
            event_id=event.event_id,
            organization_id=event.organization_id,
            camera_id=event.camera_id,
            occurred_at=_aware(event.occurred_at),
            defect_class=event.defect_class,
            confidence=event.confidence,
            model_release=event.model_release,
            preprocessing_parameters=tuple(
                (str(key), str(value)) for key, value in event.preprocessing_parameters
            ),
            threshold=event.threshold,
            input_frame_sha256=event.input_frame_sha256,
            line_id=event.line_id,
        )
        for event in session.scalars(
            select(InspectionEventRow)
            .where(
                InspectionEventRow.case_id == row.case_id,
                InspectionEventRow.organization_id == row.organization_id,
            )
            .order_by(InspectionEventRow.occurred_at)
        )
    )
    return StoredCase(
        case=DefectCase(
            case_id=row.case_id,
            organization_id=row.organization_id,
            inspection_events=events,
            status=row.status,
            assignee_id=row.assignee_id,
            last_transition_actor_id=row.last_transition_actor_id,
            line_id=row.line_id,
            product_category=row.product_category,
        ),
        updated_at=_aware(row.updated_at),
        history=_history(session, row.case_id, row.organization_id),
    )


def _stored_case_or_none(session: Session, row: DefectCaseRow) -> StoredCase | None:
    """Ignore only legacy-invalid rows that predate durable case/event seeding."""
    events_exist = session.scalar(
        select(InspectionEventRow.event_id)
        .where(
            InspectionEventRow.case_id == row.case_id,
            InspectionEventRow.organization_id == row.organization_id,
        )
        .limit(1)
    )
    return _stored_case(session, row) if events_exist is not None else None


def _history(
    session: Session, case_id: UUID, organization_id: UUID
) -> list[CaseTransition]:
    return [
        CaseTransition(
            from_status=row.from_status,
            to_status=row.to_status,
            actor_id=row.actor_id,
            occurred_at=_aware(row.occurred_at),
            correlation_id=row.correlation_id,
        )
        for row in session.scalars(
            select(CaseTransitionRow)
            .where(
                CaseTransitionRow.case_id == case_id,
                CaseTransitionRow.organization_id == organization_id,
            )
            .order_by(CaseTransitionRow.occurred_at, CaseTransitionRow.transition_id)
        )
    ]


def _to_audit_log(row: AuditLogRow) -> AuditLog:
    return AuditLog(
        audit_id=row.audit_id,
        organization_id=row.organization_id,
        sequence=row.sequence,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        action=row.action,
        change_summary=row.change_summary,
        actor_id=row.actor_id,
        occurred_at=_aware(row.occurred_at),
        correlation_id=row.correlation_id,
        request_ip=row.request_ip,
        previous_hash=row.previous_hash,
        entry_hash=row.entry_hash,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
