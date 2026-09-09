import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

WEB_BACKEND_SRC = Path(__file__).parents[1] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[3] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_schemas.events import InspectionAlert

from odp_api.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz_returns_ok(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_alert_requires_confidence_between_zero_and_one() -> None:
    valid_alert = {
        "event_id": uuid4(),
        "organization_id": uuid4(),
        "camera_id": uuid4(),
        "occurred_at": datetime.now(UTC),
        "defect_class": "scratch",
    }

    with pytest.raises(ValidationError):
        InspectionAlert(confidence=1.1, **valid_alert)
