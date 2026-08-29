from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "docker", "staging", "production"]
RetrievalBackend = Literal["inmemory", "pgvector"]


class Settings(BaseSettings):
    """Runtime settings for the Web API."""

    app_name: str = "ODPlatform Quality Inspection API"
    environment: Environment = "local"
    auth_jwt_secret: str | None = None
    redis_url: str | None = None
    database_url: str = "sqlite:////tmp/odp-quality-inspection.sqlite3"
    task_database_path: str = "/tmp/odp-tasks.sqlite3"
    # "inmemory" keeps local demos and tests fully offline; "pgvector" uses
    # PostgreSQL with the pgvector extension through a lazily imported driver.
    retrieval_backend: RetrievalBackend = "inmemory"
    postgres_url: str | None = None
    # Load the deterministic demo seed at app creation (used by the Compose
    # stack so the browser demo can log in with the demo accounts).
    seed_demo: bool = False
    seed_knowledge_on_startup: bool | None = None
    # E2E-only event injection is opt-in in every environment.
    enable_dev_event_trigger: bool = False

    model_config = SettingsConfigDict(env_prefix="ODP_", case_sensitive=False)

    @model_validator(mode="after")
    def validate_external_runtime(self) -> "Settings":
        if self.environment in {"local", "test"}:
            return self
        if not self.database_url.startswith("postgresql"):
            raise ValueError("Non-local environments require a PostgreSQL ODP_DATABASE_URL.")
        if not self.redis_url or not self.redis_url.startswith("redis://"):
            raise ValueError("Non-local environments require ODP_REDIS_URL.")
        if self.retrieval_backend != "pgvector":
            raise ValueError("Non-local environments require ODP_RETRIEVAL_BACKEND=pgvector.")
        if not self.postgres_url or not self.postgres_url.startswith("postgresql://"):
            raise ValueError("Non-local environments require a PostgreSQL ODP_POSTGRES_URL.")
        if self.environment in {"staging", "production"} and (
            self.seed_demo
            or self.seed_knowledge_on_startup is True
            or self.enable_dev_event_trigger
        ):
            raise ValueError("Staging and production prohibit startup seeds and dev triggers.")
        return self
