from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from sqlalchemy import event, func, select

from odp_api.adapters.persistence.models import (
    AuditChainHeadRow,
    AuditLogRow,
    Base,
    CaseTransitionRow,
)
from odp_api.adapters.persistence.repositories import (
    SqlAlchemyAuditRepository,
    SqlAlchemyCaseRepository,
)
from odp_api.adapters.persistence.unit_of_work import SqlAlchemyBusinessUnitOfWork
from odp_api.db import create_engine_and_session
from odp_api.modules.audit.service import AuditAppendBlocked, AuditService, AuditWriteError
from odp_api.modules.cases.application import AuditContext, CaseApplicationService
from odp_api.modules.cases.errors import InvalidCaseTransition
from odp_api.modules.identity.models import Actor
from odp_api.seed import DEMO_ORG_ID, build_demo_seed, seed_business_data


class FailingAuditUnitOfWork:
    """Test-only failure double; production has no failure switch."""

    def __init__(self, session_factory) -> None:
        self._inner = SqlAlchemyBusinessUnitOfWork(session_factory)

    def __enter__(self):
        unit_of_work = self._inner.__enter__()
        unit_of_work.audits = _FailingAuditRepository(unit_of_work.audits)
        return unit_of_work

    def __exit__(self, exc_type, exc_value, traceback):
        return self._inner.__exit__(exc_type, exc_value, traceback)


class _FailingAuditRepository:
    def __init__(self, inner) -> None:
        self._inner = inner

    def append_under_head_lock(self, command, make_entry):
        self._inner.append_under_head_lock(command, make_entry)
        self._inner._session.flush()
        raise AuditWriteError("injected audit persistence failure")


class PausingUnitOfWork:
    """Test-only interleaving point after the real SQLite write lock is acquired."""

    def __init__(self, session_factory, entered: Event, release: Event) -> None:
        self._inner = SqlAlchemyBusinessUnitOfWork(session_factory)
        self._entered = entered
        self._release = release

    def __enter__(self):
        unit_of_work = self._inner.__enter__()
        unit_of_work.cases = _PausingCaseRepository(
            unit_of_work.cases, self._entered, self._release
        )
        return unit_of_work

    def __exit__(self, exc_type, exc_value, traceback):
        return self._inner.__exit__(exc_type, exc_value, traceback)


class _PausingCaseRepository:
    def __init__(self, inner, entered: Event, release: Event) -> None:
        self._inner = inner
        self._entered = entered
        self._release = release

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def save_transition(self, *args, **kwargs):
        self._entered.set()
        if not self._release.wait(timeout=5):
            raise TimeoutError("Test transition was not released.")
        return self._inner.save_transition(*args, **kwargs)


def _audit_context(occurred_at: datetime | None = None) -> AuditContext:
    return AuditContext(
        occurred_at=occurred_at or datetime(2026, 8, 24, 9, 30, tzinfo=UTC),
        correlation_id=UUID("40000000-0000-4000-8000-000000000001"),
        request_ip="203.0.113.20",
    )


def _seeded_sessions(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine)
    seed_business_data(sessions, build_demo_seed(), PasswordHasher().hash)
    return engine, sessions


def test_audit_write_failure_rolls_back_case_status_and_transition_history(tmp_path) -> None:
    """Removing transaction rollback would leave status/history changed after an audit failure."""
    engine, sessions = _seeded_sessions(tmp_path)
    try:
        seed = build_demo_seed()
        case = seed.cases[0]
        actor: Actor = seed.actors[0]
        service = CaseApplicationService(lambda: FailingAuditUnitOfWork(sessions))

        with pytest.raises(AuditWriteError):
            service.transition(case.case_id, "IN_REVIEW", actor, _audit_context())

        cases = SqlAlchemyCaseRepository(sessions)
        stored = cases.get(case.case_id, DEMO_ORG_ID)
        assert stored is not None
        assert stored.case.status == "PENDING_CONFIRMATION"
        assert cases.history(case.case_id, DEMO_ORG_ID) == []
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(CaseTransitionRow)) == 0
            assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 0
            assert session.scalar(select(func.count()).select_from(AuditChainHeadRow)) == 0
    finally:
        engine.dispose()


def test_non_utc_context_round_trips_as_canonical_utc_and_keeps_chain_valid(tmp_path) -> None:
    """Storing a +08 timestamp without normalization changes its audit hash after SQLite restart."""
    database_url = f"sqlite:///{tmp_path / 'utc-round-trip.db'}"
    engine, sessions = create_engine_and_session(database_url)
    Base.metadata.create_all(engine)
    seed_business_data(sessions, build_demo_seed(), PasswordHasher().hash)
    seed = build_demo_seed()
    expected = datetime(2026, 8, 24, 1, 30, tzinfo=UTC)
    context = _audit_context(
        datetime(2026, 8, 24, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    )
    service = CaseApplicationService(
        lambda: SqlAlchemyBusinessUnitOfWork(sessions),
        AuditService(SqlAlchemyAuditRepository(sessions)),
    )
    try:
        stored = service.transition(seed.cases[0].case_id, "IN_REVIEW", seed.actors[0], context)
        assert stored is not None
        assert stored.updated_at == expected
        assert stored.history[-1].occurred_at == expected
    finally:
        engine.dispose()

    restarted_engine, restarted_sessions = create_engine_and_session(database_url)
    try:
        restarted_cases = SqlAlchemyCaseRepository(restarted_sessions)
        restarted_audit = AuditService(SqlAlchemyAuditRepository(restarted_sessions))
        restored = restarted_cases.get(seed.cases[0].case_id, DEMO_ORG_ID)
        assert restored is not None
        assert restored.history[-1].occurred_at == expected
        audit_entry = restarted_audit.repository.read_consistent_chain(DEMO_ORG_ID).entries[-1]
        assert audit_entry.occurred_at == expected
        assert restarted_audit.verify_organization_chain(DEMO_ORG_ID).is_valid
    finally:
        restarted_engine.dispose()


def test_append_guard_prevents_a_blocked_organization_from_bypassing_a_running_transition(tmp_path) -> None:
    """Releasing the health lock after the check lets a later block race past the commit."""
    engine, sessions = _seeded_sessions(tmp_path)
    try:
        seed = build_demo_seed()
        entered = Event()
        release = Event()
        blocked = Event()
        audit_service = AuditService(SqlAlchemyAuditRepository(sessions))
        first = CaseApplicationService(
            lambda: PausingUnitOfWork(sessions, entered, release), audit_service
        )
        second = CaseApplicationService(
            lambda: SqlAlchemyBusinessUnitOfWork(sessions), audit_service
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            in_flight = executor.submit(
                first.transition,
                seed.cases[0].case_id,
                "IN_REVIEW",
                seed.actors[0],
                _audit_context(),
            )
            assert entered.wait(timeout=1)

            def block_organization() -> None:
                audit_service.block_appends(DEMO_ORG_ID)
                blocked.set()

            block = executor.submit(block_organization)
            assert not blocked.wait(timeout=0.1)
            release.set()
            assert in_flight.result(timeout=2) is not None
            block.result(timeout=2)

        with pytest.raises(AuditAppendBlocked):
            second.transition(
                seed.cases[0].case_id,
                "RESOLVED",
                seed.actors[0],
                _audit_context(),
            )
        restored = SqlAlchemyCaseRepository(sessions).get(seed.cases[0].case_id, DEMO_ORG_ID)
        assert restored is not None
        assert restored.case.status == "IN_REVIEW"
        assert len(audit_service.repository.read_consistent_chain(DEMO_ORG_ID).entries) == 1
    finally:
        engine.dispose()


def test_sqlite_begin_immediate_serializes_two_case_writers(tmp_path) -> None:
    """Without BEGIN IMMEDIATE both writers can derive a transition from the same status."""
    engine, sessions = _seeded_sessions(tmp_path)
    try:
        seed = build_demo_seed()
        entered = Event()
        release = Event()
        first = CaseApplicationService(lambda: PausingUnitOfWork(sessions, entered, release))
        second = CaseApplicationService(lambda: SqlAlchemyBusinessUnitOfWork(sessions))
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_writer = executor.submit(
                first.transition,
                seed.cases[0].case_id,
                "IN_REVIEW",
                seed.actors[0],
                _audit_context(),
            )
            assert entered.wait(timeout=1)
            second_writer = executor.submit(
                second.transition,
                seed.cases[0].case_id,
                "IN_REVIEW",
                seed.actors[0],
                _audit_context(),
            )
            assert not second_writer.done()
            release.set()
            assert first_writer.result(timeout=2) is not None
            with pytest.raises(InvalidCaseTransition):
                second_writer.result(timeout=2)
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)
def test_postgresql_case_load_executes_a_row_lock_contract() -> None:
    """CI-only contract: the durable writer must issue SELECT ... FOR UPDATE on PostgreSQL."""
    database_url = os.environ["ODP_POSTGRES_TEST_URL"]
    engine, sessions = create_engine_and_session(database_url)
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        Base.metadata.create_all(engine)
        seed_business_data(sessions, build_demo_seed(), PasswordHasher().hash)
        with SqlAlchemyBusinessUnitOfWork(sessions) as unit_of_work:
            assert unit_of_work.cases.get(build_demo_seed().cases[0].case_id, DEMO_ORG_ID)
        assert any("FOR UPDATE" in statement.upper() for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
        engine.dispose()
