from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from fastapi import APIRouter, FastAPI
from sqlalchemy import inspect
from odp_api.adapters.auth.jwt import ActorRepository, JwtAuthenticator
from odp_api.adapters.generation.mock import MockLLMAdapter
from odp_api.adapters.notifications.redis_stream import RedisStreamInspectionAlertFeed
from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.repositories import (
    SqlAlchemyActorRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyCaseRepository,
)
from odp_api.adapters.persistence.unit_of_work import SqlAlchemyBusinessUnitOfWork
from odp_api.adapters.redis_stream import RedisSocketStreamClient, SQLiteStreamClient
from odp_api.adapters.retrieval.embedding import hash_embedding
from odp_api.adapters.retrieval.inmemory import InMemoryKnowledgeIndex
from odp_api.adapters.retrieval.pgvector import PgVectorPostgresAdapter, psycopg_executor
from odp_api.adapters.tasks.redis_stream import RedisStreamTaskAlertPublisher, RedisStreamTaskQueue
from odp_api.adapters.tasks.sqlite import SQLiteTaskRepository
from odp_api.adapters.vision.mock import MockVisionAdapter
from odp_api.modules.ai_orchestration.router import create_advice_router
from odp_api.modules.ai_orchestration.service import AdviceService
from odp_api.modules.audit.service import AuditRepository, AuditService
from odp_api.modules.audit.verify import AuditVerificationMonitor, ManagedDailyAuditVerification
from odp_api.modules.cases.application import CaseApplicationService
from odp_api.modules.cases.router import create_cases_router
from odp_api.modules.identity.service import (
    InMemoryPasswordVerifier,
    InMemoryReauthenticationStore,
    PasswordVerifier,
    ReauthenticationService,
    create_auth_router,
)
from odp_api.modules.inspection.service import InspectionService
from odp_api.modules.knowledge.ingest import KnowledgeIngestionService
from odp_api.modules.notifications.router import create_notifications_router
from odp_api.modules.tasks.service import TaskService
from odp_api.observability.metrics import (
    DEFAULT_REGISTRY,
    MetricRegistry,
    RegistryTaskMetrics,
    register_standard_metrics,
)
from odp_api.observability.router import create_metrics_router
from odp_api.observability.tracing import CorrelationIdMiddleware
from odp_api.ports.retrieval import KnowledgeIndexPort
from odp_api.ports.vision import FrameInput
from odp_api.seed import (
    DEMO_LINE_ID,
    DEMO_ORG_ID,
    PRODUCT_CATEGORY,
    DemoSeed,
    build_demo_seed,
    seed_business_data,
)
from odp_api.settings import Settings
from odp_api.db import create_engine_and_session
from argon2 import PasswordHasher

health_router = APIRouter()


@health_router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _upgrade_runtime_schema(database_url: str) -> None:
    """Keep the Docker-free SQLite runtime on the same Alembic head as Docker."""
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    probe_engine, _ = create_engine_and_session(database_url)
    try:
        inspector = inspect(probe_engine)
        if inspector.has_table("defect_cases") and not inspector.has_table("alembic_version"):
            # Task 1's early local runtime created its complete 0001 metadata
            # directly. Mark only that known baseline before applying 0002.
            command.stamp(config, "0001_p0_business_state")
    finally:
        probe_engine.dispose()
    command.upgrade(config, "head")


def create_app(
    settings: Settings | None = None,
    actor_repository: ActorRepository | None = None,
    audit_repository: AuditRepository | None = None,
    password_verifier: PasswordVerifier | None = None,
    daily_verification_interval_seconds: float = 24 * 60 * 60,
    stream_client: object | None = None,
    metric_registry: MetricRegistry | None = None,
    seed: DemoSeed | None = None,
) -> FastAPI:
    """Create the ODPlatform quality inspection API.

    ``seed`` overrides ``settings.seed_demo``; either way, when a seed is
    active the demo accounts, cases and knowledge documents replace the
    single hard-coded fixture case.
    """
    runtime_settings = settings or Settings()
    active_seed = seed if seed is not None else (
        build_demo_seed() if runtime_settings.seed_demo else None
    )
    registry = metric_registry or DEFAULT_REGISTRY
    # Pre-register the documented metric set so /metrics always exposes every
    # series, including gauges that no code path has written yet.
    register_standard_metrics(registry)
    _upgrade_runtime_schema(runtime_settings.database_url)
    engine, session_factory = create_engine_and_session(runtime_settings.database_url)
    Base.metadata.create_all(engine)
    runtime_stream_client = stream_client or _runtime_stream_client(runtime_settings)
    task_repository = SQLiteTaskRepository(runtime_settings.task_database_path)
    task_service = TaskService(
        task_repository,
        RedisStreamTaskQueue(runtime_stream_client),
        RegistryTaskMetrics(registry),
        RedisStreamTaskAlertPublisher(runtime_stream_client),
    )
    task_service.recover_unpublished()
    inspection_alert_feed = RedisStreamInspectionAlertFeed(runtime_stream_client)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        audit_verification_monitor.startup_sample_verify()
        daily_audit_verification.start()
        try:
            yield
        finally:
            await daily_audit_verification.stop()
            task_repository.close()
            close = getattr(runtime_stream_client, "close", None)
            if close is not None:
                close()
            engine.dispose()

    app = FastAPI(title=runtime_settings.app_name, lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    resolved_actor_repository = actor_repository or SqlAlchemyActorRepository(session_factory)
    resolved_password_verifier = password_verifier or InMemoryPasswordVerifier(
        active_seed.passwords if active_seed else {}
    )
    app.state.jwt_authenticator = JwtAuthenticator(
        runtime_settings.auth_jwt_secret,
        resolved_actor_repository,
    )
    app.include_router(health_router)
    app.include_router(create_metrics_router(registry))
    inspection_service = InspectionService(MockVisionAdapter(), metric_registry=registry)
    reauthentication_service = ReauthenticationService(InMemoryReauthenticationStore())
    if active_seed is None:
        # Default deterministic fixture used by the unseeded test runtime.
        initial_cases = (
            inspection_service.inspect_fixture(
                FrameInput(fixture_name="scratch-frame-001", content=b"scratch-frame-001"),
                organization_id=UUID("00000000-0000-0000-0000-000000000001"),
                camera_id=UUID("00000000-0000-0000-0000-000000000002"),
                event_id=UUID("00000000-0000-0000-0000-000000000003"),
            ),
        )
        persistence_seed = DemoSeed((), {}, initial_cases, (), "runtime-fixture")
    else:
        initial_cases = active_seed.cases
        persistence_seed = active_seed
    seed_business_data(session_factory, persistence_seed, PasswordHasher().hash)
    case_repository = SqlAlchemyCaseRepository(session_factory)
    audit_service = AuditService(audit_repository or SqlAlchemyAuditRepository(session_factory))
    audit_verification_monitor = AuditVerificationMonitor(
        audit_service, metric_registry=registry
    )
    daily_audit_verification = ManagedDailyAuditVerification(
        audit_verification_monitor, daily_verification_interval_seconds
    )
    case_application_service = CaseApplicationService(
        lambda: SqlAlchemyBusinessUnitOfWork(session_factory), audit_service
    )
    knowledge_index = _retrieval_index(runtime_settings)
    if active_seed is not None:
        _ingest_seed_documents(knowledge_index, active_seed)
    advice_service = AdviceService(
        knowledge_index, MockLLMAdapter(), metric_registry=registry
    )
    app.state.audit_service = audit_service
    app.state.audit_verification_monitor = audit_verification_monitor
    app.state.daily_audit_verification = daily_audit_verification
    app.state.task_service = task_service
    app.state.advice_service = advice_service
    app.state.inspection_service = inspection_service
    app.state.case_repository = case_repository

    app.include_router(
        create_cases_router(
            case_repository,
            reauthentication_service=reauthentication_service,
            audit_service=audit_service,
            case_application_service=case_application_service,
            metric_registry=registry,
        )
    )
    app.include_router(create_advice_router(case_repository, advice_service))
    for case in initial_cases:
        for event in case.inspection_events:
            inspection_alert_feed.publish(event.to_alert(), event.line_id)
    app.include_router(
        create_notifications_router(inspection_alert_feed)
    )
    app.include_router(
        create_auth_router(
            reauthentication_service=reauthentication_service,
            password_verifier=resolved_password_verifier,
            actor_repository=resolved_actor_repository,
        )
    )
    return app


def _retrieval_index(settings: Settings) -> KnowledgeIndexPort:
    """Select the knowledge index backend for this runtime configuration."""
    if settings.retrieval_backend == "pgvector":
        # psycopg is imported lazily inside the executor; local tests and the
        # in-memory backend never load the driver.
        return PgVectorPostgresAdapter(
            psycopg_executor(settings.postgres_url),
            embed=hash_embedding,
        )
    return InMemoryKnowledgeIndex()


def _ingest_seed_documents(index: KnowledgeIndexPort, seed: DemoSeed) -> None:
    """Index the seeded knowledge documents into the configured backend."""
    ingestion = KnowledgeIngestionService(index)
    for document in seed.documents:
        ingestion.ingest(
            organization_id=DEMO_ORG_ID,
            source_name=document.source_name,
            filename=document.filename,
            content=document.content,
            evidence_kind=document.evidence_kind,
            applicable_line_id=DEMO_LINE_ID,
            product_category=PRODUCT_CATEGORY,
        )


def _runtime_stream_client(settings: Settings) -> object:
    if settings.environment.lower() in {"production", "docker", "staging"}:
        return RedisSocketStreamClient(settings.redis_url)
    return SQLiteStreamClient(settings.task_database_path)
