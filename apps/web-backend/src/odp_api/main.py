from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import APIRouter, FastAPI
from odp_api.adapters.auth.jwt import ActorRepository, InMemoryActorRepository, JwtAuthenticator
from odp_api.adapters.generation.mock import MockLLMAdapter
from odp_api.adapters.notifications.redis_stream import RedisStreamInspectionAlertFeed
from odp_api.adapters.redis_stream import RedisSocketStreamClient, SQLiteStreamClient
from odp_api.adapters.retrieval.pgvector import PgVectorRetrievalAdapter
from odp_api.adapters.tasks.redis_stream import RedisStreamTaskAlertPublisher, RedisStreamTaskQueue
from odp_api.adapters.tasks.sqlite import SQLiteTaskRepository
from odp_api.adapters.vision.mock import MockVisionAdapter
from odp_api.modules.ai_orchestration.router import create_advice_router
from odp_api.modules.ai_orchestration.service import AdviceService
from odp_api.modules.audit.service import AuditService, InMemoryAuditRepository
from odp_api.modules.audit.verify import AuditVerificationMonitor, ManagedDailyAuditVerification
from odp_api.modules.cases.router import InMemoryCaseRepository, create_cases_router
from odp_api.modules.identity.service import (
    InMemoryPasswordVerifier,
    InMemoryReauthenticationStore,
    PasswordVerifier,
    ReauthenticationService,
    create_auth_router,
)
from odp_api.modules.inspection.service import InspectionService
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
from odp_api.ports.vision import FrameInput
from odp_api.settings import Settings

health_router = APIRouter()


@health_router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    settings: Settings | None = None,
    actor_repository: ActorRepository | None = None,
    password_verifier: PasswordVerifier | None = None,
    daily_verification_interval_seconds: float = 24 * 60 * 60,
    stream_client: object | None = None,
    metric_registry: MetricRegistry | None = None,
) -> FastAPI:
    """Create the ODPlatform quality inspection API."""
    runtime_settings = settings or Settings()
    registry = metric_registry or DEFAULT_REGISTRY
    # Pre-register the documented metric set so /metrics always exposes every
    # series, including gauges that no code path has written yet.
    register_standard_metrics(registry)
    audit_service = AuditService(InMemoryAuditRepository())
    audit_verification_monitor = AuditVerificationMonitor(
        audit_service, metric_registry=registry
    )
    daily_audit_verification = ManagedDailyAuditVerification(
        audit_verification_monitor, daily_verification_interval_seconds
    )
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

    app = FastAPI(title=runtime_settings.app_name, lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    app.state.jwt_authenticator = JwtAuthenticator(
        runtime_settings.auth_jwt_secret,
        actor_repository or InMemoryActorRepository({}),
    )
    app.include_router(health_router)
    app.include_router(create_metrics_router(registry))
    inspection_service = InspectionService(MockVisionAdapter(), metric_registry=registry)
    fixture_case = inspection_service.inspect_fixture(
        FrameInput(fixture_name="scratch-frame-001", content=b"scratch-frame-001"),
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        camera_id=UUID("00000000-0000-0000-0000-000000000002"),
        event_id=UUID("00000000-0000-0000-0000-000000000003"),
    )
    reauthentication_service = ReauthenticationService(InMemoryReauthenticationStore())
    case_repository = InMemoryCaseRepository((fixture_case,))
    advice_service = AdviceService(
        PgVectorRetrievalAdapter(), MockLLMAdapter(), metric_registry=registry
    )
    app.state.audit_service = audit_service
    app.state.audit_verification_monitor = audit_verification_monitor
    app.state.daily_audit_verification = daily_audit_verification
    app.state.task_service = task_service
    app.state.advice_service = advice_service
    app.state.inspection_service = inspection_service

    app.include_router(
        create_cases_router(
            case_repository,
            reauthentication_service=reauthentication_service,
            audit_service=audit_service,
            metric_registry=registry,
        )
    )
    app.include_router(create_advice_router(case_repository, advice_service))
    for event in fixture_case.inspection_events:
        inspection_alert_feed.publish(event.to_alert(), event.line_id)
    app.include_router(
        create_notifications_router(inspection_alert_feed)
    )
    app.include_router(
        create_auth_router(
            reauthentication_service=reauthentication_service,
            password_verifier=password_verifier or InMemoryPasswordVerifier({}),
        )
    )
    return app


def _runtime_stream_client(settings: Settings) -> object:
    if settings.environment.lower() in {"production", "docker", "staging"}:
        return RedisSocketStreamClient(settings.redis_url)
    return SQLiteStreamClient(settings.task_database_path)
