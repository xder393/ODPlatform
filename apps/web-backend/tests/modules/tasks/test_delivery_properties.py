"""Property coverage for the assembled inference control plane."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from alembic import command
from odp_api.adapters.persistence.inspection_effects import SqlAlchemyInspectionEffects
from odp_api.adapters.persistence.models import InspectionEventRow
from odp_api.adapters.persistence.task_control import (
    LEASE_SECONDS,
    RENEW_INTERVAL_SECONDS,
    SqlAlchemyTaskControlRepository,
)
from odp_api.adapters.persistence.task_models import (
    CameraInferenceStateRow,
    InferenceTaskRow,
    InspectionSessionRow,
    PublishedInferenceResultRow,
)
from odp_api.db import create_engine_and_session
from odp_api.modules.inspection.effects import InspectionEffectService
from odp_api.modules.tasks.commands import (
    InferenceExecutionContract,
    PublishInferenceCommand,
)
from odp_api.modules.tasks.recovery import RecoveryService, SystemRecoveryScope
from odp_api.ports.tasks import AdmissionRequest, StaleLease


class DeliveryHarness:
    """Drive generated delivery orders through the production persistence ports."""

    def __init__(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        self._tmp_path_factory = tmp_path_factory

    def reset(self) -> None:
        if hasattr(self, "engine"):
            self.engine.dispose()
        database_path = self._tmp_path_factory.mktemp("delivery-order") / "control-plane.db"
        _upgrade_sqlite(database_path)
        self.engine, self.sessions = create_engine_and_session(f"sqlite:///{database_path}")
        self.now = datetime.now(UTC)
        self.clock = self.now
        self.organization_id = uuid4()
        self.camera_id = uuid4()
        self.stream_session_id = uuid4()
        with self.sessions.begin() as session:
            session.add(
                InspectionSessionRow(
                    session_id=self.stream_session_id,
                    organization_id=self.organization_id,
                    camera_id=self.camera_id,
                    line_id=uuid4(),
                    source_type="rtsp",
                    sanitized_uri="rtsp://camera.example.test/stream",
                    status="ACTIVE",
                    idempotency_key="property-session",
                    started_at=self.now,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        self.repository = SqlAlchemyTaskControlRepository(
            self.sessions, clock=lambda _session: self.clock
        )
        reservation = self.repository.reserve(
            AdmissionRequest(
                organization_id=self.organization_id,
                camera_id=self.camera_id,
                stream_session_id=self.stream_session_id,
                frame_sequence=1,
                captured_at=self.now,
                content_sha256="a" * 64,
                correlation_id=uuid4(),
            ),
            self.now,
        )
        self.task = self.repository.complete_upload(
            reservation.reservation_id,
            self.organization_id,
            "frames/property.jpg",
            1,
            self.now,
        )
        self.effects = InspectionEffectService(
            SqlAlchemyInspectionEffects(self.sessions, clock=lambda _session: self.clock)
        )
        self.recovery = RecoveryService(self.repository, SystemRecoveryScope("property-test"))
        self.claim = None
        self.claims = []
        self._stale_publish_rejected = False

    def prepare_expired_reclaim_baseline(self) -> None:
        initial_claim = self.repository.claim(
            self.task.task_id, self.organization_id, "property-worker-a", self.clock
        )
        assert initial_claim is not None
        self.clock += timedelta(seconds=RENEW_INTERVAL_SECONDS)
        renewed_claim = self.repository.renew(initial_claim, self.clock)
        assert renewed_claim is not None
        self.clock += timedelta(seconds=LEASE_SECONDS + 1)
        try:
            self.effects.publish(_publish_command(renewed_claim, self.clock))
        except StaleLease:
            self._stale_publish_rejected = True
        else:
            raise AssertionError("expired lease must reject stale completion")
        expired = self.recovery.run_once(self.clock)
        assert expired.leases_expired == 1
        released = self.recovery.run_once(self.clock)
        assert released.retries_released == 1
        replacement_claim = self.repository.claim(
            self.task.task_id, self.organization_id, "property-worker-b", self.clock
        )
        assert replacement_claim is not None
        try:
            self.effects.publish(_publish_command(renewed_claim, self.clock))
        except StaleLease:
            pass
        else:
            raise AssertionError("reclaimed lease must reject stale completion")
        self.claim = replacement_claim
        self.claims.extend((renewed_claim, replacement_claim))
        self.effects.publish(_publish_command(replacement_claim, self.clock))

    def apply(self, action: str) -> None:
        if action == "deliver":
            claim = self.repository.claim(
                self.task.task_id, self.organization_id, "property-worker", self.clock
            )
            if claim is not None:
                self.claim = claim
                self.claims.append(claim)
        elif action == "renew" and self.claim is not None:
            renewed = self.repository.renew(self.claim, self.clock)
            if renewed is not None:
                self.claim = renewed
                self.claims.append(renewed)
        elif action == "advance_clock":
            self.clock += timedelta(seconds=LEASE_SECONDS + 1)
        elif action == "recover":
            self.recovery.run_once(self.clock)
        elif action == "complete" and self.claims:
            claim = self.claims.pop(0)
            try:
                self.effects.publish(_publish_command(claim, self.clock))
            except StaleLease:
                self._stale_publish_rejected = True

    def assert_invariants(self) -> None:
        with self.sessions() as session:
            task = session.get(InferenceTaskRow, self.task.task_id)
            state = session.get(
                CameraInferenceStateRow, (self.organization_id, self.camera_id)
            )
            actual_ready = session.scalar(
                select(func.count())
                .select_from(InferenceTaskRow)
                .where(
                    InferenceTaskRow.organization_id == self.organization_id,
                    InferenceTaskRow.camera_id == self.camera_id,
                    InferenceTaskRow.status == "READY",
                )
            )
            running = session.scalar(
                select(func.count())
                .select_from(InferenceTaskRow)
                .where(
                    InferenceTaskRow.organization_id == self.organization_id,
                    InferenceTaskRow.camera_id == self.camera_id,
                    InferenceTaskRow.status == "RUNNING",
                )
            )
            assert state.ready_count == actual_ready
            assert 0 <= actual_ready <= 2
            assert running <= 1
            assert (state.running_task_id == task.task_id) is (running == 1)
        assert self.published_result_count() <= 1
        assert self.inspection_event_count() <= 1

    def published_result_count(self) -> int:
        with self.sessions() as session:
            return session.scalar(
                select(func.count())
                .select_from(PublishedInferenceResultRow)
                .where(PublishedInferenceResultRow.task_id == self.task.task_id)
            )

    def inspection_event_count(self) -> int:
        with self.sessions() as session:
            return session.scalar(
                select(func.count())
                .select_from(InspectionEventRow)
                .join(
                    PublishedInferenceResultRow,
                    InspectionEventRow.source_result_id == PublishedInferenceResultRow.result_id,
                )
                .where(PublishedInferenceResultRow.task_id == self.task.task_id)
            )

    def stale_publish_was_rejected(self) -> bool:
        return self._stale_publish_rejected

    def close(self) -> None:
        self.engine.dispose()


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory):
    value = DeliveryHarness(tmp_path_factory)
    try:
        yield value
    finally:
        if hasattr(value, "engine"):
            value.close()


@given(
    actions=st.lists(
        st.sampled_from(["deliver", "renew", "advance_clock", "recover", "complete"]),
        min_size=1,
        max_size=20,
    )
)
@settings(deadline=None)
def test_any_duplicate_delivery_order_has_at_most_one_published_result(harness, actions):
    harness.reset()
    harness.assert_invariants()
    for action in actions:
        harness.apply(action)
        harness.assert_invariants()

    assert harness.published_result_count() <= 1
    assert harness.inspection_event_count() <= 1


def test_expired_claim_cannot_publish_after_reclaim_and_duplicate_delivery(harness):
    harness.reset()
    harness.prepare_expired_reclaim_baseline()
    harness.apply("complete")

    assert harness.published_result_count() == 1
    assert harness.inspection_event_count() == 1
    assert harness.stale_publish_was_rejected()


def test_finalize_uses_controlled_database_time_before_recovery(harness):
    harness.reset()
    claim = harness.repository.claim(
        harness.task.task_id, harness.organization_id, "property-worker-a", harness.clock
    )
    assert claim is not None
    harness.clock += timedelta(seconds=LEASE_SECONDS + 1)

    with pytest.raises(StaleLease, match="stale lease"):
        harness.effects.publish(_publish_command(claim, harness.clock))

    assert harness.published_result_count() == 0
    assert harness.inspection_event_count() == 0


def _upgrade_sqlite(database_path: Path) -> None:
    backend_root = Path(__file__).parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")


def _publish_command(claim, now: datetime) -> PublishInferenceCommand:
    return PublishInferenceCommand(
        claim=claim,
        execution_contract=InferenceExecutionContract(
            "property-model",
            "b" * 64,
            "1",
            "cpu",
            (1, 3, 32, 32),
            "pre",
            "post",
            0.5,
            0.5,
            "hard",
            False,
            "classes",
        ),
        frame_sha256="a" * 64,
        detections=(
            {"defect_type": "scratch", "confidence": 0.91, "spatial_zone": "property-zone"},
        ),
        stage_durations=(("model", 1.0),),
        correlation_id=uuid4(),
        database_completed_at=now,
    )
