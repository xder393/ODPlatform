"""Real MinIO contracts for immutable frame objects."""

from __future__ import annotations

import os
from hashlib import sha256
from uuid import uuid4

import pytest

minio = pytest.importorskip("minio", reason="minio-py is required for the MinIO gate")

from odp_api.adapters.storage.minio import MinioObjectStorage
from odp_api.ports.storage import ObjectAlreadyExists

ENDPOINT = os.getenv("ODP_MINIO_TEST_ENDPOINT")
ACCESS_KEY = os.getenv("ODP_MINIO_TEST_ACCESS_KEY")
SECRET_KEY = os.getenv("ODP_MINIO_TEST_SECRET_KEY")

pytestmark = pytest.mark.skipif(
    not ENDPOINT or not ACCESS_KEY or not SECRET_KEY,
    reason="requires the dedicated ODP_MINIO_TEST_* service configuration",
)


@pytest.fixture
def minio_storage():
    client = minio.Minio(ENDPOINT, access_key=ACCESS_KEY, secret_key=SECRET_KEY, secure=False)
    bucket = f"odp-task4-{uuid4().hex}"
    client.make_bucket(bucket)
    storage = MinioObjectStorage(
        ENDPOINT,
        ACCESS_KEY,
        SECRET_KEY,
        bucket,
        secure=False,
        client=client,
    )
    try:
        yield storage
    finally:
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)


def test_real_minio_round_trip_and_presign(minio_storage):
    payload = b"jpeg-frame"
    digest = sha256(payload).hexdigest()

    stored = minio_storage.put("frame-1", payload, sha256=digest, content_type="image/jpeg")

    assert stored.content_length == len(payload)
    assert stored.sha256 == digest
    assert minio_storage.get("frame-1") == payload
    assert minio_storage.presign_get("frame-1", expires_seconds=60).startswith("http")


def test_real_minio_rejects_different_immutable_bytes(minio_storage):
    first = b"first-frame"
    second = b"second-frame"
    minio_storage.put("frame-1", first, sha256=sha256(first).hexdigest())

    with pytest.raises(ObjectAlreadyExists):
        minio_storage.put("frame-1", second, sha256=sha256(second).hexdigest())
