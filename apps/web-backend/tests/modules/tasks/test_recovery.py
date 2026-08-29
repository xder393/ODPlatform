"""Unit coverage for the scheduler orchestration boundary."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


def test_recovery_service_reports_each_scheduler_transition():
    """Removing any recovery operation must make its summary count wrong."""
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope

    repo = SimpleNamespace(
        release_due_retries=lambda now, scope: 2,
        redispatch_stale_ready=lambda now, scope: 3,
        expire_leases=lambda now, scope: 1,
    )
    summary = RecoveryService(repo, SystemRecoveryScope("scheduler")).run_once(datetime.now(UTC))
    assert (summary.retries_released, summary.stale_ready_redispatched, summary.leases_expired) == (
        2,
        3,
        1,
    )


def test_recovery_service_requires_a_typed_system_scope():
    from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope

    repo = SimpleNamespace(
        release_due_retries=lambda now, scope: 0,
        redispatch_stale_ready=lambda now, scope: 0,
        expire_leases=lambda now, scope: 0,
    )
    with pytest.raises(TypeError):
        RecoveryService(repo, object())
    assert (
        RecoveryService(repo, SystemRecoveryScope("scheduler"))
        .run_once(datetime.now(UTC))
        .retries_released
        == 0
    )
