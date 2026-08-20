"""Pure, HMAC-SHA256 JWT authentication adapter for the local API runtime."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest, new
import json
from typing import Protocol
from uuid import UUID

from odp_api.modules.identity.models import Actor


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


def extract_subject(token: str, secret: str) -> UUID:
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
