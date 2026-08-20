# apps/web-backend

FastAPI service foundation for the ODPlatform quality-inspection platform.

## Run locally

Install this package and start the app with:

```bash
uvicorn odp_api.main:create_app --factory --reload
```

The service exposes `GET /healthz`, returning `{"status": "ok"}`. Runtime
configuration is provided by `Settings` and reads `ODP_`-prefixed environment
variables.

The Web service remains separate from `apps/platform`, which continues to be
the independent visual inspection core. Shared event contracts live in
`packages/shared-schemas`.

## Observability

The service exposes `GET /metrics` in the Prometheus text exposition format
(Content-Type `text/plain; version=0.0.4`). The endpoint is intentionally
unauthenticated so Prometheus can scrape it; production deployments should
restrict it at the ingress layer to an IP allowlist for the scrape server.

| Metric | Type | Meaning |
| --- | --- | --- |
| `inspection_alert_total` | counter | Inspection alerts produced by vision inference |
| `task_dead_letter_total` | counter | Tasks moved to DEAD_LETTER after exhausting retries |
| `audit_chain_verification_failure_total` | counter | Audit hash-chain verification failures |
| `vision_inference_seconds` | histogram | Vision inference call latency |
| `vision_inference_errors_total` | counter | Vision inference calls that raised an error |
| `case_resolution_seconds` | histogram | First inspection event to RESOLVED duration |
| `task_queue_depth` | gauge | Outstanding tasks (PENDING or RETRYING) |
| `rag_advice_requests_total` / `rag_advice_hits_total` | counters | RAG advice requests and hits |

Every request is assigned a correlation ID: the middleware generates a UUID
when the `X-Correlation-ID` header is absent, echoes it on the response, and
audit entries record the value so logs and audit chains can be joined.

Metrics are implemented in pure Python without `prometheus-client`, and
tracing is limited to correlation-ID propagation. Integrating the
OpenTelemetry SDK and `prometheus-client` is a production follow-up; the
metric names, the header and the instrumented call sites stay stable so the
swap is mechanical. The Compose observability stack (Prometheus,
Alertmanager, Grafana) is documented in `deploy/README.md`.

