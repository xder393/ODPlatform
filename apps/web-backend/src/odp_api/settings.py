from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the Web API."""

    app_name: str = "ODPlatform Quality Inspection API"
    environment: str = "local"
    auth_jwt_secret: str | None = None
    redis_url: str = "redis://redis:6379/0"
    task_database_path: str = "/tmp/odp-tasks.sqlite3"

    model_config = SettingsConfigDict(env_prefix="ODP_", case_sensitive=False)
