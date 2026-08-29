import pytest
from pydantic import ValidationError

from odp_api.settings import Settings


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="prod")


@pytest.mark.parametrize("environment", ["docker", "staging", "production"])
def test_external_environments_require_postgresql_redis_and_pgvector(
    environment: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(environment=environment)


def test_docker_environment_accepts_only_explicit_runtime_dependencies() -> None:
    settings = Settings(
        environment="docker",
        database_url="postgresql+psycopg://odp_app:secret@postgres:5432/odp",
        postgres_url="postgresql://odp_app:secret@postgres:5432/odp",
        redis_url="redis://redis:6379/0",
        retrieval_backend="pgvector",
    )

    assert settings.environment == "docker"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_privileged_environments_reject_startup_seeds_and_dev_triggers(
    environment: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment=environment,
            database_url="postgresql+psycopg://odp_app:secret@postgres:5432/odp",
            postgres_url="postgresql://odp_app:secret@postgres:5432/odp",
            redis_url="redis://redis:6379/0",
            retrieval_backend="pgvector",
            enable_dev_event_trigger=True,
        )


@pytest.mark.parametrize(
    "setting", [{"seed_demo": True}, {"seed_knowledge_on_startup": True}]
)
def test_privileged_environments_reject_each_startup_seed(
    setting: dict[str, bool],
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+psycopg://odp_app:secret@postgres:5432/odp",
            postgres_url="postgresql://odp_app:secret@postgres:5432/odp",
            redis_url="redis://redis:6379/0",
            retrieval_backend="pgvector",
            **setting,
        )
