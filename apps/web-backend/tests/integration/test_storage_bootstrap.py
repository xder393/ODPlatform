"""The deployment initializer must create a writable bucket without erasing evidence."""

import importlib.util
import io
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from minio import Minio


def test_storage_bootstrap_entrypoint_exists():
    assert importlib.util.find_spec("odp_api.storage_bootstrap") is not None


def test_bootstrap_is_repeatable_and_preserves_evidence():
    endpoint = os.getenv("ODP_MINIO_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("ODP_MINIO_TEST_ENDPOINT required")
    access = os.environ["ODP_MINIO_TEST_ACCESS_KEY"]
    secret = os.environ["ODP_MINIO_TEST_SECRET_KEY"]
    client = Minio(endpoint, access_key=access, secret_key=secret, secure=False)
    bucket = f"bootstrap-test-{uuid4().hex}"
    env = dict(os.environ, ODP_MINIO_ENDPOINT=endpoint, ODP_MINIO_ACCESS_KEY=access,
               ODP_MINIO_SECRET_KEY=secret, ODP_MINIO_BUCKET=bucket,
               ODP_MINIO_SECURE="false", ODP_MINIO_REGION="us-east-1")
    try:
        for iteration in range(2):
            result = subprocess.run([sys.executable, "-m", "odp_api.storage_bootstrap"],
                                    env=env, capture_output=True, text=True, timeout=30,
                                    check=False)
            assert result.returncode == 0, result.stderr
            assert client.bucket_exists(bucket)
            if iteration == 0:
                client.put_object(bucket, "evidence", io.BytesIO(b"retained"), 8)
        response = client.get_object(bucket, "evidence")
        try:
            assert response.read() == b"retained"
        finally:
            response.close()
            response.release_conn()
    finally:
        if client.bucket_exists(bucket):
            client.remove_object(bucket, "evidence")
            client.remove_bucket(bucket)
