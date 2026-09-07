import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)

from alembic import command
from odp_api.adapters.auth.argon2 import Argon2PasswordVerifier
from odp_api.adapters.auth.jwt import ActorRepository, JwtAuthenticator
from odp_api.adapters.auth.redis_security import (
    RedisReauthenticationStore,
    RedisWebSocketTicketStore,
)
from odp_api.adapters.auth.sqlite_security import (
    SqliteReauthenticationStore,
    SqliteWebSocketTicketStore,
)
from odp_api.adapters.generation.mock import MockLLMAdapter
from odp_api.adapters.notifications.redis_durable_feed import (
    RedisDurableInspectionAlertFeed,
)
from odp_api.adapters.notifications.redis_gateway_feed import (
    RedisGatewayInspectionAlertFeed,
)
from odp_api.adapters.notifications.sqlite_feed import SqliteInspectionAlertFeed
from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.repositories import (
    SqlAlchemyActorRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyCaseRepository,
    SqlAlchemyPasswordCredentialRepository,
)
from odp_api.adapters.persistence.unit_of_work import SqlAlchemyBusinessUnitOfWork
from odp_api.adapters.redis_stream import RedisSocketStreamClient, SQLiteStreamClient
from odp_api.adapters.retrieval.embedding import hash_embedding
from odp_api.adapters.retrieval.inmemory import InMemoryKnowledgeIndex
from odp_api.adapters.retrieval.pgvector import (
    PgVectorPostgresAdapter,
    psycopg_executor,
)
from odp_api.adapters.tasks.redis_stream import (
    RedisStreamTaskAlertPublisher,
    RedisStreamTaskQueue,
)
from odp_api.adapters.vision.mock import MockVisionAdapter
from odp_api.db import create_engine_and_session
from odp_api.modules.ai_orchestration.router import create_advice_router
from odp_api.modules.ai_orchestration.service import AdviceService
from odp_api.modules.audit.service import AuditRepository, AuditService
from odp_api.modules.audit.verify import (
    AuditVerificationMonitor,
    ManagedDailyAuditVerification,
)
from odp_api.modules.cases.application import CaseApplicationService
from odp_api.modules.cases.router import create_cases_router
from odp_api.modules.identity.service import (
    PasswordVerifier,
    ReauthenticationService,
    create_auth_router,
)
from odp_api.modules.identity.tickets import WebSocketTicketService
from odp_api.modules.inspection.service import InspectionService
from odp_api.modules.knowledge.ingest import KnowledgeIngestionService
from odp_api.modules.notifications.dev_router import (
    create_development_notifications_router,
)
from odp_api.modules.notifications.router import create_notifications_router
from odp_api.modules.tasks.service import TaskService
from odp_api.observability.logging import configure_uvicorn_access_logging
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
from odp_api.processes.common import process_id
from odp_api.seed import (
    DEMO_LINE_ID,
    DEMO_ORG_ID,
    PRODUCT_CATEGORY,
    DemoSeed,
    build_demo_seed,
    seed_business_data,
)
from odp_api.settings import Settings

health_router = APIRouter()


@health_router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    try:
        with request.app.state.session_factory() as session:
            session.execute(text("SELECT 1"))
    except (OperationalError, InterfaceError, DisconnectionError, DBAPIError) as error:
        if _is_database_unavailable(error):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database unavailable",
            ) from error
        raise
    return {"status": "ok"}


def _is_database_unavailable(error: BaseException) -> bool:
    """Distinguish connection/driver outages from domain constraint conflicts."""
    return (
        isinstance(error, (OperationalError, InterfaceError, DisconnectionError))
        or (isinstance(error, DBAPIError) and error.connection_invalidated)
    ) and not isinstance(error, IntegrityError)


def _upgrade_runtime_schema(database_url: str) -> None:
    """Keep the Docker-free SQLite runtime on the same Alembic head as Docker."""
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    probe_engine, _ = create_engine_and_session(database_url)
    try:
        inspector = sqlalchemy_inspect(probe_engine)
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
    configure_uvicorn_access_logging()
    active_seed = seed if seed is not None else (
        build_demo_seed() if runtime_settings.seed_demo else None
    )
    registry = metric_registry or DEFAULT_REGISTRY
    # Pre-register the documented metric set so /metrics always exposes every
    # series, including gauges that no code path has written yet.
    register_standard_metrics(registry)
    # All externally deployed environments are migrated and seeded by the
    # one-shot migrator, never by the runtime credential.
    managed_database = runtime_settings.environment in {"local", "test"}
    if managed_database:
        _upgrade_runtime_schema(runtime_settings.database_url)
    engine, session_factory = create_engine_and_session(runtime_settings.database_url)
    if managed_database:
        Base.metadata.create_all(engine)
    runtime_stream_client = stream_client or _runtime_stream_client(runtime_settings)
    # P0's local SQLite task service remains available for local/test demos.
    # Deployed P1 composition owns task authority in PostgreSQL and the
    # independent Worker/Relay processes; the API must not create a hidden
    # /tmp SQLite state file when running under Docker/staging/production.
    task_repository = None
    task_service = None
    if runtime_settings.environment in {"local", "test"}:
        from odp_api.adapters.tasks.sqlite import SQLiteTaskRepository

        task_repository = SQLiteTaskRepository(runtime_settings.task_database_path)
        task_service = TaskService(
            task_repository,
            RedisStreamTaskQueue(runtime_stream_client),
            RegistryTaskMetrics(registry),
            RedisStreamTaskAlertPublisher(runtime_stream_client),
        )
        task_service.recover_unpublished()
    # Facts are always database-backed. Redis is deliberately not used as the
    # source of truth, so a transient stream outage cannot erase reconciliation.
    sqlite_alert_feed = SqliteInspectionAlertFeed(session_factory)
    deployed_runtime = runtime_settings.environment.lower() in {"production", "docker", "staging"}
    if deployed_runtime:
        # The Gateway is read-only: P1 inspection effects append durable facts
        # and Outbox events, then Relay wakes this consumer with EventEnvelope.
        # Keep the direct publisher only behind the explicit dev-trigger route.
        inspection_alert_feed = RedisGatewayInspectionAlertFeed(
            sqlite_alert_feed,
            runtime_stream_client,
            instance_id=process_id("realtime-gateway"),
        )
        dev_alert_publisher = RedisDurableInspectionAlertFeed(
            sqlite_alert_feed, runtime_stream_client
        )
    else:
        inspection_alert_feed = sqlite_alert_feed
        dev_alert_publisher = sqlite_alert_feed

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        audit_verification_monitor.startup_sample_verify()
        daily_audit_verification.start()
        try:
            yield
        finally:
            await daily_audit_verification.stop()
            if task_repository is not None:
                task_repository.close()
            close_feed = getattr(inspection_alert_feed, "close", None)
            if close_feed is not None:
                result = close_feed()
                if inspect.isawaitable(result):
                    await result
            close = getattr(runtime_stream_client, "close", None)
            if close is not None:
                close()
            engine.dispose()

    app = FastAPI(title=runtime_settings.app_name, lifespan=lifespan)

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(_request: Request, error: SQLAlchemyError) -> JSONResponse:
        if _is_database_unavailable(error):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Database unavailable"},
            )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Database operation failed"},
        )

    app.add_middleware(CorrelationIdMiddleware)
    resolved_actor_repository = actor_repository or SqlAlchemyActorRepository(session_factory)
    app.state.session_factory = session_factory
    app.state.actor_repository = resolved_actor_repository
    app.state.jwt_authenticator = JwtAuthenticator(
        runtime_settings.auth_jwt_secret,
        resolved_actor_repository,
    )
    app.include_router(health_router)
    app.include_router(create_metrics_router(registry))
    inspection_service = InspectionService(MockVisionAdapter(), metric_registry=registry)
    if active_seed is None and managed_database:
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
    elif active_seed is not None:
        initial_cases = active_seed.cases
        persistence_seed = active_seed
    else:
        initial_cases = ()
        persistence_seed = DemoSeed((), {}, (), (), "externally-seeded")
    if managed_database:
        seed_business_data(session_factory, persistence_seed, PasswordHasher().hash)
    resolved_password_verifier = password_verifier or Argon2PasswordVerifier(
        SqlAlchemyPasswordCredentialRepository(session_factory)
    )
    if runtime_settings.environment.lower() in {"production", "docker", "staging"}:
        reauthentication_store = RedisReauthenticationStore(runtime_stream_client)
        websocket_ticket_store = RedisWebSocketTicketStore(runtime_stream_client)
    else:
        reauthentication_store = SqliteReauthenticationStore(session_factory)
        websocket_ticket_store = SqliteWebSocketTicketStore(session_factory)
    reauthentication_service = ReauthenticationService(reauthentication_store)
    websocket_ticket_service = WebSocketTicketService(websocket_ticket_store)
    app.state.websocket_ticket_service = websocket_ticket_service
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
    if active_seed is not None and _should_seed_knowledge(runtime_settings):
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
    app.state.inspection_alert_feed = inspection_alert_feed
    app.state.metric_registry = registry

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
    # Docker/staging/prod facts are seeded exactly once by the migrate service.
    # App-managed SQLite runtimes need the same deterministic initial snapshot.
    if managed_database:
        for case in initial_cases:
            for event in case.inspection_events:
                inspection_alert_feed.publish(event.to_alert(), event.line_id)
    app.include_router(
        create_notifications_router(inspection_alert_feed)
    )
    if runtime_settings.enable_dev_event_trigger and runtime_settings.environment in {"local", "test", "docker"}:
        app.include_router(create_development_notifications_router(dev_alert_publisher))
    app.include_router(
        create_auth_router(
            reauthentication_service=reauthentication_service,
            password_verifier=resolved_password_verifier,
            actor_repository=resolved_actor_repository,
            websocket_ticket_service=websocket_ticket_service,
        )
    )
    return app


def _retrieval_index(settings: Settings) -> KnowledgeIndexPort:
    """Select the knowledge index backend for this runtime configuration."""
    if settings.retrieval_backend == "pgvector":
        # psycopg is imported lazily inside the executor; local tests and the
        # in-memory backend never load the driver.
        return PgVectorPostgresAdapter(
            psycopg_executor(settings.postgres_url or ""),
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


def _should_seed_knowledge(settings: Settings) -> bool:
    if settings.environment not in {"local", "test"}:
        return False
    if settings.seed_knowledge_on_startup is not None:
        return settings.seed_knowledge_on_startup
    return True


def _runtime_stream_client(settings: Settings) -> object:
    if settings.environment.lower() in {"production", "docker", "staging"}:
        return RedisSocketStreamClient(settings.redis_url or "")
    return SQLiteStreamClient(settings.task_database_path)
