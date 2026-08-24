"""Persistent Argon2id password verification adapter."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from odp_api.modules.identity.ports import PasswordCredentialPort


class Argon2PasswordVerifier:
    """Verify passwords against durable Argon2id credential hashes only."""

    def __init__(self, credentials: PasswordCredentialPort) -> None:
        self._credentials = credentials
        self._hasher = PasswordHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, actor_id, password: str) -> bool:
        password_hash = self._credentials.password_hash(actor_id)
        if password_hash is None:
            return False
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError):
            return False
