# deploy

Docker Compose platform for ODPlatform, including the observability stack
(Prometheus, Alertmanager and Grafana).

The P1B runtime is split into independent process containers: a frame
ingestor, transactional-Outbox relay, two inference workers, a recovery
scheduler, an artifact reconciler, and Redis Stream retention. PostgreSQL is
the task/evidence authority; Redis is only the distribution plane.

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
| frame-ingestor | —            | Claims `inspection_sessions` and uploads sampled frames      |
| outbox-relay  | —             | Publishes committed events to Redis Streams                  |
| inference-worker-1/2 | —       | Independent CPU inference consumers with lease renewal       |
| recovery-scheduler | —         | Single PostgreSQL-advisory-lock recovery leader              |
| artifact-reconciler | —       | Repairs pending MinIO uploads and protected cleanup          |
| stream-retention | —          | Redis 7 exact `MINID` retention with PEL safety               |
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

## Database bootstrap

`migrate` 是唯一的数据库 bootstrap 服务：它以既有卷 owner `odp` 执行
角色/RAG/Alembic 迁移和授权，再以兼容 runtime role `odp_app` 依次播种业务数据、
durable inspection alerts（`python -m odp_api.seed_alerts`）和知识库。API
使用独立的 `odp_api` 登录角色；Worker、Relay 和 Scheduler/Artifact
Reconciler 分别使用 `odp_worker`、`odp_relay` 和 `odp_scheduler`，其 SQL 授权
边界按最小权限划分。API 不会在启动时重播这些告警。旧卷升级请运行
`deploy/postgres/upgrade-existing-volume.sh`；脚本使用 migrate 的退出码，失败
即失败。Compose 仍使用 editable `pip install`，尚未 frozen；这是明确保留的
后续改进项。

P1 进程的启动只依赖 `migrate` 完成、Redis/MinIO 健康检查；运行时恢复不依赖
Compose 的 `depends_on`。每个进程从 PostgreSQL、Redis、MinIO 和（需要推理的
进程）模型 SHA 配置中做 fail-closed readiness 校验。模型 fixture 生成并提交
后，可通过 `ODP_MODEL_SHA256` 覆盖 Compose 中的占位 digest。

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
