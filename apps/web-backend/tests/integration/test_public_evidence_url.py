"""Real S3 verification: host-specific signature survives a browser-side GET."""

import os
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from uuid import uuid4

import pytest

from odp_api.adapters.storage.minio import MinioObjectStorage


@pytest.mark.skipif(not os.getenv("ODP_MINIO_TEST_ENDPOINT"), reason="dedicated MinIO endpoint required")
def test_public_signature_downloads_original_bytes_and_rejects_host_rewrite():
    from minio import Minio

    endpoint = os.environ["ODP_MINIO_TEST_ENDPOINT"]
    access = os.environ["ODP_MINIO_TEST_ACCESS_KEY"]
    secret = os.environ["ODP_MINIO_TEST_SECRET_KEY"]
    client = Minio(endpoint, access_key=access, secret_key=secret, secure=False)
    bucket = f"evidence-test-{uuid4().hex}"
    payload = b"deterministic evidence bytes"
    client.make_bucket(bucket)
    try:
        client.put_object(bucket, "frame.txt", BytesIO(payload), len(payload))
        storage = MinioObjectStorage(
            "internal.invalid:9000", access, secret, bucket, client=client,
            public_endpoint=endpoint, public_secure=False,
        )
        signed = storage.presign_get("frame.txt")
        with urlopen(signed, timeout=5) as response:
            assert response.status == 200
            assert response.read() == payload
        # The dedicated local/CI service is exposed on a loopback port.
        parts = urlsplit(signed)
        other_host = "localhost" if parts.hostname == "127.0.0.1" else "127.0.0.1"
        rewritten = urlunsplit(parts._replace(netloc=f"{other_host}:{parts.port}"))
        with pytest.raises(HTTPError) as failure:
            urlopen(rewritten, timeout=5)
        assert failure.value.code == 403
    finally:
        client.remove_object(bucket, "frame.txt")
        client.remove_bucket(bucket)
