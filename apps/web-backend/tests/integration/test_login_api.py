"""Login endpoint and WebSocket ticket integration tests."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from odp_api.adapters.auth.jwt import extract_subject
from odp_api.adapters.persistence.models import ActorRow
from odp_api.main import create_app
from odp_api.seed import DEMO_ACCOUNTS, build_demo_seed


def _seeded_client() -> tuple[TestClient, object]:
    app = create_app(
        settings=_seeded_settings(),
        seed=build_demo_seed(),
    )
    return TestClient(app), app


def _seeded_settings():
    from odp_api.settings import Settings

    database_stem = f"/tmp/odp-login-{uuid4().hex}"
    return Settings(
        auth_jwt_secret="test-login-secret",
        database_url=f"sqlite:///{database_stem}.sqlite3",
        task_database_path=f"{database_stem}-tasks.sqlite3",
    )


def test_login_returns_a_token_for_a_seeded_account() -> None:
    client, _ = _seeded_client()
    email, password, _role = DEMO_ACCOUNTS[0]
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    token = response.json()["access_token"]
    # The issued token must be verifiable and carry the actor UUID subject.
    subject = extract_subject(token, "test-login-secret")
    assert isinstance(subject, UUID)


def test_login_rejects_a_wrong_password_with_401() -> None:
    client, _ = _seeded_client()
    email, _password, _role = DEMO_ACCOUNTS[0]
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"


def test_login_rejects_an_unknown_email_with_the_same_401() -> None:
    """Unknown email and wrong password must be indistinguishable."""
    client, _ = _seeded_client()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.test", "password": "odp-inspector-dev"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"


def test_login_token_authenticates_protected_endpoints() -> None:
    client, _ = _seeded_client()
    email, password, _role = DEMO_ACCOUNTS[0]
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/v1/cases", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 10


def test_protected_endpoint_requires_a_token() -> None:
    client, _ = _seeded_client()
    response = client.get("/api/v1/cases")
    assert response.status_code == 401


def test_websocket_accepts_a_bearer_issued_ticket() -> None:
    client, _ = _seeded_client()
    email, password, _role = DEMO_ACCOUNTS[0]
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).json()["access_token"]
    ticket = client.post(
        "/api/v1/auth/websocket-ticket", headers={"Authorization": f"Bearer {token}"}
    ).json()["ticket"]
    with client.websocket_connect(f"/ws/inspection-events?ticket={ticket}") as websocket:
        payload = websocket.receive_json()
        assert payload["alert"]["defect_class"] == "scratch"


def test_websocket_rejects_missing_ticket() -> None:
    from starlette.websockets import WebSocketDisconnect

    client, _ = _seeded_client()
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/ws/inspection-events"
    ):
        pass
    # The server closes with policy-violation 1008 before accepting the socket.
    assert exc_info.value.code == 1008


def test_disabled_actor_loses_existing_jwt_login_and_issued_websocket_ticket() -> None:
    """Disabling an actor must invalidate each authentication path on its next lookup."""
    client, app = _seeded_client()
    email, password, _role = DEMO_ACCOUNTS[0]
    token = client.post("/api/v1/auth/login", json={"email": email, "password": password}).json()["access_token"]
    ticket = client.post(
        "/api/v1/auth/websocket-ticket", headers={"Authorization": f"Bearer {token}"}
    ).json()["ticket"]
    actor_id = extract_subject(token, "test-login-secret")
    with app.state.session_factory.begin() as session:
        session.scalar(select(ActorRow).where(ActorRow.actor_id == actor_id)).enabled = False

    assert client.get("/api/v1/cases", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": email, "password": password}).status_code == 401
    with pytest.raises(
        WebSocketDisconnect
    ) as disconnected, client.websocket_connect(f"/ws/inspection-events?ticket={ticket}"):
        pass
    assert disconnected.value.code == 1008
