"""Thread-safe in-process metrics rendered in the Prometheus text format.

No third-party client is used on purpose: the repository's test and demo
environments run without Docker or external services, so the registry must
stay pure Python and dependency-free. The exposition format follows the
Prometheus text format version 0.0.4 and can be scraped directly from
``GET /metrics``.
"""

import math
import threading
from typing import Final

VISION_INFERENCE_SECONDS_BUCKETS: Final = (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
CASE_RESOLUTION_SECONDS_BUCKETS: Final = (60.0, 300.0, 900.0, 1800.0, 3600.0, 14400.0, 86400.0)
_AUTO_HISTOGRAM_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# The standard metric set documented in the plan; every registry, injected or
# default, resolves these names to the same kind, help text and buckets.
_STANDARD_COUNTERS: Final = {
    "inspection_alert_total": "Total number of inspection alerts produced by vision inference.",
    "task_dead_letter_total": (
        "Total number of tasks moved to DEAD_LETTER after exhausting their retry budget."
    ),
    "audit_chain_verification_failure_total": (
        "Total number of audit hash-chain verification failures."
    ),
    "vision_inference_errors_total": "Total number of vision inference calls that raised an error.",
    "rag_advice_requests_total": "Total number of AI advice requests served.",
    "rag_advice_hits_total": (
        "Total number of AI advice requests answered with retrievable evidence."
    ),
    "websocket_reconnect_total": "Total inspection WebSocket connections.",
    "websocket_reconciled_events_total": "Total alert events sent during reconciliation.",
}
_STANDARD_GAUGES: Final = {
    "task_queue_depth": "Current number of outstanding tasks (PENDING or RETRYING).",
    "websocket_active_connections": "Current active inspection WebSocket connections.",
}
_STANDARD_HISTOGRAMS: Final = {
    "vision_inference_seconds": (
        "Time spent inside a single vision inference call.",
        VISION_INFERENCE_SECONDS_BUCKETS,
    ),
    "case_resolution_seconds": (
        "Time from the first inspection event to the case reaching RESOLVED.",
        CASE_RESOLUTION_SECONDS_BUCKETS,
    ),
}


class Counter:
    """Monotonic counter rendered as a Prometheus counter metric."""

    def __init__(self, name: str, help: str) -> None:
        self.name = name
        self.help = help
        self._value = 0.0
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0) -> None:
        with self._lock:
            self._value += value

    def snapshot(self) -> float:
        with self._lock:
            return self._value

    def exposition_lines(self) -> tuple[str, ...]:
        return (
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} counter",
            f"{self.name} {_format_sample(self.snapshot())}",
        )


class Gauge:
    """Settable gauge rendered as a Prometheus gauge metric."""

    def __init__(self, name: str, help: str) -> None:
        self.name = name
        self.help = help
        self._value = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = float(value)

    def add(self, value: float) -> None:
        with self._lock:
            self._value += float(value)

    def snapshot(self) -> float:
        with self._lock:
            return self._value

    def exposition_lines(self) -> tuple[str, ...]:
        return (
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} gauge",
            f"{self.name} {_format_sample(self.snapshot())}",
        )


class Histogram:
    """Bucketed observation histogram rendered in Prometheus histogram format."""

    def __init__(self, name: str, help: str, buckets: tuple[float, ...]) -> None:
        if not buckets:
            raise ValueError("A histogram requires at least one bucket bound.")
        self.name = name
        self.help = help
        self.buckets = tuple(sorted(buckets))
        self._count = 0
        self._sum = 0.0
        self._cumulative_counts = [0] * len(self.buckets)
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._count += 1
            self._sum += value
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    self._cumulative_counts[index] += 1

    def snapshot(self) -> tuple[int, float, tuple[int, ...]]:
        with self._lock:
            return self._count, self._sum, tuple(self._cumulative_counts)

    def exposition_lines(self) -> tuple[str, ...]:
        count, total, cumulative = self.snapshot()
        lines = [
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} histogram",
        ]
        for bound, bucket_count in zip(self.buckets, cumulative, strict=True):
            lines.append(
                f'{self.name}_bucket{{le="{_format_bucket_bound(bound)}"}} '
                f"{_format_sample(bucket_count)}"
            )
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {_format_sample(count)}')
        lines.append(f"{self.name}_sum {_format_sample(total)}")
        lines.append(f"{self.name}_count {_format_sample(count)}")
        return tuple(lines)


class MetricRegistry:
    """Named collection of counters, gauges and histograms, safe for concurrent use."""

    def __init__(self) -> None:
        self._metrics: dict[str, Counter | Gauge | Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help: str) -> Counter:
        """Register and return a counter, or the existing metric with that name."""
        return self._register(name, Counter(name, help))

    def gauge(self, name: str, help: str) -> Gauge:
        """Register and return a gauge, or the existing metric with that name."""
        return self._register(name, Gauge(name, help))

    def histogram(self, name: str, help: str, buckets: tuple[float, ...]) -> Histogram:
        """Register and return a histogram, or the existing metric with that name."""
        return self._register(name, Histogram(name, help, buckets))

    def inc(self, name: str, value: float = 1.0) -> None:
        """Increment the named counter, registering it on first use."""
        metric = self._get_or_create(name, Counter)
        if not isinstance(metric, Counter):
            raise TypeError(f"Metric {name} is a {type(metric).__name__}, not a counter.")
        metric.inc(value)

    def set(self, name: str, value: float) -> None:
        """Set the named gauge, registering it on first use."""
        metric = self._get_or_create(name, Gauge)
        if not isinstance(metric, Gauge):
            raise TypeError(f"Metric {name} is a {type(metric).__name__}, not a gauge.")
        metric.set(value)

    def add(self, name: str, value: float) -> None:
        metric = self._get_or_create(name, Gauge)
        if not isinstance(metric, Gauge):
            raise TypeError(f"Metric {name} is a {type(metric).__name__}, not a gauge.")
        metric.add(value)

    def observe(self, name: str, value: float) -> None:
        """Record an observation in the named histogram, registering it on first use."""
        metric = self._get_or_create(name, Histogram)
        if not isinstance(metric, Histogram):
            raise TypeError(f"Metric {name} is a {type(metric).__name__}, not a histogram.")
        metric.observe(value)

    def render(self) -> str:
        """Return the full Prometheus text exposition for every registered metric."""
        with self._lock:
            metrics = tuple(self._metrics.values())
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.exposition_lines())
        return "\n".join(lines) + "\n"

    def _register(self, name: str, metric: Counter | Gauge | Histogram):
        with self._lock:
            existing = self._metrics.get(name)
            if existing is None:
                self._metrics[name] = metric
                return metric
            if type(existing) is not type(metric):
                raise ValueError(
                    f"Metric {name} is already registered as {type(existing).__name__}."
                )
            return existing

    def _get_or_create(self, name: str, metric_type):
        with self._lock:
            existing = self._metrics.get(name)
            if existing is None:
                existing = _standard_or_auto_metric(name, metric_type)
                self._metrics[name] = existing
            return existing


class RegistryTaskMetrics:
    """TaskMetricsPort adapter mirroring task queue state into a registry."""

    def __init__(self, registry: MetricRegistry | None = None) -> None:
        self._registry = registry or DEFAULT_REGISTRY

    def set_queue_depth(self, depth: int) -> None:
        self._registry.set("task_queue_depth", depth)

    def increment_dead_letter(self) -> None:
        self._registry.inc("task_dead_letter_total")


def _standard_or_auto_metric(name: str, metric_type):
    """Build a standard metric when the name is known, a generic one otherwise."""
    if metric_type is Counter and name in _STANDARD_COUNTERS:
        return Counter(name, _STANDARD_COUNTERS[name])
    if metric_type is Gauge and name in _STANDARD_GAUGES:
        return Gauge(name, _STANDARD_GAUGES[name])
    if metric_type is Histogram and name in _STANDARD_HISTOGRAMS:
        help_text, buckets = _STANDARD_HISTOGRAMS[name]
        return Histogram(name, help_text, buckets)
    if metric_type is Histogram:
        return Histogram(name, f"Histogram {name} (auto-registered).", _AUTO_HISTOGRAM_BUCKETS)
    return metric_type(name, f"{metric_type.__name__} {name} (auto-registered).")


def _escape_help(help_text: str) -> str:
    return help_text.replace("\\", "\\\\").replace("\n", "\\n")


def _format_sample(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value)}.0"
    return repr(float(value))


def _format_bucket_bound(bound: float) -> str:
    if math.isinf(bound):
        return "+Inf"
    return f"{bound:g}"


def register_standard_metrics(registry: MetricRegistry) -> None:
    """Ensure the documented standard metric set exists on the given registry."""
    for name, help_text in _STANDARD_COUNTERS.items():
        registry.counter(name, help_text)
    for name, help_text in _STANDARD_GAUGES.items():
        registry.gauge(name, help_text)
    for name, (help_text, buckets) in _STANDARD_HISTOGRAMS.items():
        registry.histogram(name, help_text, buckets)


DEFAULT_REGISTRY = MetricRegistry()
register_standard_metrics(DEFAULT_REGISTRY)
