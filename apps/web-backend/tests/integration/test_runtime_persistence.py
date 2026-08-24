from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from odp_api.main import create_app
from odp_api.seed import DEMO_ACCOUNTS, DEMO_ORG_ID, build_demo_seed
from odp_api.settings import Settings


CORRELATION_ID = UUID("40000000-0000-4000-8000-000000000002")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_jwt_secret="runtime-persistence-secret",
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        task_database_path=str(tmp_path / "tasks.db"),
    )


def _login(client: TestClient) -> str:
    email, password, _role = DEMO_ACCOUNTS[0]
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_transition_history_and_audit_chain_survive_runtime_restart(tmp_path) -> None:
    """Reverting runtime composition to in-memory repositories loses this restarted workflow."""
    settings = _settings(tmp_path)
    seed = build_demo_seed()
    with TestClient(create_app(settings=settings, seed=seed)) as first:
        token = _login(first)
        case_id = first.get("/api/v1/cases", headers=_auth(token)).json()[0]["case_id"]
        transitioned = first.post(
            f"/api/v1/cases/{case_id}/transitions",
            json={"status": "IN_REVIEW"},
            headers={**_auth(token), "X-Correlation-ID": str(CORRELATION_ID)},
        )
        transitioned.raise_for_status()

    with TestClient(create_app(settings=settings, seed=seed)) as restarted:
        token = _login(restarted)
        payload = restarted.get("/api/v1/cases", headers=_auth(token)).json()[0]

        assert payload["status"] == "IN_REVIEW"
        history = payload["history"][-1]
        assert history | {"occurred_at": None} == {
            "from_status": "PENDING_CONFIRMATION",
            "to_status": "IN_REVIEW",
            "actor_id": str(seed.actors[0].actor_id),
            "occurred_at": None,
            "correlation_id": str(CORRELATION_ID),
        }
        assert history["occurred_at"].endswith("Z")
        assert restarted.app.state.audit_service.verify_organization_chain(DEMO_ORG_ID).is_valid


def test_successful_simulated_pause_is_appended_to_the_durable_audit_chain(tmp_path) -> None:
    """Removing the pause audit command makes this durable audit fact disappear."""
    settings = _settings(tmp_path)
    seed = build_demo_seed()
    with TestClient(create_app(settings=settings, seed=seed)) as client:
        token = _login(client)
        case_id = client.get("/api/v1/cases", headers=_auth(token)).json()[0]["case_id"]
        reauthenticated = client.post(
            "/api/v1/auth/reauthenticate",
            json={"password": DEMO_ACCOUNTS[0][1]},
            headers=_auth(token),
        )
        reauthenticated.raise_for_status()

        paused = client.post(
            f"/api/v1/cases/{case_id}/pause",
            headers={**_auth(token), "X-Correlation-ID": str(CORRELATION_ID)},
        )

        assert paused.status_code == 200
        entries = client.app.state.audit_service.repository.read_consistent_chain(DEMO_ORG_ID).entries
        assert entries[-1].action == "defect_case.pause.simulated"
        assert entries[-1].correlation_id == CORRELATION_ID
