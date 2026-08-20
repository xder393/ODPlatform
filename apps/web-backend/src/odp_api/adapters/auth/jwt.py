"""Small JWT subject parser used by authentication adapters.

Signature verification remains the responsibility of the configured identity provider;
this pure adapter only extracts the already-validated token subject.
"""

import base64
import json
from uuid import UUID


class InvalidJwtSubject(ValueError):
    pass


def extract_subject(token: str) -> UUID:
    """Extract a UUID ``sub`` claim from a compact JWT payload."""
    try:
        _header, encoded_payload, _signature = token.split(".")
        padded_payload = encoded_payload + "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded_payload))
        return UUID(payload["sub"])
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidJwtSubject("JWT must contain a UUID subject claim.") from error
