import json
import os
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

BACKEND_SRC = Path(__file__).parents[2] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[4] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(SHARED_SCHEMAS_SRC), str(BACKEND_SRC)]

from odp_schemas.events import EventEnvelope, InspectionAlertCreated

from odp_api.adapters.events.redis_streams import (
    EVENT_DESTINATIONS,
    RedisOutboxPublisher,
)
from odp_api.adapters.persistence.models import Base
from odp_api.adapters.persistence.outbox import SqlAlchemyOutboxRepository
from odp_api.adapters.persistence.task_models import OutboxEventRow
from odp_api.db import create_engine_and_session
from odp_api.ports.events import ClaimedOutboxEvent, UnknownEventType
from odp_api.processes.outbox_relay import OutboxRelay

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


class InjectedCommitFailure(RuntimeError):
    pass


@dataclass
class StoredEvent:
    event: ClaimedOutboxEvent
    available_at: datetime
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    publish_attempts: int = 0
    published_at: datetime | None = None
    last_error: str | None = None


class FakeOutboxRepository:
    def __init__(self, claim_lease: timedelta = timedelta(seconds=1)):
        self.events: dict[UUID, StoredEvent] = {}
        self.claim_lease = claim_lease
        self.fail_next_mark = False
        self.claim_calls: list[tuple[int, datetime, str, timedelta]] = []

    def insert_ready(
        self, now: datetime = NOW, *, event_type: str = "vision.inference.requested.v1"
    ):
        outbox_id, organization_id, task_id = uuid4(), uuid4(), uuid4()
        event = ClaimedOutboxEvent(
            outbox_id=outbox_id,
            organization_id=organization_id,
            aggregate_type="inference_task",
            aggregate_id=task_id,
            task_id=task_id,
            dispatch_seq=2,
            event_type=event_type,
            schema_version=1,
            payload={"task_id": str(task_id), "dispatch_seq": 2},
            occurred_at=now,
        )
        self.events[outbox_id] = StoredEvent(event, now)
        return event

    def claim_ready(self, limit, now, claim_owner, claim_lease):
        self.claim_calls.append((limit, now, claim_owner, claim_lease))
        claimed = []
        for stored in sorted(
            self.events.values(), key=lambda item: str(item.event.outbox_id)
        ):
            if len(claimed) >= limit:
                break
            if stored.published_at is not None or stored.available_at > now:
                continue
            if stored.claim_expires_at is not None and stored.claim_expires_at > now:
                continue
            stored.claim_owner = claim_owner
            stored.claim_expires_at = now + claim_lease
            claimed.append(stored.event)
        return claimed

    def mark_published(self, outbox_id, organization_id, claim_owner, published_at):
        stored = self.events[outbox_id]
        if self.fail_next_mark:
            self.fail_next_mark = False
            raise InjectedCommitFailure("mark commit failed")
        if (
            stored.event.organization_id != organization_id
            or stored.claim_owner != claim_owner
            or stored.published_at is not None
        ):
            return False
        stored.published_at = published_at
        stored.claim_owner = stored.claim_expires_at = None
        return True

    def mark_publish_failed(
        self, outbox_id, organization_id, claim_owner, failed_at, error, retry_delay
    ):
        stored = self.events[outbox_id]
        if (
            stored.event.organization_id != organization_id
            or stored.claim_owner != claim_owner
            or stored.published_at is not None
        ):
            return False
        stored.publish_attempts += 1
        stored.available_at = failed_at + retry_delay
        stored.last_error = error
        stored.claim_owner = stored.claim_expires_at = None
        return True


class RecordingPublisher:
    def __init__(self):
        self.envelopes = []

    def publish(self, envelope):
        if envelope.event_type not in EVENT_DESTINATIONS:
            raise UnknownEventType(envelope.event_type)
        self.envelopes.append(envelope)
        return f"{len(self.envelopes)}-0"


class OutagePublisher:
    def publish(self, envelope):
        raise OSError("Redis unavailable")


class RecordingRedis:
    def __init__(self):
        self.calls = []

    def xadd(self, stream, fields):
        self.calls.append((stream, fields))
        return "1-0"


class _EmptyResult:
    def all(self):
        return []


class _CapturingSession:
    def __init__(self, statements):
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement):
        self._statements.append(statement)
        return _EmptyResult()

    def commit(self):
        pass

    def rollback(self):
        pass


def test_postgresql_claim_statement_locks_only_outbox_rows():
    statements = []
    repository = SqlAlchemyOutboxRepository(
        lambda: _CapturingSession(statements), clock=lambda _session: NOW
    )

    repository.claim_ready(1, NOW, "relay-test", timedelta(seconds=1))

    compiled = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF outbox_events SKIP LOCKED" in compiled


def test_relay_republishes_when_redis_succeeded_before_db_mark():
    repository = FakeOutboxRepository()
    publisher = RecordingPublisher()
    relay = OutboxRelay(
        repository,
        publisher,
        relay_id="relay-test",
        claim_lease=timedelta(seconds=1),
    )
    event = repository.insert_ready()
    repository.fail_next_mark = True

    with pytest.raises(InjectedCommitFailure):
        relay.run_batch(10, NOW)

    assert len(publisher.envelopes) == 1
    relay.run_batch(10, NOW + timedelta(seconds=1))
    assert [envelope.event_id for envelope in publisher.envelopes] == [
        event.outbox_id,
        event.outbox_id,
    ]


def test_relay_backoff_keeps_outbox_row_after_redis_outage():
    repository = FakeOutboxRepository()
    event = repository.insert_ready()
    relay = OutboxRelay(
        repository,
        OutagePublisher(),
        relay_id="relay-test",
        claim_lease=timedelta(seconds=1),
        backoff_base=timedelta(seconds=2),
        backoff_cap=timedelta(seconds=10),
    )

    first = relay.run_batch(10, NOW)
    stored = repository.events[event.outbox_id]

    assert first.claimed == 1
    assert first.failed == 1
    assert stored.publish_attempts == 1
    assert stored.available_at == NOW + timedelta(seconds=2)
    assert stored.published_at is None
    assert relay.run_batch(10, NOW + timedelta(seconds=1)).claimed == 0


def test_relay_keeps_oversized_envelope_durable_and_backed_off():
    repository = FakeOutboxRepository()
    event = repository.insert_ready(event_type="test.event.v1")
    repository.events[event.outbox_id].event = replace(
        event, payload={"body": "x" * (64 * 1024)}
    )
    redis = RecordingRedis()
    publisher = RedisOutboxPublisher(redis, {"test.event.v1": "odp:test:events"})
    relay = OutboxRelay(
        repository,
        publisher,
        relay_id="relay-test",
        backoff_base=timedelta(seconds=2),
        backoff_cap=timedelta(seconds=10),
    )

    result = relay.run_batch(10, NOW)
    stored = repository.events[event.outbox_id]

    assert result.failed == 1
    assert redis.calls == []
    assert stored.published_at is None
    assert stored.publish_attempts == 1
    assert stored.available_at == NOW + timedelta(seconds=2)


def test_relay_claim_is_bounded_by_configured_batch_size():
    repository = FakeOutboxRepository()
    for _ in range(3):
        repository.insert_ready()
    relay = OutboxRelay(
        repository, RecordingPublisher(), relay_id="relay-test", batch_size=2
    )

    result = relay.run_batch(10, NOW)

    assert result.claimed == 2
    assert repository.claim_calls[0][0] == 2


def test_relay_never_discards_unknown_event_rows():
    repository = FakeOutboxRepository()
    event = repository.insert_ready(event_type="unknown.event.v1")
    relay = OutboxRelay(repository, RecordingPublisher(), relay_id="relay-test")

    result = relay.run_batch(10, NOW)
    stored = repository.events[event.outbox_id]

    assert result.failed == 1
    assert stored.published_at is None
    assert stored.publish_attempts == 1
    assert stored.last_error and "unknown" in stored.last_error


def test_relay_envelope_does_not_reuse_inference_payload_object_key():
    repository = FakeOutboxRepository()
    event = repository.insert_ready()
    repository.events[event.outbox_id].event = replace(
        event,
        payload={
            "task_id": str(event.task_id),
            "dispatch_seq": event.dispatch_seq,
            "object_key": "secret",
        },
    )
    publisher = RecordingPublisher()
    relay = OutboxRelay(repository, publisher, relay_id="relay-test")

    relay.run_batch(10, NOW)

    body = json.loads(publisher.envelopes[0].canonical_json())
    assert body["payload"] == {
        "task_id": str(event.task_id),
        "dispatch_seq": event.dispatch_seq,
    }
    assert "object_key" not in publisher.envelopes[0].canonical_json()


def test_relay_preserves_only_the_p1a_alert_payload_contract():
    repository = FakeOutboxRepository()
    event = repository.insert_ready(event_type="inspection.alert.created.v1")
    alert = InspectionAlertCreated(
        alert_id=event.aggregate_id,
        organization_id=event.organization_id,
        case_id=uuid4(),
        event_id=uuid4(),
        camera_id=uuid4(),
        line_id=uuid4(),
        defect_type="scratch",
        severity="HIGH",
        confidence=0.91,
        occurred_at=NOW,
        business_cursor="42",
    )
    repository.events[event.outbox_id].event = replace(
        event,
        payload={**alert.model_dump(mode="json"), "object_key": "private/frame.jpg"},
    )
    publisher = RecordingPublisher()
    relay = OutboxRelay(repository, publisher, relay_id="relay-test")

    relay.run_batch(10, NOW)

    body = json.loads(publisher.envelopes[0].canonical_json())
    assert body["payload"] == alert.model_dump(mode="json")
    assert "object_key" not in publisher.envelopes[0].canonical_json()


def test_sqlalchemy_repository_claims_and_guarded_marks_are_durable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    outbox_id, organization_id, aggregate_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            OutboxEventRow(
                outbox_id=outbox_id,
                organization_id=organization_id,
                aggregate_type="inference_task",
                aggregate_id=aggregate_id,
                task_id=None,
                dispatch_seq=None,
                event_type="inspection.alert.created.v1",
                schema_version=1,
                payload={"alert_id": str(aggregate_id)},
                available_at=NOW,
                publish_attempts=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    repository = SqlAlchemyOutboxRepository(sessions, clock=lambda _session: NOW)
    claimed = repository.claim_ready(10, NOW, "relay-a", timedelta(seconds=1))
    assert [event.outbox_id for event in claimed] == [outbox_id]
    assert repository.mark_published(outbox_id, organization_id, "relay-a", NOW)
    assert not repository.mark_published(outbox_id, organization_id, "relay-a", NOW)
    with sessions() as session:
        row = session.get(OutboxEventRow, outbox_id)
        assert row.published_at.replace(tzinfo=UTC) == NOW
        assert row.claim_owner is None


def test_sqlalchemy_repository_claim_expiry_allows_reclaim(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'expiry.db'}")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    outbox_id, organization_id, aggregate_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            OutboxEventRow(
                outbox_id=outbox_id,
                organization_id=organization_id,
                aggregate_type="inference_task",
                aggregate_id=aggregate_id,
                event_type="inspection.alert.created.v1",
                schema_version=1,
                payload={"alert_id": str(aggregate_id)},
                available_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    clock = [NOW]
    repository = SqlAlchemyOutboxRepository(sessions, clock=lambda _session: clock[0])
    first = repository.claim_ready(10, NOW, "relay-a", timedelta(seconds=1))
    assert len(first) == 1
    clock[0] = NOW + timedelta(seconds=0.5)
    assert (
        repository.claim_ready(
            10, NOW + timedelta(seconds=0.5), "relay-b", timedelta(seconds=1)
        )
        == ()
    )
    clock[0] = NOW + timedelta(seconds=1)
    reclaimed = repository.claim_ready(
        10, NOW + timedelta(seconds=1), "relay-b", timedelta(seconds=1)
    )
    assert [event.outbox_id for event in reclaimed] == [outbox_id]


def test_sqlalchemy_relay_does_not_publish_inference_without_authoritative_task(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing-task.db'}")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    outbox_id, organization_id, aggregate_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            OutboxEventRow(
                outbox_id=outbox_id,
                organization_id=organization_id,
                aggregate_type="inference_task",
                aggregate_id=aggregate_id,
                task_id=aggregate_id,
                dispatch_seq=1,
                event_type="vision.inference.requested.v1",
                schema_version=1,
                payload={
                    "task_id": str(aggregate_id),
                    "dispatch_seq": 1,
                    "object_key": "private/frame.jpg",
                },
                available_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    publisher = RecordingPublisher()
    repository = SqlAlchemyOutboxRepository(sessions, clock=lambda _session: NOW)
    result = OutboxRelay(repository, publisher, relay_id="relay-test").run_batch(
        10, NOW
    )

    assert result.failed == 1
    assert publisher.envelopes == []
    with sessions() as session:
        assert session.get(OutboxEventRow, outbox_id).published_at is None


@pytest.mark.skipif(
    not os.getenv("ODP_POSTGRES_TEST_URL"),
    reason="requires the dedicated ODP_POSTGRES_TEST_URL CI database",
)
def test_postgresql_competing_relays_claim_each_outbox_once():
    """Two system relays may race, but SKIP LOCKED must partition the rows."""
    # The full PostgreSQL migration/concurrency gate is exercised by the P1A
    # persistence suite; this focused test keeps its own rows tenant-scoped.
    database_url = os.environ["ODP_POSTGRES_TEST_URL"]
    engine, sessions = create_engine_and_session(database_url)
    try:
        Base.metadata.create_all(engine)
        organization_id = uuid4()
        with sessions.begin() as session:
            for _ in range(4):
                aggregate_id = uuid4()
                session.add(
                    OutboxEventRow(
                        outbox_id=uuid4(),
                        organization_id=organization_id,
                        aggregate_type="inference_task",
                        aggregate_id=aggregate_id,
                        event_type="inspection.alert.created.v1",
                        schema_version=1,
                        payload={"alert_id": str(aggregate_id)},
                        available_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
        first = SqlAlchemyOutboxRepository(sessions, clock=lambda _session: NOW)
        second = SqlAlchemyOutboxRepository(sessions, clock=lambda _session: NOW)
        from concurrent.futures import ThreadPoolExecutor

        def claim(repo, owner):
            return repo.claim_ready(2, NOW, owner, timedelta(seconds=30))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda args: claim(*args), ((first, "a"), (second, "b")))
            )
        ids = [event.outbox_id for result in results for event in result]
        assert len(ids) == len(set(ids)) == 4
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.getenv("ODP_REDIS_TEST_URL"),
    reason="requires the dedicated ODP_REDIS_TEST_URL CI Redis",
)
def test_real_redis_publisher_keeps_stream_history_untrimmed():
    """The relay publisher must leave PEL-safe retention to another process."""
    from redis import Redis

    stream = f"odp:test:outbox-relay:{uuid4()}"
    event = EventEnvelope(
        event_id=uuid4(),
        event_type="vision.inference.requested.v1",
        schema_version=1,
        occurred_at=NOW,
        correlation_id=uuid4(),
        organization_id=uuid4(),
        aggregate_id=uuid4(),
        payload={"task_id": str(uuid4()), "dispatch_seq": 1},
    )
    client = Redis.from_url(os.environ["ODP_REDIS_TEST_URL"], decode_responses=True)
    publisher = RedisOutboxPublisher(client, {event.event_type: stream})
    try:
        publisher.publish(event)
        publisher.publish(event)
        assert client.xlen(stream) == 2
        assert client.xrange(stream)[0][1]["envelope"] == event.canonical_json()
    finally:
        client.delete(stream)
        client.close()
