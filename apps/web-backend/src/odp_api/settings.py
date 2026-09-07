from typing import ClassVar, Literal

from pydantic import BaseModel, model_validator
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
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str | None = None
    minio_secure: bool = True
    minio_public_endpoint: str | None = None
    minio_public_secure: bool = True
    minio_region: str = "us-east-1"
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


class ProcessReadiness(BaseModel):
    """Concrete readiness result for an independently deployed process."""

    ready: bool
    reasons: tuple[str, ...] = ()


class ProcessSettings(BaseSettings):
    """Fail-closed shared settings for P1 process containers."""

    environment: Environment = "local"
    database_url: str | None = None
    redis_url: str | None = None
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str | None = None
    model_path: str | None = None
    model_sha256: str | None = None
    execution_provider: str = "CPUExecutionProvider"
    lease_seconds: int = 20
    recovery_loop_seconds: int = 2
    scheduling_margin_seconds: int = 5
    requires_model: ClassVar[bool] = False

    model_config = SettingsConfigDict(env_prefix="ODP_", case_sensitive=False)

    @model_validator(mode="after")
    def validate_runtime(self) -> "ProcessSettings":
        if self.lease_seconds <= 0 or self.recovery_loop_seconds <= 0 or self.scheduling_margin_seconds < 0:
            raise ValueError("process timing settings must be positive")
        if self.lease_seconds + self.recovery_loop_seconds + self.scheduling_margin_seconds > 30:
            raise ValueError("lease and recovery budget must fit inside thirty seconds")
        if self.environment not in {"local", "test"}:
            required = {
                "database_url": self.database_url,
                "redis_url": self.redis_url,
                "minio_endpoint": self.minio_endpoint,
                "minio_access_key": self.minio_access_key,
                "minio_secret_key": self.minio_secret_key,
                "minio_bucket": self.minio_bucket,
            }
            if self.requires_model:
                required.update(
                    model_path=self.model_path,
                    model_sha256=self.model_sha256,
                )
            missing = [name for name, value in required.items() if not value or not value.strip()]
            if missing:
                raise ValueError(f"docker process settings missing: {', '.join(missing)}")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("deployed processes require PostgreSQL")
            if not self.redis_url.startswith("redis://"):
                raise ValueError("deployed processes require Redis")
            if self.execution_provider != "CPUExecutionProvider":
                raise ValueError("only CPUExecutionProvider is permitted by the P1 gate")
        return self

    @classmethod
    def valid_test_instance(cls) -> "ProcessSettings":
        return cls(
            environment="test",
            database_url="postgresql+psycopg://test:test@localhost/odp",
            redis_url="redis://localhost:6379/0",
            minio_endpoint="localhost:9000",
            minio_access_key="test-access",
            minio_secret_key="test-secret",
            minio_bucket="test-artifacts",
            model_path="tests/fixtures/models/tiny-detector.onnx",
            model_sha256="a" * 64,
        )

    def readiness(
        self,
        *,
        database_ok: bool,
        redis_ok: bool,
        minio_ok: bool,
        model_loaded: bool,
        model_sha_verified: bool,
        provider_ok: bool,
    ) -> ProcessReadiness:
        checks = {
            "database": database_ok,
            "redis": redis_ok,
            "minio": minio_ok,
            "execution_provider": provider_ok,
        }
        if self.requires_model:
            checks["model_loaded"] = model_loaded
            checks["model_sha256"] = model_sha_verified
        reasons = tuple(name for name, passed in checks.items() if not passed)
        return ProcessReadiness(ready=not reasons, reasons=reasons)


class WorkerSettings(ProcessSettings):
    requires_model: ClassVar[bool] = True


class IngestorSettings(ProcessSettings):
    requires_model: ClassVar[bool] = True


class RelaySettings(ProcessSettings):
    pass


class SchedulerSettings(ProcessSettings):
    pass


class ArtifactReconcilerSettings(ProcessSettings):
    pass


class StreamRetentionSettings(ProcessSettings):
    pass
