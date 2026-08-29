from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from odp_api.adapters.auth.jwt import InMemoryActorRepository, issue_token
from odp_api.main import create_app
from odp_api.seed import DEMO_ACCOUNTS, build_demo_seed
from odp_api.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_jwt_secret="database-availability-secret",
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        task_database_path=str(tmp_path / "tasks.db"),
    )


def _unavailable_database_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, OSError("database dsn must not leak"))


def test_healthz_checks_database_and_returns_503_when_it_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    """Removing the SELECT 1 check would return 200 after the runtime database disappears."""
    app = create_app(settings=_settings(tmp_path), seed=build_demo_seed())

    class UnavailableSession:
        def __enter__(self):
            raise _unavailable_database_error()

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    monkeypatch.setattr(app.state, "session_factory", lambda: UnavailableSession())
    response = TestClient(app, raise_server_exceptions=False).get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert "dsn" not in response.text


def test_login_maps_database_connectivity_failure_to_a_safe_503(
    tmp_path, monkeypatch
) -> None:
    """A database failure during actor lookup must not become a leaked 500 response."""
    app = create_app(settings=_settings(tmp_path), seed=build_demo_seed())
    monkeypatch.setattr(
        app.state.actor_repository,
        "get_by_email",
        lambda _email: (_ for _ in ()).throw(_unavailable_database_error()),
    )
    email, password, _role = DEMO_ACCOUNTS[0]

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert "dsn" not in response.text


def test_case_query_maps_database_connectivity_failure_to_a_safe_503(
    tmp_path, monkeypatch
) -> None:
    """A lost case-store connection must not be reported as authorization or an internal DSN error."""
    seed = build_demo_seed()
    app = create_app(
        settings=_settings(tmp_path),
        seed=seed,
        actor_repository=InMemoryActorRepository(
            {actor.actor_id: actor for actor in seed.actors}
        ),
    )
    monkeypatch.setattr(
        app.state.case_repository,
        "list",
        lambda _organization_id, _updated_after: (_ for _ in ()).throw(
            _unavailable_database_error()
        ),
    )
    token = issue_token(seed.actors[0].actor_id, "database-availability-secret")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/cases", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert "dsn" not in response.text
