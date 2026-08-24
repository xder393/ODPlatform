# deploy

Docker Compose platform for ODPlatform, including the observability stack
(Prometheus, Alertmanager and Grafana).

## Start and stop

```bash
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml down
```

The API container installs the bind-mounted source as editable packages, so it
does not use `uv sync --frozen` yet. Backend CI validates
`apps/web-backend/uv.lock` and installs its hashed frozen export instead.
Making the Compose path equally frozen is tracked as a follow-up because it
needs an image-level `uv` installation and a container validation pass.

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

## Web frontend (nginx)

The compose stack also serves the built Web frontend through an nginx
entrypoint:

- **nginx / frontend**: port `8080` — serves the static build of
  `apps/web-frontend/` and proxies `/api` and `/ws` to the `api` service,
  so the browser only ever talks to one origin
  (`E2E_BASE_URL=http://localhost:8080` is the base URL used by the
  Playwright suite and CI).

## Enterprise quality inspection demo script

One-shot demo of the full quality inspection loop (login, live alert, cited
AI advice, case handling, audit/metrics):

1. `docker compose -f deploy/compose.yaml up -d --build`
2. Wait for health: `curl -fsS http://localhost:8080/healthz`
   → `{"status":"ok"}` (retry until it succeeds)
3. Open http://localhost:8080 and log in as the inspector demo account
   (`inspector@example.test` / `odp-inspector-dev`; the leader and admin
   accounts are listed in the repository root README)
4. The live alert area shows a "疑似表面划痕" (suspected surface scratch)
   card from the simulated camera feed
5. Select the "待确认" case in the case list — the AI advice panel appears
   with a "可信度高" (high confidence) badge and citations including the
   document version and page numbers
6. Optional high-risk operation demo: click "模拟暂停产线" (simulate line
   pause), re-enter the password when the 5-minute reauthentication prompt
   appears
7. Click "确认复检" (confirm review) — the case timeline shows
   "待确认 → 复核中"
8. Click "完成处置" (complete handling) — the case becomes "已处置"
   (resolved) and the manual transition is appended to the audit hash chain
9. Open Grafana at http://localhost:3000 (`admin` / `admin`) — the
   "Quality Inspection" dashboard shows alert rate, inference latency,
   case resolution duration and RAG advice hit rate
10. Open Prometheus at http://localhost:9090 and query
    `odp_inspection_alerts_total` / `odp_audit_verification_failures_total`;
    stop the stack with `docker compose -f deploy/compose.yaml down`
