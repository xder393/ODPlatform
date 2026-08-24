"""Development trigger contracts used by the browser E2E workflow."""

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from odp_api.adapters.persistence.models import Base
from odp_api.db import create_engine_and_session
from odp_api.main import create_app
from odp_api.seed import DEMO_ACCOUNTS, DEMO_LINE_ID, build_demo_seed
from odp_api.settings import Settings


def _settings(
    tmp_path: Path, *, environment: str = "local", enable_dev_event_trigger: bool = False
) -> Settings:
    return Settings(
        environment=environment,
        enable_dev_event_trigger=enable_dev_event_trigger,
        auth_jwt_secret="dev-trigger-test-secret",
        database_url=f"sqlite:///{tmp_path / f'{environment}.db'}",
        task_database_path=str(tmp_path / f"{environment}-tasks.db"),
    )


def _token(client: TestClient) -> str:
    email, password, _ = DEMO_ACCOUNTS[0]
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def test_development_trigger_publishes_a_future_authorized_line_event(tmp_path: Path) -> None:
    """Replacing the trigger with an unauthenticated or cross-line publish is a security bug."""
    app = create_app(settings=_settings(tmp_path, enable_dev_event_trigger=True), seed=build_demo_seed())
    event_id = uuid4()
    with TestClient(app) as client:
        token = _token(client)
        response = client.post(
            "/api/v1/dev/inspection-events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_id": str(event_id), "line_id": str(DEMO_LINE_ID)},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["alert"]["event_id"] == str(event_id)
    assert payload["cursor"].isdigit()


def test_development_trigger_rejects_an_unauthorized_line(tmp_path: Path) -> None:
    """Deriving tenant scope only from request JSON would let an inspector publish to another line."""
    app = create_app(settings=_settings(tmp_path, enable_dev_event_trigger=True), seed=build_demo_seed())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/dev/inspection-events",
            headers={"Authorization": f"Bearer {_token(client)}"},
            json={"event_id": str(uuid4()), "line_id": str(uuid4())},
        )

    assert response.status_code == 403


@pytest.mark.parametrize("environment", ["local", "staging", "production", "prod"])
def test_development_trigger_requires_explicit_enablement(tmp_path: Path, environment: str) -> None:
    """Environment spelling must not accidentally expose a generic event-publication backdoor."""
    class FakeRedis:
        def xadd(self, *_: object) -> str:
            return "1-0"

        def xadd_bounded(self, *_: object) -> str:
            return "1-0"

        def xlen(self, *_: object) -> int:
            return 0

        def close(self) -> None:
            return None

    settings = _settings(tmp_path, environment=environment)
    if environment == "staging":
        engine, _ = create_engine_and_session(settings.database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
    app = create_app(
        settings=settings,
        seed=build_demo_seed(),
        stream_client=FakeRedis(),
    )

    assert "/api/v1/dev/inspection-events" not in {
        route.path for route in app.routes if hasattr(route, "path")
    }
