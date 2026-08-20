"""HTTP exposure for the observability package."""

from fastapi import APIRouter, Response

from odp_api.observability.metrics import MetricRegistry

PROMETHEUS_TEXT_CONTENT_TYPE = "text/plain; version=0.0.4"


def create_metrics_router(registry: MetricRegistry) -> APIRouter:
    """Expose ``GET /metrics`` in Prometheus text exposition format."""
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        # Intentionally unauthenticated so Prometheus can scrape it without
        # credentials. Production deployments must restrict this route at the
        # ingress layer to an IP allowlist for the scrape server instead.
        return Response(content=registry.render(), media_type=PROMETHEUS_TEXT_CONTENT_TYPE)

    return router
