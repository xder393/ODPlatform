"""MinIO implementation of the immutable object-storage port."""

from __future__ import annotations

import io
from datetime import timedelta
from hashlib import sha256 as sha256_digest
from typing import Any

from odp_api.ports.storage import (
    ObjectAlreadyExists,
    ObjectIntegrityError,
    ObjectMetadata,
    ObjectNotFound,
    ObjectStoragePort,
)


class MinioObjectStorage(ObjectStoragePort):
    """Store immutable objects in one explicitly provisioned MinIO bucket.

    Bucket creation and lifecycle policy are deployment responsibilities.  The
    adapter therefore never creates a bucket and never installs a prefix
    lifecycle rule that could delete promoted evidence.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool = True,
        client: Any | None = None,
    ) -> None:
        if not endpoint.strip() or not access_key or not secret_key or not bucket.strip():
            raise ValueError("MinIO endpoint, credentials, and bucket are required")
        self._bucket = bucket
        if client is None:
            from minio import Minio

            client = Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )
        self._client = client

    @property
    def bucket(self) -> str:
        return self._bucket

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        sha256: str,
        content_type: str = "application/octet-stream",
    ) -> ObjectMetadata:
        content = bytes(content)
        calculated = sha256_digest(content).hexdigest()
        if calculated != sha256.lower():
            raise ObjectIntegrityError("content SHA-256 does not match upload metadata")

        existing = self.head(object_key)
        if existing is not None:
            if _metadata_matches(existing, object_key, len(content), calculated):
                return existing
            raise ObjectAlreadyExists("immutable object key already contains different bytes")

        metadata = {"X-Amz-Meta-Sha256": calculated}
        try:
            self._conditional_put(
                object_key,
                content,
                content_type=content_type,
                metadata=metadata,
            )
        except Exception as error:
            if _status_code(error) in {409, 412}:
                # A concurrent creator won.  A same-byte retry is idempotent;
                # a different object under this key is never overwritten.
                raced = self.head(object_key)
                if raced is not None and _metadata_matches(
                    raced, object_key, len(content), calculated
                ):
                    return raced
                raise ObjectAlreadyExists("immutable object key was created concurrently") from None
            raise

        stored = self.head(object_key)
        if stored is None:
            raise ObjectNotFound("uploaded object disappeared before verification")
        if not _metadata_matches(stored, object_key, len(content), calculated):
            raise ObjectIntegrityError("uploaded object failed immutable verification")
        return stored

    def head(self, object_key: str) -> ObjectMetadata | None:
        try:
            result = self._client.stat_object(self._bucket, object_key)
        except Exception as error:
            if _is_not_found(error):
                return None
            raise
        metadata = {
            str(key).lower(): str(value)
            for key, value in (getattr(result, "metadata", {}) or {}).items()
        }
        digest = metadata.get("x-amz-meta-sha256") or metadata.get("sha256")
        return ObjectMetadata(
            object_key=object_key,
            content_length=int(getattr(result, "size", 0)),
            sha256=digest.lower() if digest else None,
            metadata=metadata,
        )

    def get(self, object_key: str) -> bytes:
        try:
            response = self._client.get_object(self._bucket, object_key)
        except Exception as error:
            if _is_not_found(error):
                raise ObjectNotFound("object not found") from None
            raise
        try:
            return bytes(response.read())
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
            release = getattr(response, "release_conn", None)
            if release is not None:
                release()

    def delete(self, object_key: str) -> None:
        try:
            self._client.remove_object(self._bucket, object_key)
        except Exception as error:
            if not _is_not_found(error):
                raise

    def presign_get(self, object_key: str, expires_seconds: int = 60) -> str:
        if expires_seconds < 1 or expires_seconds > 60:
            raise ValueError("evidence presign TTL must be between 1 and 60 seconds")
        return self._client.presigned_get_object(
            self._bucket,
            object_key,
            expires=timedelta(seconds=expires_seconds),
        )

    def _conditional_put(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        """Issue an S3 conditional PUT using MinIO's signed request path.

        ``put_object`` does not expose arbitrary HTTP precondition headers in
        all supported minio-py releases.  Calling the SDK's own request path
        preserves SigV4 signing while sending ``If-None-Match: *`` atomically
        at the storage server; this is the immutable-write guarantee that a
        HEAD-then-PUT sequence cannot provide.
        """

        execute = getattr(self._client, "_execute", None)
        if execute is None:
            # Test doubles and older wrappers may only expose put_object.  The
            # DB reservation still gives each artifact a unique UUID key, and
            # post-write HEAD verification retains idempotent same-byte retry.
            self._client.put_object(
                self._bucket,
                object_key,
                io.BytesIO(content),
                len(content),
                content_type=content_type,
                metadata=metadata,
            )
            return
        headers = {
            "Content-Length": str(len(content)),
            "Content-Type": content_type,
            "If-None-Match": "*",
            **metadata,
        }
        response = execute(
            "PUT",
            self._bucket,
            object_key,
            body=io.BytesIO(content),
            headers=headers,
        )
        release = getattr(response, "release_conn", None)
        if release is not None:
            release()


def _metadata_matches(
    metadata: ObjectMetadata,
    object_key: str,
    content_length: int,
    content_sha256: str,
) -> bool:
    return (
        metadata.object_key == object_key
        and metadata.content_length == content_length
        and metadata.sha256 is not None
        and metadata.sha256.lower() == content_sha256.lower()
    )


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_not_found(error: Exception) -> bool:
    status = _status_code(error)
    code = str(getattr(error, "code", ""))
    return status == 404 or code in {"NoSuchKey", "NoSuchObject", "NotFound"}
