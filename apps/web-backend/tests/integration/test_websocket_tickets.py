"""End-to-end contracts for opaque, one-time WebSocket tickets."""

from datetime import UTC, datetime, timedelta
import json
import logging
import logging.config
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from odp_api.adapters.auth.redis_security import RedisWebSocketTicketStore
from odp_api.adapters.auth.sqlite_security import SqliteWebSocketTicketStore
from odp_api.adapters.persistence.models import WebSocketTicketRow
from odp_api.adapters.persistence.repositories import SqlAlchemyPasswordCredentialRepository
from odp_api.main import create_app
from odp_api.modules.identity.tickets import InvalidWebSocketTicket, WebSocketTicketService
from odp_api.observability.logging import configure_uvicorn_access_logging
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


def test_sqlite_ticket_store_prunes_expired_rows_on_consume_and_issue(tmp_path: Path) -> None:
    """Without expiry cleanup, durable ticket rows grow indefinitely after their TTL."""
    app = create_app(settings=_settings(tmp_path), seed=build_demo_seed())
    service = WebSocketTicketService(SqliteWebSocketTicketStore(app.state.session_factory))
    actor_id = uuid4()

    expired = service.issue(actor_id, NOW)
    with pytest.raises(InvalidWebSocketTicket):
        service.consume(expired, NOW + timedelta(seconds=61))
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WebSocketTicketRow)) == 0

    service.issue(actor_id, NOW)
    service.issue(actor_id, NOW + timedelta(seconds=61))
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WebSocketTicketRow)) == 1


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
            assert websocket.receive_json()["alert"]["defect_class"] == "scratch"

        with pytest.raises(WebSocketDisconnect) as reused:
            with client.websocket_connect(f"/ws/inspection-events?ticket={ticket}"):
                pass
        assert reused.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as legacy:
            with client.websocket_connect(f"/ws/inspection-events?token={token}"):
                pass
        assert legacy.value.code == 1008


def test_uvicorn_websocket_access_logs_strip_valid_reused_and_legacy_credentials(
    caplog, capsys
) -> None:
    """Uvicorn's access logger must never format a WebSocket query credential."""
    configure_uvicorn_access_logging()
    access_logger = logging.getLogger("uvicorn.access")
    stderr_handler = logging.StreamHandler(sys.stderr)
    access_logger.addHandler(stderr_handler)
    previous_level = access_logger.level
    previous_propagate = access_logger.propagate
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = True
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    valid_ticket = "valid-ticket-secret"
    reused_ticket = "reused-ticket-secret"
    legacy_jwt = "legacy-jwt-secret"
    try:
        for path in (
            f"/ws/inspection-events?ticket={valid_ticket}",
            f"/ws/inspection-events?ticket={reused_ticket}",
            f"/ws/inspection-events?token={legacy_jwt}",
        ):
            # This is Uvicorn's actual access-log argument shape for a handshake.
            access_logger.info('%s - "%s %s HTTP/%s" %d', "testclient", "GET", path, "1.1", 101)
        captured = capsys.readouterr()
    finally:
        access_logger.removeHandler(stderr_handler)
        access_logger.setLevel(previous_level)
        access_logger.propagate = previous_propagate

    for secret in (valid_ticket, reused_ticket, legacy_jwt):
        assert secret not in captured.out
        assert secret not in captured.err
        assert secret not in caplog.text
        assert all(secret not in record.getMessage() for record in caplog.records)


def test_uvicorn_error_websocket_handshake_logs_strip_query_credentials(caplog, capsys) -> None:
    """Uvicorn 0.52 logs WebSocket acceptance/rejection on ``uvicorn.error``."""
    configure_uvicorn_access_logging()
    error_logger = logging.getLogger("uvicorn.error")
    stdout_handler = logging.StreamHandler(sys.stdout)
    stderr_handler = logging.StreamHandler(sys.stderr)
    error_logger.addHandler(stdout_handler)
    error_logger.addHandler(stderr_handler)
    error_logger.addHandler(caplog.handler)
    previous_level = error_logger.level
    previous_propagate = error_logger.propagate
    error_logger.setLevel(logging.INFO)
    error_logger.propagate = False
    valid_ticket = "valid-error-ticket-secret"
    reused_ticket = "reused-error-ticket-secret"
    legacy_jwt = "legacy-error-jwt-secret"
    try:
        # These are Uvicorn 0.52.4's real WebSocket handshake log records.
        error_logger.info(
            '%s - "WebSocket %s" [accepted]',
            "testclient",
            f"/ws/inspection-events?ticket={valid_ticket}",
        )
        error_logger.info(
            '%s - "WebSocket %s" 403',
            "testclient",
            f"/ws/inspection-events?ticket={reused_ticket}",
        )
        error_logger.info(
            '%s - "WebSocket %s" 403',
            "testclient",
            f"/ws/inspection-events?token={legacy_jwt}",
        )
        error_logger.info("%s", "non-WebSocket lifecycle log remains readable")
        captured = capsys.readouterr()
    finally:
        error_logger.removeHandler(stdout_handler)
        error_logger.removeHandler(stderr_handler)
        error_logger.removeHandler(caplog.handler)
        error_logger.setLevel(previous_level)
        error_logger.propagate = previous_propagate

    for secret in (valid_ticket, reused_ticket, legacy_jwt):
        assert secret not in captured.out
        assert secret not in captured.err
        assert secret not in caplog.text
        assert all(secret not in record.getMessage() for record in caplog.records)
    assert "non-WebSocket lifecycle log remains readable" in captured.out


def test_uvicorn_error_non_websocket_question_mark_message_survives_real_log_config(capsys) -> None:
    """A broad sanitizer must not corrupt ordinary Uvicorn error formatting."""
    config_path = Path(__file__).parents[2] / "src" / "odp_api" / "observability" / "uvicorn_logging.json"
    logging.config.dictConfig(json.loads(config_path.read_text()))
    logger = logging.getLogger("uvicorn.error")

    logger.error("Health probe failed? retrying %s", "soon")
    captured = capsys.readouterr()

    assert "Health probe failed? retrying soon" in captured.err
    assert "Logging error" not in captured.err


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
