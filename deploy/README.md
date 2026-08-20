# deploy

Docker Compose platform for ODPlatform, including the observability stack
(Prometheus, Alertmanager and Grafana).

## Start and stop

```bash
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml down
```

## Services and ports

| Service       | Port          | Notes                                                        |
| ------------- | ------------- | ------------------------------------------------------------ |
| api           | 8000          | FastAPI; exposes `GET /metrics` and `GET /healthz`           |
| postgres      | 5432          | database `odp`, user/password `odp`                          |
| redis         | 6379          | task queue and alert streams                                 |
| minio         | 9000 / 9001   | object storage; console `minioadmin` / `minioadmin`          |
| prometheus    | 9090          | scrapes `api:8000/metrics` every 15s and evaluates rules     |
| alertmanager  | 9093          | demo webhook receiver (see below)                            |
| grafana       | 3000          | default credentials `admin` / `admin`                        |

## Observability

- **Prometheus UI**: http://localhost:9090 — targets, metric browser and
  alert rules (`deploy/observability/alerts.yml`). The rules alert on task
  queue depth > 1,000, task dead letters > 0, audit chain verification
  failures > 0 and a vision inference error rate above 10%.
- **Alertmanager UI**: http://localhost:9093 — routing state. The demo
  receiver posts to `http://api:8000/healthz`, which is a no-op endpoint.
  Replace `deploy/observability/alertmanager.yml` with a real receiver
  (Slack, PagerDuty, Opsgenie, on-call webhook) for production.
- **Grafana UI**: http://localhost:3000 — the "Quality Inspection" dashboard
  is provisioned automatically from
  `deploy/observability/grafana/dashboards/quality-inspection.json` and uses
  the Prometheus data source by default. It covers inspection alert rate,
  inference latency (p50/p95), task queue depth, case resolution duration,
  RAG advice hit rate, API/WebSocket health, dead letters and audit
  verification failures.

Configuration files are mounted read-only from
`deploy/observability/`; edit them on the host and restart the affected
service (`docker compose -f deploy/compose.yaml restart prometheus`).
