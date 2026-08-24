from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the Web API."""

    app_name: str = "ODPlatform Quality Inspection API"
    environment: str = "local"
    auth_jwt_secret: str | None = None
    redis_url: str = "redis://redis:6379/0"
    database_url: str = "sqlite:////tmp/odp-quality-inspection.sqlite3"
    task_database_path: str = "/tmp/odp-tasks.sqlite3"
    # "inmemory" keeps local demos and tests fully offline; "pgvector" uses
    # PostgreSQL with the pgvector extension through a lazily imported driver.
    retrieval_backend: str = "inmemory"
    postgres_url: str = "postgresql://odp:odp@postgres:5432/odp"
    # Load the deterministic demo seed at app creation (used by the Compose
    # stack so the browser demo can log in with the demo accounts).
    seed_demo: bool = False
    # E2E-only event injection is opt-in in every environment.
    enable_dev_event_trigger: bool = False

    model_config = SettingsConfigDict(env_prefix="ODP_", case_sensitive=False)
