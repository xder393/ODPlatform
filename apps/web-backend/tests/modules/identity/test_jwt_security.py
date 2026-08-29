"""Regression coverage for temporal JWT validation."""

import base64
import json
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new
from uuid import uuid4

import pytest

from odp_api.adapters.auth.jwt import InvalidJwtSubject, extract_subject, issue_token

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
SECRET = "jwt-security-test-secret"


def _signed_claims(claims: dict[str, object]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(new(SECRET.encode(), f"{header}.{payload}".encode(), sha256).digest()).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": str(uuid4()), "iat": int(NOW.timestamp())},
        {"sub": str(uuid4()), "exp": int(NOW.timestamp()) + 60},
        {"sub": str(uuid4()), "iat": "invalid", "exp": int(NOW.timestamp()) + 60},
        {"sub": str(uuid4()), "iat": int(NOW.timestamp()) + 61, "exp": int(NOW.timestamp()) + 3600},
        {"sub": str(uuid4()), "iat": int(NOW.timestamp()) - 3600, "exp": int(NOW.timestamp()) - 1},
        {"sub": str(uuid4()), "iat": int(NOW.timestamp()) + 30, "exp": int(NOW.timestamp()) + 30},
    ],
)
def test_rejects_missing_invalid_future_expired_or_unordered_temporal_claims(claims) -> None:
    """Removing temporal validation would accept one of these forged JWTs."""
    token = _signed_claims(claims)

    with pytest.raises(InvalidJwtSubject):
        extract_subject(token, SECRET, now=NOW)


def test_accepts_integer_temporal_claims_in_the_allowed_window() -> None:
    """Rejecting a currently valid, issued JWT would break ordinary Bearer authentication."""
    actor_id = uuid4()
    token = issue_token(actor_id, SECRET, now=NOW)

    assert extract_subject(token, SECRET, now=NOW) == actor_id
