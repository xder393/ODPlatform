"""Typed boundary for immutable inference-frame object storage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """Metadata returned by a storage ``HEAD`` operation.

    ``sha256`` is deliberately a first-class value rather than an ETag.  An
    ETag is not a content digest for multipart uploads and cannot be used to
    verify an inference frame safely.
    """

    object_key: str
    content_length: int
    sha256: str | None
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def size(self) -> int:
        """Compatibility spelling used by object-storage SDKs."""

        return self.content_length


class ObjectStorageError(RuntimeError):
    """Base class for storage failures that must not leak provider details."""


class ObjectNotFound(ObjectStorageError):
    """The requested immutable object does not exist."""


class ObjectAlreadyExists(ObjectStorageError):
    """A key exists with bytes different from the attempted upload."""


class ObjectIntegrityError(ObjectStorageError):
    """Provider metadata or bytes failed the caller's integrity contract."""


class ObjectStoragePort(Protocol):
    """Synchronous object operations used from worker threads/processes.

    Implementations must treat a key as immutable.  A repeated ``put`` with
    the same key is only successful when length and SHA-256 metadata match;
    replacing bytes under an existing key is prohibited.
    """

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        sha256: str,
        content_type: str = "application/octet-stream",
    ) -> ObjectMetadata: ...

    def head(self, object_key: str) -> ObjectMetadata | None: ...

    def get(self, object_key: str) -> bytes: ...

    def delete(self, object_key: str) -> None: ...

    def presign_get(self, object_key: str, expires_seconds: int = 60) -> str: ...
