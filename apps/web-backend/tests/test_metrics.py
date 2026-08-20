import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

WEB_BACKEND_SRC = Path(__file__).parents[1] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[3] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.main import create_app
from odp_api.modules.audit.models import AuditCommand
from odp_api.modules.audit.service import AuditService, InMemoryAuditRepository
from odp_api.modules.audit.verify import AuditVerificationMonitor
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.service import get_current_actor
from odp_api.observability.metrics import MetricRegistry
from odp_api.observability.tracing import CorrelationIdMiddleware, get_correlation_id
from odp_api.ports.vision import FrameInput
from odp_api.settings import Settings


def make_settings(tmp_path) -> Settings:
    return Settings(task_database_path=str(tmp_path / "odp-tasks.sqlite3"))


def test_registry_renders_valid_prometheus_exposition_format() -> None:
    registry = MetricRegistry()
    registry.counter("sample_events_total", "Total sampled events.")
    registry.inc("sample_events_total", 2)
    registry.histogram("sample_latency_seconds", "Latency.", (0.1, 1.0, 5.0))
    registry.observe("sample_latency_seconds", 0.05)
    registry.observe("sample_latency_seconds", 3.0)
    rendered = registry.render()

    assert "# HELP sample_events_total Total sampled events." in rendered
    assert "# TYPE sample_events_total counter" in rendered
    assert "sample_events_total 2.0" in rendered
    assert "# TYPE sample_latency_seconds histogram" in rendered
    # Buckets are cumulative and include the +Inf overflow bucket. Bounds use
    # Go %g-style formatting, matching the official Prometheus client.
    assert 'sample_latency_seconds_bucket{le="0.1"} 1.0' in rendered
    assert 'sample_latency_seconds_bucket{le="1"} 1.0' in rendered
    assert 'sample_latency_seconds_bucket{le="5"} 2.0' in rendered
    assert 'sample_latency_seconds_bucket{le="+Inf"} 2.0' in rendered
    assert "sample_latency_seconds_sum 3.05" in rendered
    assert "sample_latency_seconds_count 2.0" in rendered


def test_inspection_event_increments_alert_counter_and_metrics_endpoint(tmp_path) -> None:
    registry = MetricRegistry()
    app = create_app(metric_registry=registry, settings=make_settings(tmp_path))

    with TestClient(app) as client:
        before = client.get("/metrics")
        assert before.status_code == 200
        assert before.headers["content-type"].startswith("text/plain")
        # The startup fixture inspection already produced one alert.
        assert "inspection_alert_total 1.0" in before.text
        assert "vision_inference_seconds_count 1.0" in before.text

        app.state.inspection_service.inspect_fixture(
            FrameInput(fixture_name="scratch-frame-002", content=b"scratch-frame-002"),
            organization_id=UUID("00000000-0000-0000-0000-000000000001"),
            camera_id=UUID("00000000-0000-0000-0000-000000000002"),
        )

        after = client.get("/metrics")
        assert "inspection_alert_total 2.0" in after.text
        assert "vision_inference_seconds_count 2.0" in after.text


def test_metrics_endpoint_exposes_task_dead_letter_total(tmp_path) -> None:
    registry = MetricRegistry()
    app = create_app(metric_registry=registry, settings=make_settings(tmp_path))
    task_service = app.state.task_service

    async def fail_inference(payload: dict[str, object]) -> None:
        raise RuntimeError("inference exploded")

    with TestClient(app) as client:
        record = task_service.enqueue(
            "vision_inference", "frame-001", {"frame": "frame-001"}
        )
        assert "task_queue_depth 1.0" in client.get("/metrics").text

        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
        for attempt in range(3):
            record = asyncio.run(
                task_service.process_vision(record.task_id, fail_inference, now=now)
            )
            now = now + timedelta(seconds=2 ** attempt)

        assert task_service.get(record.task_id).status == "DEAD_LETTER"
        body = client.get("/metrics").text
        assert "task_dead_letter_total 1.0" in body
        assert "task_queue_depth 0.0" in body


def test_audit_chain_verification_failure_increments_counter() -> None:
    repository = InMemoryAuditRepository()
    service = AuditService(repository)
    organization_id = uuid4()
    service.append(
        AuditCommand(
            organization_id=organization_id,
            resource_type="defect_case",
            resource_id=uuid4(),
            action="defect_case.transition",
            change_summary="{}",
            actor_id=uuid4(),
            occurred_at=datetime.now(UTC),
            correlation_id=None,
            request_ip=None,
        )
    )
    entry = repository.entries_for_organization(organization_id)[0]
    repository.unsafe_replace_change_summary_for_test(entry.audit_id, '{"tampered": true}')

    registry = MetricRegistry()
    monitor = AuditVerificationMonitor(service, metric_registry=registry)
    monitor.startup_sample_verify()

    assert "audit_chain_verification_failure_total 1.0" in registry.render()


def test_case_resolution_observes_resolution_duration(tmp_path) -> None:
    registry = MetricRegistry()
    app = create_app(metric_registry=registry, settings=make_settings(tmp_path))
    app.dependency_overrides[get_current_actor] = lambda: Actor(
        UUID("00000000-0000-0000-0000-000000000003"),
        UUID("00000000-0000-0000-0000-000000000001"),
        Role.ADMINISTRATOR,
        frozenset(),
    )

    with TestClient(app) as client:
        case = client.get("/api/v1/cases").json()[0]
        reviewed = client.post(
            f"/api/v1/cases/{case['case_id']}/transitions", json={"status": "IN_REVIEW"}
        )
        assert reviewed.status_code == 200
        resolved = client.post(
            f"/api/v1/cases/{case['case_id']}/transitions", json={"status": "RESOLVED"}
        )
        assert resolved.status_code == 200
        assert "case_resolution_seconds_count 1.0" in client.get("/metrics").text


def test_correlation_id_middleware_generates_and_echoes(tmp_path) -> None:
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        generated = client.get("/healthz")
        assert generated.status_code == 200
        generated_id = generated.headers.get("X-Correlation-ID")
        assert generated_id is not None
        assert UUID(generated_id)  # valid UUIDv4 string

        supplied_id = "abcdef12-3456-7890-abcd-ef1234567890"
        echoed = client.get("/healthz", headers={"X-Correlation-ID": supplied_id})
        assert echoed.headers["X-Correlation-ID"] == supplied_id


def test_get_correlation_id_reads_the_assigned_request_id() -> None:
    async def correlation_aware_app(scope, receive, send) -> None:
        response = PlainTextResponse(get_correlation_id() or "")
        await response(scope, receive, send)

    client = TestClient(CorrelationIdMiddleware(correlation_aware_app))

    response = client.get("/")
    assert response.status_code == 200
    assert response.text == response.headers["X-Correlation-ID"]
    assert UUID(response.text)
