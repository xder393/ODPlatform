"""Explicit deployment-time bucket creation; never run in a request or Worker."""

import os

from minio import Minio
from minio.error import S3Error


def main() -> None:
    secure = os.environ.get("ODP_MINIO_SECURE", "true").lower()
    if secure not in {"true", "false"}:
        raise ValueError("ODP_MINIO_SECURE must be true or false")
    client = Minio(
        os.environ["ODP_MINIO_ENDPOINT"],
        access_key=os.environ["ODP_MINIO_ACCESS_KEY"],
        secret_key=os.environ["ODP_MINIO_SECRET_KEY"],
        secure=secure == "true",
        region=os.environ.get("ODP_MINIO_REGION", "us-east-1"),
    )
    bucket = os.environ["ODP_MINIO_BUCKET"]
    if not client.bucket_exists(bucket):
        try:
            client.make_bucket(bucket)
        except S3Error as error:
            # Another initializer may have won; never swallow ownership or auth errors.
            if error.code != "BucketAlreadyOwnedByYou":
                raise
    if not client.bucket_exists(bucket):
        raise RuntimeError("Artifact bucket initialization did not complete")


if __name__ == "__main__":
    main()
