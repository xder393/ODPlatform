"""Unit coverage for the scheduler orchestration boundary."""

from datetime import UTC, datetime
from types import SimpleNamespace


def test_recovery_service_reports_each_scheduler_transition():
    """Removing any recovery operation must make its summary count wrong."""
    from odp_api.modules.tasks.recovery import RecoveryService

    repo = SimpleNamespace(
        release_due_retries=lambda now: 2,
        redispatch_stale_ready=lambda now, organization_id=None: 3,
        expire_leases=lambda now, organization_id=None: 1,
    )
    summary = RecoveryService(repo).run_once(datetime.now(UTC))
    assert (summary.retries_released, summary.stale_ready_redispatched, summary.leases_expired) == (
        2,
        3,
        1,
    )
