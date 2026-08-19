from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the Web API."""

    app_name: str = "ODPlatform Quality Inspection API"
    environment: str = "local"

    model_config = SettingsConfigDict(env_prefix="ODP_", case_sensitive=False)
