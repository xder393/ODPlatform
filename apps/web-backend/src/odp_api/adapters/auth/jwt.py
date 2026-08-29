"""Pure, HMAC-SHA256 JWT authentication adapter for the local API runtime."""

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest, new
from typing import Protocol
from uuid import UUID

from odp_api.modules.identity.models import Actor

# Matches the 12-hour login grant documented for the demo.
ACCESS_TOKEN_TTL_SECONDS = 12 * 60 * 60


class InvalidJwtSubject(ValueError):
    """Raised when a compact token is malformed, unsigned, or has no valid subject."""


class ActorRepository(Protocol):
    def get(self, actor_id: UUID) -> Actor | None: ...


class InMemoryActorRepository:
    """Process-local actor resolver for test and development app composition."""

    def __init__(self, actors: Mapping[UUID, Actor]) -> None:
        self._actors = dict(actors)

    def get(self, actor_id: UUID) -> Actor | None:
        return self._actors.get(actor_id)

    def get_by_email(self, email: str) -> Actor | None:
        """Return the actor registered under exactly this email address."""
        for actor in self._actors.values():
            if actor.email == email:
                return actor
        return None


@dataclass(frozen=True, slots=True)
class JwtAuthenticator:
    secret: str | None
    actors: ActorRepository

    def authenticate(self, token: str) -> Actor:
        if not self.secret:
            raise InvalidJwtSubject("JWT authentication is not configured.")
        actor_id = extract_subject(token, self.secret)
        actor = self.actors.get(actor_id)
        if actor is None:
            raise InvalidJwtSubject("JWT subject has no active actor grant.")
        return actor


def issue_token(
    actor_id: UUID,
    secret: str,
    *,
    expires_in_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """Issue a compact HS256 JWT carrying the actor UUID in its ``sub`` claim.

    The payload includes mandatory integer ``iat`` and ``exp`` claims that
    ``extract_subject`` validates against the current server time.
    """
    issued_at = int((now or datetime.now(UTC)).timestamp())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(actor_id),
        "iat": issued_at,
        "exp": issued_at + expires_in_seconds,
    }
    encoded_header = _encode_segment(json.dumps(header, separators=(",", ":")))
    encoded_payload = _encode_segment(json.dumps(payload, separators=(",", ":")))
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    signature = base64.urlsafe_b64encode(
        new(secret.encode(), signing_input, sha256).digest()
    ).rstrip(b"=").decode()
    return f"{encoded_header}.{encoded_payload}.{signature}"


def extract_subject(token: str, secret: str, *, now: datetime | None = None) -> UUID:
    """Verify a compact HS256 JWT and return its UUID ``sub`` claim."""
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        signing_input = f"{encoded_header}.{encoded_payload}".encode()
        expected_signature = new(secret.encode(), signing_input, sha256).digest()
        supplied_signature = _decode_segment(encoded_signature)
        if not compare_digest(supplied_signature, expected_signature):
            raise InvalidJwtSubject("JWT signature is invalid.")
        header = json.loads(_decode_segment(encoded_header))
        if not isinstance(header, dict):
            raise InvalidJwtSubject("JWT header must be an object.")
        if header.get("alg") != "HS256":
            raise InvalidJwtSubject("JWT algorithm is not allowed.")
        payload = json.loads(_decode_segment(encoded_payload))
        if not isinstance(payload, dict):
            raise InvalidJwtSubject("JWT payload must be an object.")
        _validate_temporal_claims(payload, now or datetime.now(UTC))
        return UUID(payload["sub"])
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        if isinstance(error, InvalidJwtSubject):
            raise
        raise InvalidJwtSubject("JWT must contain a UUID subject claim.") from error


def _decode_segment(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode_segment(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def _validate_temporal_claims(payload: dict[object, object], now: datetime) -> None:
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
    ):
        raise InvalidJwtSubject("JWT temporal claims are invalid.")
    current_timestamp = int(now.timestamp())
    if expires_at <= issued_at or expires_at <= current_timestamp or issued_at > current_timestamp + 60:
        raise InvalidJwtSubject("JWT temporal claims are invalid.")
