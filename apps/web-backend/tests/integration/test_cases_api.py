from pathlib import Path
import sys

from fastapi.testclient import TestClient


WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.main import create_app


def test_fixture_case_can_be_listed_with_reproducible_detection_metadata_and_reviewed() -> None:
    """A missing fixture-to-case workflow or transition route makes this fail."""
    client = TestClient(create_app())

    listed = client.get("/api/v1/cases")

    assert listed.status_code == 200
    cases = listed.json()
    assert len(cases) == 1
    case = cases[0]
    assert case["status"] == "PENDING_CONFIRMATION"
    assert case["inspection_events"] == [
        {
            "defect_class": "scratch",
            "confidence": 0.964,
            "model_release": "mock-yolo-1.0",
            "preprocessing_parameters": {"fixture": "scratch-frame-001"},
            "threshold": 0.8,
            "input_frame_sha256": "17a3b51349eb5e1bc31b3c46811941b49c7b2ef32038f9a3ecae48c3c635664a",
        }
    ]

    transitioned = client.post(
        f"/api/v1/cases/{case['case_id']}/transitions",
        json={"status": "IN_REVIEW"},
    )

    assert transitioned.status_code == 200
    assert transitioned.json()["status"] == "IN_REVIEW"
