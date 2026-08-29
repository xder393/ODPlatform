import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from odp_api.adapters.persistence.repositories import SqlAlchemyAuditRepository
from odp_api.db import create_engine_and_session
from odp_api.modules.audit.models import AuditCommand
from odp_api.modules.audit.service import AuditService


def test_runtime_grant_script_preserves_append_only_audit_boundaries() -> None:
    from odp_api.database_roles import grant_sql

    sql = grant_sql()

    assert "GRANT SELECT, INSERT ON TABLE public.audit_logs TO odp_app" in sql
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.audit_logs FROM odp_app" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE public.audit_chain_heads TO odp_app" in sql
    assert "REVOKE DELETE, TRUNCATE ON TABLE public.audit_chain_heads FROM odp_app" in sql
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO odp_app" in sql
    for table_name in ("knowledge_documents", "knowledge_parent_chunks", "knowledge_chunk_index"):
        assert table_name in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO odp_app" in sql


def test_bootstrap_role_script_repairs_the_runtime_login_for_existing_volumes() -> None:
    from odp_api.database_roles import bootstrap_sql

    sql = bootstrap_sql()

    assert "CREATE ROLE odp_app" in sql
    assert "ALTER ROLE odp_app LOGIN PASSWORD 'odp_app_dev'" in sql


@pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_APP_TEST_URL"),
    reason="requires isolated runtime PostgreSQL role configured by CI",
)
def test_postgresql_runtime_role_is_not_owner_and_cannot_mutate_audit_rows() -> None:
    """CI contract: the app role can append but PostgreSQL rejects audit rewrites."""
    app_engine, sessions = create_engine_and_session(os.environ["ODP_POSTGRES_APP_TEST_URL"])
    organization_id = uuid4()
    try:
        with app_engine.connect() as connection:
            runtime_user = connection.scalar(text("SELECT current_user"))
            owner = connection.scalar(
                text("SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = 'audit_logs'")
            )
            assert runtime_user == "odp_app"
            assert owner == "odp"
        appended = AuditService(SqlAlchemyAuditRepository(sessions)).append(
            AuditCommand(
                organization_id=organization_id,
                resource_type="case",
                resource_id=uuid4(),
                action="created",
                change_summary="append succeeds",
                actor_id=None,
                occurred_at=datetime.now(UTC),
                correlation_id=None,
                request_ip=None,
            )
        )

        with pytest.raises(ProgrammingError), app_engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_logs SET action = 'rewritten' WHERE audit_id = :audit_id"),
                {"audit_id": appended.audit_id},
            )
        with pytest.raises(ProgrammingError), app_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM audit_logs WHERE audit_id = :audit_id"),
                {"audit_id": appended.audit_id},
            )
    finally:
        app_engine.dispose()
