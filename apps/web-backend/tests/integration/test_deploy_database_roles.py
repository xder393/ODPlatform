from pathlib import Path

ROOT = Path(__file__).parents[4]


def test_compose_isolates_owner_credentials_in_one_shot_migrator() -> None:
    compose = (ROOT / "deploy" / "compose.yaml").read_text()

    migrate_section = compose.split("\n  migrate:\n", 1)[1].split("\n  api:\n", 1)[0]
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  web:\n", 1)[0]
    assert "POSTGRES_USER: odp" in compose
    assert "POSTGRES_PASSWORD: odp" in compose
    assert "ODP_MIGRATOR_DATABASE_URL" not in api_section
    assert "postgresql+psycopg://odp:" not in api_section
    assert "postgresql://odp:odp@" not in api_section
    assert "ODP_DATABASE_URL: postgresql+psycopg://odp_api:" in api_section
    assert "ODP_POSTGRES_URL: postgresql://odp_api:" in api_section
    assert "ODP_MIGRATOR_DATABASE_URL" in migrate_section
    assert "python -m odp_api.migrations" in migrate_section
    assert "alembic upgrade head" in migrate_section
    assert "python -m odp_api.database_roles" in migrate_section
    assert "python -m odp_api.seed_knowledge" in migrate_section
    assert "service_completed_successfully" in api_section
