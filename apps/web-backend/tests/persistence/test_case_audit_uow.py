from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher

from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.repositories import SqlAlchemyCaseRepository
from odp_api.adapters.persistence.unit_of_work import SqlAlchemyBusinessUnitOfWork
from odp_api.db import create_engine_and_session
from odp_api.modules.audit.service import AuditWriteError
from odp_api.modules.cases.application import AuditContext, CaseApplicationService
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
        raise AuditWriteError("injected audit persistence failure")


def _audit_context() -> AuditContext:
    return AuditContext(
        occurred_at=datetime(2026, 8, 24, 9, 30, tzinfo=UTC),
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
    finally:
        engine.dispose()
