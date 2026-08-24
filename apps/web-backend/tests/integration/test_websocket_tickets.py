"""End-to-end contracts for opaque, one-time WebSocket tickets."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from odp_api.adapters.auth.redis_security import RedisWebSocketTicketStore
from odp_api.adapters.persistence.repositories import SqlAlchemyPasswordCredentialRepository
from odp_api.main import create_app
from odp_api.modules.identity.tickets import InvalidWebSocketTicket, SqliteWebSocketTicketStore, WebSocketTicketService
from odp_api.seed import DEMO_ACCOUNTS, build_demo_seed
from odp_api.settings import Settings


NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_jwt_secret="websocket-ticket-test-secret",
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        task_database_path=str(tmp_path / "tasks.db"),
    )


def _login(client: TestClient) -> str:
    email, password, _role = DEMO_ACCOUNTS[0]
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def test_password_database_contains_argon2_hash_and_login_uses_it(tmp_path: Path) -> None:
    """Replacing the runtime verifier with plaintext data would make this fail."""
    with TestClient(create_app(settings=_settings(tmp_path), seed=build_demo_seed())) as client:
        credentials = SqlAlchemyPasswordCredentialRepository(client.app.state.session_factory)
        assert credentials.password_hash(build_demo_seed().actors[0].actor_id).startswith("$argon2id$")
        assert _login(client)


def test_ticket_service_consumes_once_and_rejects_expiry(tmp_path: Path) -> None:
    """A non-atomic read or an ignored expiry would let a ticket authenticate twice."""
    app = create_app(settings=_settings(tmp_path), seed=build_demo_seed())
    service = WebSocketTicketService(SqliteWebSocketTicketStore(app.state.session_factory))
    actor_id = uuid4()

    ticket = service.issue(actor_id, NOW)
    assert len(ticket.encode()) >= 43  # URL-safe encoding of 256 random bits.
    assert service.consume(ticket, NOW) == actor_id
    with pytest.raises(InvalidWebSocketTicket):
        service.consume(ticket, NOW)

    expired = service.issue(actor_id, NOW)
    with pytest.raises(InvalidWebSocketTicket):
        service.consume(expired, NOW + timedelta(seconds=61))


def test_redis_ticket_store_uses_digest_key_ttl_and_atomic_consumption() -> None:
    """A raw-ticket key or non-atomic read/delete would expose or reuse the credential."""
    class FakeRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}
            self.set_calls: list[tuple[str, int, bool]] = []

        def set_ex(self, key: str, value: str, seconds: int, *, nx: bool = False) -> bool:
            self.set_calls.append((key, seconds, nx))
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

        def get(self, key: str) -> str | None:
            return self.values.get(key)

        def getdel(self, key: str) -> str | None:
            return self.values.pop(key, None)

    fake = FakeRedis()
    store = RedisWebSocketTicketStore(fake)
    ticket = "not-stored-in-redis-keys"
    actor_id = uuid4()

    assert store.issue(ticket, actor_id, NOW + timedelta(seconds=60))
    key, ttl, nx = fake.set_calls[0]
    assert ticket not in key
    assert ttl == 60 and nx is True
    assert store.consume(ticket, NOW) == actor_id
    assert store.consume(ticket, NOW) is None


def test_ticket_endpoint_is_bearer_protected_single_use_and_does_not_echo_ticket(tmp_path: Path) -> None:
    """Returning JWT query authentication or echoing an invalid ticket leaks a reusable credential."""
    with TestClient(create_app(settings=_settings(tmp_path), seed=build_demo_seed())) as client:
        token = _login(client)
        denied = client.post("/api/v1/auth/websocket-ticket")
        assert denied.status_code == 401

        issued = client.post(
            "/api/v1/auth/websocket-ticket",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert issued.status_code == 200
        payload = issued.json()
        ticket = payload["ticket"]
        assert payload["expires_in"] == 60
        assert token not in issued.text

        with client.websocket_connect(f"/ws/inspection-events?ticket={ticket}") as websocket:
            assert websocket.receive_json()["defect_class"] == "scratch"

        with pytest.raises(WebSocketDisconnect) as reused:
            with client.websocket_connect(f"/ws/inspection-events?ticket={ticket}"):
                pass
        assert reused.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as legacy:
            with client.websocket_connect(f"/ws/inspection-events?token={token}"):
                pass
        assert legacy.value.code == 1008


def test_reauthentication_marker_survives_a_local_runtime_restart(tmp_path: Path) -> None:
    """Replacing durable markers with a process-local dictionary loses high-risk authorization."""
    settings = _settings(tmp_path)
    seed = build_demo_seed()
    with TestClient(create_app(settings=settings, seed=seed)) as first:
        token = _login(first)
        reauthenticated = first.post(
            "/api/v1/auth/reauthenticate",
            headers={"Authorization": f"Bearer {token}"},
            json={"password": DEMO_ACCOUNTS[0][1]},
        )
        assert reauthenticated.status_code == 200

    with TestClient(create_app(settings=settings, seed=seed)) as restarted:
        token = _login(restarted)
        case_id = restarted.get("/api/v1/cases", headers={"Authorization": f"Bearer {token}"}).json()[0]["case_id"]
        paused = restarted.post(
            f"/api/v1/cases/{case_id}/pause", headers={"Authorization": f"Bearer {token}"}
        )
        assert paused.status_code == 200
