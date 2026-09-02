"""Load-bearing unit coverage for exact Redis 7 PEL-safe retention."""

from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 8, 25, 12, 0, 3, tzinfo=UTC)


def test_safe_trim_planner_orders_redis_ids_numerically_not_lexicographically():
    """Replacing numeric tuple ordering with string ordering must fail."""
    from odp_api.modules.tasks.retention import GroupProgress, SafeTrimPlanner

    groups = (
        GroupProgress("group-a", last_delivered_id="9-10", smallest_pending_id=None),
        GroupProgress("group-b", last_delivered_id="10-0", smallest_pending_id=None),
    )

    assert SafeTrimPlanner.safe_min_id(groups, "99-0") == "9-10"


def test_safe_trim_planner_uses_smallest_pending_before_delivery_progress_and_floor():
    """Ignoring either the PEL minimum or floor would cross a live boundary."""
    from odp_api.modules.tasks.retention import GroupProgress, SafeTrimPlanner

    groups = (
        GroupProgress("group-a", last_delivered_id="90-0", smallest_pending_id="40-0"),
        GroupProgress("group-b", last_delivered_id="80-0", smallest_pending_id=None),
    )

    assert SafeTrimPlanner.safe_min_id(groups, "70-0") == "40-0"
    assert SafeTrimPlanner.safe_min_id(
        (GroupProgress("group-a", "90-0", None),), "70-0"
    ) == "70-0"


def test_safe_trim_planner_rejects_pending_beyond_observed_delivery_progress():
    """A rewound group must retain history below its still-Pending delivery."""
    from odp_api.modules.tasks.retention import GroupProgress, SafeTrimPlanner

    groups = (
        GroupProgress(
            "rewound-group",
            last_delivered_id="1000-0",
            smallest_pending_id="2000-0",
        ),
    )

    assert SafeTrimPlanner.safe_min_id(groups, "2500-0") is None


@pytest.mark.parametrize(
    "groups,floor",
    [
        ((), "70-0"),
        ((None,), "70-0"),
        (("missing-last-delivered",), "70-0"),
        (("bad-pending",), "70-0"),
        (("non-string-name",), "70-0"),
        (("valid",), "bad-id"),
    ],
)
def test_safe_trim_planner_refuses_empty_incomplete_or_malformed_progress(groups, floor):
    """Optimistically trimming on incomplete Redis state must fail this test."""
    from odp_api.modules.tasks.retention import GroupProgress, SafeTrimPlanner

    fixtures = {
        None: GroupProgress("group-a", None, None),
        "missing-last-delivered": GroupProgress("group-a", None, "40-0"),
        "bad-pending": GroupProgress("group-a", "90-0", "9-x"),
        "non-string-name": GroupProgress(42, "90-0", None),
        "valid": GroupProgress("group-a", "90-0", None),
    }
    normalized = tuple(fixtures[item] for item in groups)

    assert SafeTrimPlanner.safe_min_id(normalized, floor) is None


def test_minimum_retention_policy_is_positive_configurable_and_utc_based():
    """Using local time or accepting a non-positive retention window must fail."""
    from odp_api.modules.tasks.retention import MinimumRetentionPolicy

    policy = MinimumRetentionPolicy(timedelta(milliseconds=1500))
    assert policy.floor_id(datetime(1970, 1, 1, 0, 0, 3, tzinfo=UTC)) == "1500-0"
    with pytest.raises(ValueError, match="positive"):
        MinimumRetentionPolicy(timedelta(0))
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.floor_id(datetime(1970, 1, 1, 0, 0, 3, tzinfo=UTC).replace(tzinfo=None))


class RecordingRedis:
    def __init__(self, groups, pending):
        self.groups = list(groups)
        self.pending = dict(pending)
        self.calls = []

    async def xinfo_groups(self, stream):
        self.calls.append(("XINFO", "GROUPS", stream))
        return list(self.groups)

    async def xpending(self, stream, group):
        self.calls.append(("XPENDING", stream, group))
        return self.pending[group]

    async def xgroup_destroy(self, stream, group):
        self.calls.append(("XGROUP", "DESTROY", stream, group))
        before = len(self.groups)
        self.groups = [item for item in self.groups if item["name"] != group]
        return int(len(self.groups) != before)

    async def execute_command(self, *args):
        self.calls.append(tuple(args))
        return 4


@pytest.mark.anyio
async def test_controller_reads_every_group_and_emits_only_exact_minid_equals_trim():
    """Approximate/MAXLEN trimming or skipping a group must fail this contract."""
    from odp_api.modules.tasks.retention import (
        MinimumRetentionPolicy,
        RedisRetentionAdapter,
        StreamRetentionController,
        StreamRetentionPolicy,
    )

    client = RecordingRedis(
        groups=[
            {"name": "group-a", "last-delivered-id": "90-0"},
            {"name": "group-b", "last-delivered-id": "80-0"},
        ],
        pending={
            "group-a": {
                "pending": 1,
                "min": "40-0",
                "max": "40-0",
                "consumers": [{"name": "worker-a", "pending": 1}],
            },
            "group-b": {"pending": 0, "min": None, "max": None, "consumers": []},
        },
    )
    controller = StreamRetentionController(
        RedisRetentionAdapter(client),
        StreamRetentionPolicy(
            "odp:inference:tasks", MinimumRetentionPolicy(timedelta(seconds=1))
        ),
        clock=lambda: NOW,
    )

    summary = await controller.run_once()

    assert summary.safe_min_id == "40-0"
    assert summary.groups_considered == 2
    assert summary.trimmed_entries == 4
    assert client.calls[-1] == (
        "XTRIM",
        "odp:inference:tasks",
        "MINID",
        "=",
        "40-0",
    )
    assert [call for call in client.calls if call[0] == "XPENDING"] == [
        ("XPENDING", "odp:inference:tasks", "group-a"),
        ("XPENDING", "odp:inference:tasks", "group-b"),
    ]
    assert all("MAXLEN" not in call and "~" not in call for call in client.calls)


@pytest.mark.anyio
async def test_controller_does_not_trim_when_one_group_progress_is_untrustworthy():
    """A malformed member of the all-groups safety set must stop trimming."""
    from odp_api.modules.tasks.retention import (
        MinimumRetentionPolicy,
        RedisRetentionAdapter,
        StreamRetentionController,
        StreamRetentionPolicy,
    )

    client = RecordingRedis(
        groups=[
            {"name": "good", "last-delivered-id": "90-0"},
            {"name": "bad"},
        ],
        pending={
            "good": {"pending": 0, "min": None, "max": None, "consumers": []},
            "bad": {"pending": 0, "min": None, "max": None, "consumers": []},
        },
    )
    controller = StreamRetentionController(
        RedisRetentionAdapter(client),
        StreamRetentionPolicy(
            "odp:inference:tasks", MinimumRetentionPolicy(timedelta(seconds=1))
        ),
        clock=lambda: NOW,
    )

    summary = await controller.run_once()

    assert summary.safe_min_id is None
    assert summary.trimmed_entries == 0
    assert not [call for call in client.calls if call[0] == "XTRIM"]


@pytest.mark.anyio
async def test_controller_decodes_bytes_group_state_and_pending_ids():
    """Assuming decode_responses=True must not disable a safe Redis deployment."""
    from odp_api.modules.tasks.retention import (
        MinimumRetentionPolicy,
        RedisRetentionAdapter,
        StreamRetentionController,
        StreamRetentionPolicy,
    )

    client = RecordingRedis(
        groups=[{b"name": b"group-a", b"last-delivered-id": b"90-0"}],
        pending={
            "group-a": {
                b"pending": 1,
                b"min": b"40-0",
                b"max": b"40-0",
                b"consumers": [{b"name": b"worker", b"pending": 1}],
            }
        },
    )
    controller = StreamRetentionController(
        RedisRetentionAdapter(client),
        StreamRetentionPolicy(
            "odp:inference:tasks", MinimumRetentionPolicy(timedelta(seconds=1))
        ),
        clock=lambda: NOW,
    )

    summary = await controller.run_once()

    assert (summary.safe_min_id, summary.groups_considered) == ("40-0", 1)


@pytest.mark.anyio
async def test_controller_refuses_internally_inconsistent_xpending_summary():
    """A positive Pending count without a maximum ID is not trustworthy progress."""
    from odp_api.modules.tasks.retention import (
        MinimumRetentionPolicy,
        RedisRetentionAdapter,
        StreamRetentionController,
        StreamRetentionPolicy,
    )

    client = RecordingRedis(
        groups=[{"name": "group-a", "last-delivered-id": "90-0"}],
        pending={
            "group-a": {
                "pending": 1,
                "min": "40-0",
                "max": None,
                "consumers": [],
            }
        },
    )
    controller = StreamRetentionController(
        RedisRetentionAdapter(client),
        StreamRetentionPolicy(
            "odp:inference:tasks", MinimumRetentionPolicy(timedelta(seconds=1))
        ),
        clock=lambda: NOW,
    )

    summary = await controller.run_once()

    assert summary.safe_min_id is None
    assert summary.trimmed_entries == 0
    assert not [call for call in client.calls if call[0] == "XTRIM"]


@pytest.mark.anyio
async def test_expired_allowlisted_gateway_group_is_destroyed_before_alert_progress_read():
    """Deleting unknown/live/inference groups or ordering cleanup after XINFO must fail."""
    from odp_api.modules.tasks.retention import (
        GatewayGroupExpiryRegistry,
        GatewayGroupLease,
        MinimumRetentionPolicy,
        RedisRetentionAdapter,
        StreamRetentionController,
        StreamRetentionPolicy,
    )

    expired = GatewayGroupLease(
        "odp-alert-gateway:expired", expires_at=NOW - timedelta(seconds=1)
    )
    live = GatewayGroupLease(
        "odp-alert-gateway:live", expires_at=NOW + timedelta(seconds=1)
    )
    with pytest.raises(ValueError, match="allowlist"):
        GatewayGroupLease("odp-inference-workers", expires_at=NOW - timedelta(seconds=1))
    registry = GatewayGroupExpiryRegistry((expired, live))
    client = RecordingRedis(
        groups=[
            {"name": expired.group_name, "last-delivered-id": "10-0"},
            {"name": live.group_name, "last-delivered-id": "20-0"},
            {"name": "odp-inference-workers", "last-delivered-id": "30-0"},
        ],
        pending={
            live.group_name: {"pending": 0, "min": None, "max": None, "consumers": []},
            "odp-inference-workers": {
                "pending": 0,
                "min": None,
                "max": None,
                "consumers": [],
            },
        },
    )
    controller = StreamRetentionController(
        RedisRetentionAdapter(client),
        StreamRetentionPolicy(
            "odp:inspection:alerts",
            MinimumRetentionPolicy(timedelta(seconds=1)),
            gateway_groups=registry,
        ),
        clock=lambda: NOW,
    )

    summary = await controller.run_once()

    assert summary.destroyed_groups == (expired.group_name,)
    assert client.calls[0] == (
        "XGROUP",
        "DESTROY",
        "odp:inspection:alerts",
        expired.group_name,
    )
    assert not any(live.group_name in call for call in client.calls if call[:2] == ("XGROUP", "DESTROY"))
    assert not any(
        "odp-inference-workers" in call
        for call in client.calls
        if call[:2] == ("XGROUP", "DESTROY")
    )


@pytest.mark.anyio
async def test_gateway_cleanup_adapter_refuses_non_alert_stream_and_empty_instance_name():
    """Bypassing the typed policy must not broaden destructive group cleanup."""
    from odp_api.modules.tasks.retention import RedisRetentionAdapter

    client = RecordingRedis(groups=[], pending={})
    adapter = RedisRetentionAdapter(client)

    with pytest.raises(ValueError, match="alert stream"):
        await adapter.destroy_group(
            "odp:inference:tasks", "odp-alert-gateway:expired"
        )
    with pytest.raises(ValueError, match="allowlist"):
        await adapter.destroy_group("odp:inspection:alerts", "odp-alert-gateway:")

    assert not [call for call in client.calls if call[:2] == ("XGROUP", "DESTROY")]


@pytest.mark.anyio
async def test_gateway_registry_issues_typed_expired_capabilities_only():
    """Raw names and live leases must never be usable as destructive authority."""
    from odp_api.modules.tasks.retention import (
        GatewayGroupExpiryRegistry,
        GatewayGroupLease,
        RedisRetentionAdapter,
    )

    expired = GatewayGroupLease(
        "odp-alert-gateway:expired-capability",
        expires_at=NOW - timedelta(seconds=1),
    )
    live = GatewayGroupLease(
        "odp-alert-gateway:live-capability",
        expires_at=NOW + timedelta(seconds=1),
    )
    registry = GatewayGroupExpiryRegistry((expired, live))
    capabilities = registry.expired_groups(NOW)
    assert len(capabilities) == 1
    assert capabilities[0].group_name == expired.group_name

    client = RecordingRedis(
        groups=[{"name": expired.group_name, "last-delivered-id": "10-0"}],
        pending={},
    )
    adapter = RedisRetentionAdapter(client)

    with pytest.raises(TypeError, match="expired Gateway capability"):
        await adapter.destroy_group("odp:inspection:alerts", expired.group_name)
    with pytest.raises(TypeError, match="expired Gateway capability"):
        await adapter.destroy_group("odp:inspection:alerts", live)
    assert not [call for call in client.calls if call[:2] == ("XGROUP", "DESTROY")]

    assert await adapter.destroy_group(
        "odp:inspection:alerts", capabilities[0]
    ) is True
    assert client.calls == [
        (
            "XGROUP",
            "DESTROY",
            "odp:inspection:alerts",
            expired.group_name,
        )
    ]


@pytest.mark.anyio
async def test_stream_retention_process_isolates_policy_failure_and_reports_success():
    """One unavailable stream must not suppress another stream's retention summary."""
    from odp_api.modules.tasks.retention import TrimSummary
    from odp_api.processes.stream_retention import StreamRetentionProcess

    class FailedController:
        async def run_once(self):
            raise OSError("Redis stream unavailable")

    class HealthyController:
        async def run_once(self):
            return TrimSummary("healthy", "10-0", 1, 2)

    process = StreamRetentionProcess((FailedController(), HealthyController()))

    summary = await process.run_once()

    assert summary.streams == (TrimSummary("healthy", "10-0", 1, 2),)
    assert len(summary.failures) == 1
    assert summary.failures[0].error_type == "OSError"


@pytest.mark.anyio
async def test_stream_retention_loop_stops_cooperatively_after_completed_iteration():
    """Ignoring an already-set stop signal would execute an unwanted second sweep."""
    import asyncio

    from odp_api.modules.tasks.retention import TrimSummary
    from odp_api.processes.stream_retention import StreamRetentionProcess

    stop = asyncio.Event()
    calls = []

    class StopController:
        async def run_once(self):
            calls.append("run")
            stop.set()
            return TrimSummary("stream", None, 0, 0)

    process = StreamRetentionProcess((StopController(),), interval_seconds=0.01)
    await process.run(stop)

    assert calls == ["run"]
    assert process.last_summary is not None
