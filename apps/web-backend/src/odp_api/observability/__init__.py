"""Cross-cutting observability for the quality-inspection API.

Metrics use a dependency-free, hand-written Prometheus text exposition format;
tracing is limited to correlation-ID generation and propagation. Both are
deliberately small so a production deployment can swap in prometheus-client and
the OpenTelemetry SDK without changing the instrumented call sites or names.
"""

from odp_api.observability.metrics import DEFAULT_REGISTRY, MetricRegistry
from odp_api.observability.tracing import get_correlation_id

__all__ = ["DEFAULT_REGISTRY", "MetricRegistry", "get_correlation_id"]
