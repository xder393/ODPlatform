from fastapi import APIRouter, FastAPI
from uuid import UUID

from odp_api.adapters.auth.jwt import ActorRepository, InMemoryActorRepository, JwtAuthenticator
from odp_api.adapters.vision.mock import MockVisionAdapter
from odp_api.modules.cases.router import InMemoryCaseRepository, create_cases_router
from odp_api.modules.identity.service import (
    InMemoryPasswordVerifier,
    InMemoryReauthenticationStore,
    PasswordVerifier,
    ReauthenticationService,
    create_auth_router,
)
from odp_api.modules.inspection.service import InspectionService
from odp_api.modules.notifications.router import (
    InMemoryInspectionAlertRepository,
    create_notifications_router,
)
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
) -> FastAPI:
    """Create the ODPlatform quality inspection API."""
    runtime_settings = settings or Settings()
    app = FastAPI(title=runtime_settings.app_name)
    app.state.jwt_authenticator = JwtAuthenticator(
        runtime_settings.auth_jwt_secret,
        actor_repository or InMemoryActorRepository({}),
    )
    app.include_router(health_router)
    inspection_service = InspectionService(MockVisionAdapter())
    fixture_case = inspection_service.inspect_fixture(
        FrameInput(fixture_name="scratch-frame-001", content=b"scratch-frame-001"),
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        camera_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    reauthentication_service = ReauthenticationService(InMemoryReauthenticationStore())
    app.include_router(
        create_cases_router(
            InMemoryCaseRepository((fixture_case,)),
            reauthentication_service=reauthentication_service,
        )
    )
    fixture_alerts = tuple(
        event.to_alert() for event in fixture_case.inspection_events
    )
    app.include_router(
        create_notifications_router(
            InMemoryInspectionAlertRepository(
                fixture_alerts,
                {event.event_id: event.line_id for event in fixture_case.inspection_events},
            ),
        )
    )
    app.include_router(
        create_auth_router(
            reauthentication_service=reauthentication_service,
            password_verifier=password_verifier or InMemoryPasswordVerifier({}),
        )
    )
    return app
