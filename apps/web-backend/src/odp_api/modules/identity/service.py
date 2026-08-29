from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from odp_api.modules.identity.models import Actor
from odp_api.modules.identity.ports import (
    REAUTHENTICATION_TTL_SECONDS,
    ReauthenticationStorePort,
)
from odp_api.modules.identity.tickets import WebSocketTicketService


class RecentReauthenticationRequired(PermissionError):
    """Raised when a simulated high-risk command lacks a five-minute marker."""


class PasswordVerifier(Protocol):
    def verify(self, actor_id: UUID, password: str) -> bool: ...


class ActorEmailLookup(Protocol):
    def get_by_email(self, email: str) -> Actor | None: ...


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
    def __init__(self, store: ReauthenticationStorePort) -> None:
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
    """Consume an opaque ticket, then reload its actor's current grants."""
    ticket = websocket.query_params.get("ticket")
    if not ticket:
        raise WebSocketAuthenticationError("Authentication required")
    try:
        service: WebSocketTicketService = websocket.app.state.websocket_ticket_service
        actor_id = service.consume(ticket)
        actor = websocket.app.state.actor_repository.get(actor_id)
        if actor is None:
            raise WebSocketAuthenticationError("Invalid credentials")
        return actor
    except ValueError as error:
        raise WebSocketAuthenticationError("Invalid credentials") from error


class ReauthenticateRequest(BaseModel):
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def create_auth_router(
    actor_provider: Callable[[], Actor] = get_current_actor,
    reauthentication_service: ReauthenticationService | None = None,
    password_verifier: PasswordVerifier | None = None,
    actor_repository: ActorEmailLookup | None = None,
    websocket_ticket_service: WebSocketTicketService | None = None,
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

    @router.post("/login")
    def login(request: LoginRequest, http_request: Request) -> dict[str, str]:
        """Exchange demo credentials for a compact JWT access token.

        Unknown email and wrong password deliberately produce the same 401 so
        the endpoint never discloses which accounts exist.
        """
        repository = actor_repository
        if repository is None:
            raise HTTPException(status_code=503, detail="Actor lookup is not configured.")
        actor = repository.get_by_email(request.email.strip().lower())
        if actor is None or not verifier.verify(actor.actor_id, request.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        authenticator = getattr(http_request.app.state, "jwt_authenticator", None)
        secret = getattr(authenticator, "secret", None)
        if not secret:
            raise HTTPException(status_code=503, detail="JWT authentication is not configured.")
        # Deferred import keeps this module free of adapter imports; the pure
        # signing function lives next to the verification logic it pairs with.
        from odp_api.adapters.auth.jwt import issue_token

        return {"access_token": issue_token(actor.actor_id, secret)}

    @router.post("/websocket-ticket")
    def websocket_ticket(actor: Actor = Depends(actor_provider)) -> dict[str, str | int]:
        if websocket_ticket_service is None:
            raise HTTPException(status_code=503, detail="WebSocket authentication is not configured.")
        return {"ticket": websocket_ticket_service.issue(actor.actor_id), "expires_in": 60}

    return router
