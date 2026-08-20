import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
from uuid import UUID, uuid4

from fastapi.testclient import TestClient


WEB_BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.adapters.auth.jwt import InMemoryActorRepository
from odp_api.main import create_app
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import InMemoryPasswordVerifier
from odp_api.settings import Settings


def signed_token(subject: UUID, secret: str) -> str:
    def encode(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode({"sub": str(subject)})
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def test_runtime_rejects_missing_and_invalid_bearer_tokens() -> None:
    client = TestClient(create_app())

    missing = client.get("/api/v1/cases")
    invalid = client.get("/api/v1/cases", headers={"Authorization": "Bearer invalid"})

    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_runtime_resolves_distinct_bearer_subjects_to_tenant_scoped_actors() -> None:
    secret = "test-signing-secret"
    fixture_organization_id = UUID("00000000-0000-0000-0000-000000000001")
    permitted_actor = Actor(uuid4(), fixture_organization_id, Role.ADMINISTRATOR, frozenset())
    foreign_actor = Actor(uuid4(), uuid4(), Role.ADMINISTRATOR, frozenset())
    app = create_app(
        Settings(auth_jwt_secret=secret),
        actor_repository=InMemoryActorRepository(
            {permitted_actor.actor_id: permitted_actor, foreign_actor.actor_id: foreign_actor}
        ),
    )
    client = TestClient(app)

    permitted = client.get(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {signed_token(permitted_actor.actor_id, secret)}"},
    )
    foreign = client.get(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {signed_token(foreign_actor.actor_id, secret)}"},
    )

    assert permitted.status_code == 200
    assert len(permitted.json()) == 1
    assert foreign.status_code == 200
    assert foreign.json() == []


def test_reauthentication_uses_the_authenticated_bearer_subject() -> None:
    secret = "test-signing-secret"
    actor = Actor(uuid4(), uuid4(), Role.INSPECTOR, frozenset())
    app = create_app(
        Settings(auth_jwt_secret=secret),
        actor_repository=InMemoryActorRepository({actor.actor_id: actor}),
        password_verifier=InMemoryPasswordVerifier({actor.actor_id: "actor-password"}),
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/reauthenticate",
        headers={"Authorization": f"Bearer {signed_token(actor.actor_id, secret)}"},
        json={"password": "actor-password"},
    )

    assert response.status_code == 200
    assert response.json() == {"reauthenticated": True}
