"""Unit contracts for the MinIO object-storage adapter."""

from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

import pytest

from odp_api.adapters.storage.minio import MinioObjectStorage
from odp_api.ports.storage import (
    ObjectAlreadyExists,
    ObjectIntegrityError,
    ObjectNotFound,
)


class FakeMinioClient:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.presign_calls = []

    def stat_object(self, bucket, key):
        del bucket
        if key not in self.objects:
            raise FakeS3Error(code="NoSuchKey", status_code=404)
        content, digest = self.objects[key]
        return SimpleNamespace(
            object_name=key,
            size=len(content),
            metadata={"X-Amz-Meta-Sha256": digest},
        )

    def put_object(self, bucket, key, data, length, *, content_type, metadata):
        del bucket
        self.put_calls.append((key, data.read(), length, content_type, metadata))
        self.objects[key] = (self.put_calls[-1][1], metadata["x-amz-meta-sha256"])
        return SimpleNamespace(object_name=key, size=length)

    def get_object(self, bucket, key):
        del bucket
        if key not in self.objects:
            raise FakeS3Error(code="NoSuchKey", status_code=404)
        return BytesIO(self.objects[key][0])

    def remove_object(self, bucket, key):
        del bucket
        self.objects.pop(key, None)

    def presigned_get_object(self, bucket, key, *, expires):
        self.presign_calls.append((bucket, key, expires))
        return "https://minio.test/presigned"


class FakeS3Error(Exception):
    def __init__(self, *, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def test_put_is_idempotent_for_same_immutable_bytes():
    client = FakeMinioClient()
    storage = MinioObjectStorage("minio.test:9000", "access", "secret", "frames", client=client)
    digest = sha256(b"bytes").hexdigest()
    client.objects["frame-1"] = (b"bytes", digest)

    metadata = storage.put("frame-1", b"bytes", sha256=digest)

    assert metadata.content_length == 5
    assert client.put_calls == []


def test_put_rejects_overwrite_with_different_digest():
    client = FakeMinioClient()
    storage = MinioObjectStorage("minio.test:9000", "access", "secret", "frames", client=client)
    client.objects["frame-1"] = (b"old", "b" * 64)

    with pytest.raises(ObjectAlreadyExists):
        storage.put("frame-1", b"new", sha256=sha256(b"new").hexdigest())


def test_head_maps_missing_object_to_none_and_get_raises_not_found():
    client = FakeMinioClient()
    storage = MinioObjectStorage("minio.test:9000", "access", "secret", "frames", client=client)

    assert storage.head("missing") is None
    with pytest.raises(ObjectNotFound):
        storage.get("missing")


def test_put_rejects_provider_metadata_mismatch():
    client = FakeMinioClient()
    storage = MinioObjectStorage("minio.test:9000", "access", "secret", "frames", client=client)
    client.put_object = lambda *args, **kwargs: client.objects.__setitem__(
        args[1], (b"bytes", "d" * 64)
    )

    with pytest.raises(ObjectIntegrityError):
        storage.put("frame-1", b"bytes", sha256="a" * 64)


def test_presign_rejects_long_expiry():
    storage = MinioObjectStorage("minio.test:9000", "access", "secret", "frames", client=FakeMinioClient())

    with pytest.raises(ValueError, match="60"):
        storage.presign_get("frame-1", expires_seconds=61)
