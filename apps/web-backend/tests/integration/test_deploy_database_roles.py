from pathlib import Path

ROOT = Path(__file__).parents[4]


def test_compose_uses_distinct_migrator_and_runtime_database_roles() -> None:
    compose = (ROOT / "deploy" / "compose.yaml").read_text()

    assert "POSTGRES_USER: odp_migrator" in compose
    assert "ODP_MIGRATOR_DATABASE_URL" in compose
    assert "ODP_RUNTIME_DATABASE_URL" in compose
    assert "python -m odp_api.database_roles" in compose
    assert "docker-entrypoint-initdb.d/001-create-app-role.sql" in compose
