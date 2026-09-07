"""Fail-closed settings contracts for deployed P1 processes."""

import pytest
from pydantic import ValidationError

from odp_api.settings import ArtifactReconcilerSettings, WorkerSettings


def test_worker_requires_postgres_redis_minio_and_model():
    with pytest.raises(ValidationError):
        WorkerSettings(environment="docker", model_path="")


def test_recovery_budget_is_under_thirty_seconds():
    settings = WorkerSettings.valid_test_instance()
    assert (
        settings.lease_seconds
        + settings.recovery_loop_seconds
        + settings.scheduling_margin_seconds
        <= 30
    )


def test_worker_readiness_fails_closed_for_missing_dependencies():
    settings = WorkerSettings.valid_test_instance()
    assert not settings.readiness(
        database_ok=True,
        redis_ok=True,
        minio_ok=True,
        model_loaded=False,
        model_sha_verified=True,
        provider_ok=True,
    ).ready


def test_artifact_reconciler_does_not_require_a_model_release():
    settings = ArtifactReconcilerSettings(
        environment="docker",
        database_url="postgresql+psycopg://scheduler:scheduler@postgres/odp",
        redis_url="redis://redis:6379/0",
        minio_endpoint="minio:9000",
        minio_access_key="access",
        minio_secret_key="secret",
        minio_bucket="artifacts",
    )
    assert not settings.requires_model
