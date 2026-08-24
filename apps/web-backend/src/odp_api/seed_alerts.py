"""One-shot, idempotent durable alert seeding for the migration service."""

from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.notifications.sqlite_feed import SqliteInspectionAlertFeed
from odp_api.db import create_engine_and_session
from odp_api.seed import build_demo_seed
from odp_api.settings import Settings


def seed_demo_alerts(session_factory: sessionmaker[Session]) -> None:
    """Persist every deterministic demo inspection event exactly once."""
    feed = SqliteInspectionAlertFeed(session_factory)
    for case in build_demo_seed().cases:
        for event in case.inspection_events:
            feed.publish(event.to_alert(), event.line_id)


def main() -> None:
    """Use the runtime business DSN and fail closed on connection/write errors."""
    engine, sessions = create_engine_and_session(Settings().database_url)
    try:
        seed_demo_alerts(sessions)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
