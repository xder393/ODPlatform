"""One-shot durable inspection-alert seed contracts."""

from pathlib import Path

from sqlalchemy import func, select

from odp_api.adapters.persistence.models import Base, InspectionAlertFeedRow
from odp_api.db import create_engine_and_session
from odp_api.seed_alerts import seed_demo_alerts


def test_alert_seed_is_idempotent_and_survives_a_new_feed(tmp_path: Path) -> None:
    """Migration seeding must not depend on an API process replaying demo events."""
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'alerts.db'}")
    Base.metadata.create_all(engine)
    try:
        seed_demo_alerts(sessions)
        seed_demo_alerts(sessions)
        with sessions() as session:
            assert (
                session.scalar(select(func.count()).select_from(InspectionAlertFeedRow))
                == 10
            )
            assert [
                row.cursor
                for row in session.scalars(
                    select(InspectionAlertFeedRow).order_by(
                        InspectionAlertFeedRow.cursor
                    )
                )
            ] == list(range(1, 11))
    finally:
        engine.dispose()
