"""Property coverage for the assembled inference control plane."""

from datetime import UTC, datetime
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
from odp_api.adapters.persistence.task_control import SqlAlchemyTaskControlRepository
from odp_api.adapters.persistence.task_models import (
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
        database_path = (
            self._tmp_path_factory.mktemp("delivery-order") / "control-plane.db"
        )
        _upgrade_sqlite(database_path)
        self.engine, self.sessions = create_engine_and_session(
            f"sqlite:///{database_path}"
        )
        self.now = datetime.now(UTC)
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
        self.repository = SqlAlchemyTaskControlRepository(self.sessions)
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
            SqlAlchemyInspectionEffects(self.sessions)
        )
        self.recovery = RecoveryService(
            self.repository, SystemRecoveryScope("property-test")
        )
        self.claim = None

    def apply(self, action: str) -> None:
        if action == "deliver":
            claim = self.repository.claim(
                self.task.task_id, self.organization_id, "property-worker", self.now
            )
            if claim is not None:
                self.claim = claim
        elif action == "renew" and self.claim is not None:
            renewed = self.repository.renew(self.claim, self.now)
            if renewed is not None:
                self.claim = renewed
        elif action == "expire":
            self.recovery.run_once(self.now)
        elif action == "complete" and self.claim is not None:
            try:
                self.effects.publish(_publish_command(self.claim, self.now))
            except StaleLease:
                pass

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
                    InspectionEventRow.source_result_id
                    == PublishedInferenceResultRow.result_id,
                )
                .where(PublishedInferenceResultRow.task_id == self.task.task_id)
            )

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
        st.sampled_from(["deliver", "renew", "expire", "complete"]),
        min_size=1,
        max_size=20,
    )
)
@settings(deadline=None)
def test_any_duplicate_delivery_order_has_at_most_one_published_result(
    harness, actions
):
    harness.reset()
    for action in actions:
        harness.apply(action)

    assert harness.published_result_count() <= 1
    assert harness.inspection_event_count() <= 1


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
            {
                "defect_type": "scratch",
                "confidence": 0.91,
                "spatial_zone": "property-zone",
            },
        ),
        stage_durations=(("model", 1.0),),
        correlation_id=uuid4(),
        database_completed_at=now,
    )
