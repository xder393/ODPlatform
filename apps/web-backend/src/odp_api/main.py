from fastapi import APIRouter, FastAPI

from odp_api.settings import Settings


health_router = APIRouter()


@health_router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the ODPlatform quality inspection API."""
    runtime_settings = settings or Settings()
    app = FastAPI(title=runtime_settings.app_name)
    app.include_router(health_router)
    return app
