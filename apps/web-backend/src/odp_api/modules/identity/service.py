from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Callable, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from odp_api.modules.identity.models import Actor

REAUTHENTICATION_TTL_SECONDS = 300


class RecentReauthenticationRequired(PermissionError):
    """Raised when a simulated high-risk command lacks a five-minute marker."""


class PasswordVerifier(Protocol):
    def verify(self, actor_id: UUID, password: str) -> bool: ...


class InMemoryPasswordVerifier:
    """Deterministic password adapter for tests and the local demo only."""

    def __init__(self, passwords: dict[UUID, str]) -> None:
        self._passwords = passwords

    def verify(self, actor_id: UUID, password: str) -> bool:
        expected = self._passwords.get(actor_id)
        return expected is not None and compare_digest(expected, password)


class InMemoryReauthenticationStore:
    """Minimal Redis-compatible expiring marker store with no external dependency."""

    def __init__(self) -> None:
        self._markers: dict[str, tuple[datetime, datetime]] = {}

    def set_last_reauth_at(self, actor_id: UUID, occurred_at: datetime) -> None:
        self._markers[f"last_reauth_at:{actor_id}"] = (
            occurred_at,
            occurred_at + timedelta(seconds=REAUTHENTICATION_TTL_SECONDS),
        )

    def get_last_reauth_at(self, actor_id: UUID, now: datetime) -> datetime | None:
        key = f"last_reauth_at:{actor_id}"
        marker = self._markers.get(key)
        if marker is None:
            return None
        occurred_at, expires_at = marker
        if now > expires_at:
            del self._markers[key]
            return None
        return occurred_at


class ReauthenticationService:
    def __init__(self, store: InMemoryReauthenticationStore) -> None:
        self._store = store

    def record_success(self, actor_id: UUID, now: datetime) -> None:
        self._store.set_last_reauth_at(actor_id, now)

    def require_recent_reauth(self, actor_id: UUID, now: datetime) -> None:
        if self._store.get_last_reauth_at(actor_id, now) is None:
            raise RecentReauthenticationRequired("Recent reauthentication is required.")


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Actor:
    """Authenticate a Bearer JWT using the runtime-configured actor resolver."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        return request.app.state.jwt_authenticator.authenticate(credentials.credentials)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        ) from error


class WebSocketAuthenticationError(ValueError):
    """Raised when a WebSocket handshake lacks a valid Bearer credential."""


def get_current_websocket_actor(websocket: WebSocket) -> Actor:
    """Authenticate a WebSocket's Authorization header without HTTPBearer(Request)."""
    authorization = websocket.headers.get("authorization")
    if authorization is None:
        raise WebSocketAuthenticationError("Authentication required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token:
        raise WebSocketAuthenticationError("Invalid credentials")
    try:
        return websocket.app.state.jwt_authenticator.authenticate(token)
    except ValueError as error:
        raise WebSocketAuthenticationError("Invalid credentials") from error


class ReauthenticateRequest(BaseModel):
    password: str


def create_auth_router(
    actor_provider: Callable[[], Actor] = get_current_actor,
    reauthentication_service: ReauthenticationService | None = None,
    password_verifier: PasswordVerifier | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
    service = reauthentication_service or ReauthenticationService(InMemoryReauthenticationStore())
    verifier = password_verifier or InMemoryPasswordVerifier({})

    @router.post("/reauthenticate")
    def reauthenticate(
        request: ReauthenticateRequest,
        actor: Actor = Depends(actor_provider),
    ) -> dict[str, bool]:
        if not verifier.verify(actor.actor_id, request.password):
            raise HTTPException(status_code=401, detail="Invalid password")
        service.record_success(actor.actor_id, datetime.now(UTC))
        return {"reauthenticated": True}

    return router
