# deploy

Docker Compose platform for ODPlatform, including the observability stack
(Prometheus, Alertmanager and Grafana).

The P1B runtime is split into independent process containers: a frame
ingestor, transactional-Outbox relay, two inference workers, a recovery
scheduler, an artifact reconciler, and Redis Stream retention. PostgreSQL is
the task/evidence authority; Redis is only the distribution plane.

Workers renew model-scoped presence in Redis every 5 seconds with a 15-second
TTL, after the model has loaded and the consumer group has started. Admission
pauses when no matching worker presence remains and resumes on a fresh pulse
(subject to the existing backlog and frame-age checks). A stopping worker does
not delete shared presence, so another worker's renewal remains valid. This is
a process-liveness signal, not a guarantee that inference or downstream storage
is healthy. Database leases and fencing still decide task ownership.

## Start and stop

```bash
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml down
```

All Python services share the image built by `deploy/Dockerfile.backend`.
Its build checks `apps/web-backend/uv.lock` offline, exports the frozen runtime
dependencies, and installs only hash-verified wheels into `/opt/venv`. Python
and the build-only uv image are pinned by multi-platform digest. Startup and
restart never run pip/uv or download packages. The lockfile is not regenerated.

Application/shared-schema source, SQL grants, migrations and the synthetic model
are copied into the image at their existing repository paths. The runtime uses
an explicit `PYTHONPATH`, avoiding an unlocked packaging build backend. Rebuild
after source changes; the default stack no longer bind-mounts the whole checkout.
Only runtime dependencies are installed, not pytest/ONNX training/dev tools.

Use `ODP_BACKEND_IMAGE=odp-backend:YOUR_RELEASE` to give all services the same
release tag. Build once with `docker compose -f deploy/compose.yaml build api`,
then start a prebuilt stack with `up -d --no-build --pull never` (the web image
must also exist). This freezes Python/runtime inputs, not every dependency in
the entire platform: external service images and example credentials remain
development defaults. It does not claim bit-for-bit OCI archive reproducibility.

### Recorded inputs and upgrades

The `recordings` named volume is mounted at `/workspace/recordings`: writable
by the ingestor, read-only by the API. Keep recorded input and recovery-probe
state there so recreating an application container does not erase them. Copy a
sample and use the container path in the UI:

```bash
docker compose -f deploy/compose.yaml cp ./sample.avi frame-ingestor:/workspace/recordings/sample.avi
```

`down --volumes` deletes this volume, so reserve that command for disposable data.

Before upgrading an existing installation, stop old application processes and
copy/remount any recorded sources previously reached through the checkout bind
mount at their original container paths, or stop/recreate those sessions with a
new source path. Do not remove the original source files. Explicit read-only
media/model mounts remain supported through a Compose override; avoid mounting
over `/opt/venv` or the image's source tree. Then build the image, run `migrate`,
and start all processes from the same release. Retain the old image and data
until acceptance is complete; image rollback does not undo schema migrations.

Offline image acceptance (requires a built image and Docker):

```bash
ODP_DOCKER_RUNTIME_TEST=1 uvx --from pytest==9.1.1 pytest -q \
  apps/web-backend/tests/integration/test_frozen_runtime_image.py
```

This starts a network-disabled container without host-source mounts, imports
the actual runtime, loads the ONNX fixture, encodes a frame, migrates SQLite,
and starts the actual API command through a health check. A separate negative
build verifies that a stale lockfile is rejected before installing dependencies.
The gate also validates the shared-schema package's Python/dependency constraints
against the actual image. Missing or incompatible dependencies fail CI; introducing
schema extras or direct URLs requires explicit lock integration rather than a
partial dependency check.
Full PostgreSQL/Redis/MinIO and browser acceptance remain separate gates.

### Frozen runtime acceptance — 2026-09-11

Verified locally on Linux ARM64 containers (Python 3.12.14) from base
`63be03fad88a754befd33aaf253d350068ae353d` plus this change. The backend lock
was unchanged: SHA-256
`532a0ca3771f5d4e7e8bed478809e0d987d008201e8f5554d2db7698f434ab94`.

- RED before the image change: the actual network-disabled bare Python
  container failed with `ModuleNotFoundError: cv2` (1 failed, 1 passed).
- GREEN: 22 focused runtime/process tests; 580 backend tests with every
  PostgreSQL, Redis, MinIO and Docker opt-in gate enabled (0 skipped).
- Frontend: 43 unit tests, TypeScript/Vite production build, and all 4 browser
  E2E tests with a required recorded source. The real evidence image opened.
- A fresh, isolated 17-service Compose project started with `PIP_NO_INDEX=1`
  and `UV_OFFLINE=1` on every Python service. Migration/storage bootstrap exited
  0. All 10 Python containers used image
  `sha256:a650b139ef0ea40902b282717b20c6136277de54408ac8d4cb403f94f037bac5`.
- The independent recorded pipeline verified linked result/case/alert/audit and
  evidence bytes. After SIGKILL **and container recreation**, the recovery probe
  observed generation 1 → 2, a higher committed sequence, and `STOPPED` held
  past one lease. Both recording and probe-state files survived in the volume.
- Two browser-resolved case histories were byte-equivalent after an API
  restart (canonical JSON SHA-256 before/after:
  `199e1e81ac0ec71e1965223676a5a9ea4210ce554416e4bf356fa9de345bf89b`).
- The stale-lock negative test verifies failure at lock validation, rather than
  accepting an arbitrary Docker/network failure. Ruff and `git diff --check`
  passed.

Known observations: the existing Starlette/httpx deprecation warning, ONNX's
virtual CPU vendor warning, and Playwright's color-environment warning were
non-fatal. No package upgrade was included. This is local acceptance, not a
claim that this branch's new GitHub CI or native AMD64 execution has run; those
remain required PR gates. The fixture does not establish model accuracy,
capacity, or production readiness of the wider Compose stack.

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

Role regression gate: in an explicitly disposable PostgreSQL instance with a
database named `odp`, set `ODP_ROLE_GATE_ADMIN_URL` and run
`pytest -q tests/integration/test_p1_role_gate.py` from the backend directory.
This gate applies the bootstrap and grant scripts (including development role
passwords), so it must not target a shared or production instance. It verifies
required privileges, audit/credential restrictions, and real API/Relay/Scheduler
queries through their non-owner logins. It creates tables from ORM metadata;
full Compose migration and process validation remain separate gates.

`migrate` 是唯一的数据库 bootstrap 服务：它以既有卷 owner `odp` 执行
角色/RAG/Alembic 迁移和授权，再以兼容 runtime role `odp_app` 依次播种业务数据、
durable inspection alerts（`python -m odp_api.seed_alerts`）和知识库。API
使用独立的 `odp_api` 登录角色；Worker、Relay 和 Scheduler/Artifact
Reconciler 分别使用 `odp_worker`、`odp_relay` 和 `odp_scheduler`，其 SQL 授权
边界按最小权限划分。API 不会在启动时重播这些告警。旧卷升级请运行
`deploy/postgres/upgrade-existing-volume.sh`；脚本使用 migrate 的退出码，失败
即失败。迁移与业务进程使用同一个预构建、锁定依赖的后端镜像。

P1 进程的启动只依赖 `migrate` 完成、Redis/MinIO 健康检查；运行时恢复不依赖
Compose 的 `depends_on`。每个进程从 PostgreSQL、Redis、MinIO 和（需要推理的
进程）模型 SHA 配置中做 fail-closed readiness 校验。仓库内已提交固定的
`tests/fixtures/models/tiny-detector.onnx`，其 SHA-256 已写入 Compose 默认值；
`scripts/build_fixture_onnx.py` 使用官方 ONNX helper 和 checker 生成并校验模型，
重建需要安装后端 dev 依赖。该模型固定输出划痕，仅用于验证管道，不代表检测准确率。
生产部署必须替换模型文件并通过 `ODP_MODEL_SHA256` 配置已审批模型的摘要。

## Web frontend (nginx)

The compose stack also serves the built Web frontend through an nginx
entrypoint:

- **nginx / frontend**: port `8080` — serves the static build of
  `apps/web-frontend/` and proxies `/api` and `/ws` to the `api` service,
  so the browser only ever talks to one origin
  (`E2E_BASE_URL=http://localhost:8080` is the base URL used by the
  Playwright suite and CI).

## Enterprise quality inspection demo script

### Recorded-video integration gate

With dedicated `ODP_POSTGRES_TEST_URL`, `ODP_REDIS_TEST_URL`, and
`ODP_MINIO_TEST_ENDPOINT` / `ODP_MINIO_TEST_ACCESS_KEY` /
`ODP_MINIO_TEST_SECRET_KEY` configured, run from `apps/web-backend`:

```sh
PYTHONPATH=src:../../packages/shared-schemas/src .venv/bin/pytest -q tests/integration/test_recorded_video_pipeline.py
```

The test generates a small AVI, decodes and samples it with OpenCV, uploads
immutable evidence to MinIO, publishes the PostgreSQL Outbox through Redis,
runs the actual ONNX Runtime model, and verifies durable result/case/alert/audit
rows. Duplicate delivery must remain harmless, and evidence hashes must match
after reopening the database pool. Each run owns a random schema, bucket, and
stream names and cleans those resources. The model emits a fixed detection;
this gate does not measure accuracy or exercise Compose role grants, process
supervision, or browser delivery. PostgreSQL test credentials need permission
to create and drop the isolated test schema.

### Browser-visible evidence links

The API uses `ODP_MINIO_ENDPOINT` for internal storage traffic and
`ODP_MINIO_PUBLIC_ENDPOINT` (host plus optional port, without scheme or path)
for evidence signing. Compose defaults to `localhost:9000` for local browsers.
For remote access, set the public endpoint to the hostname reachable by the
browser and set `ODP_MINIO_PUBLIC_SECURE=true` when it serves HTTPS.
The API's internal TLS setting is separate (`ODP_MINIO_SECURE`).

Signatures include the public Host header. Do not rewrite a signed URL's host
or path; a proxy must preserve both. Configure `ODP_MINIO_REGION` to the bucket's
region (Compose: `us-east-1`) so signing does not attempt network discovery
through the public address. Existing tenant/line authorization still runs
before signing, and links expire after 60 seconds.

Browser-only operations regression (controlled HTTP, not the video pipeline):

```sh
cd apps/web-frontend
E2E_BASE_URL=http://localhost:8080 npm run test:e2e -- operations.spec.ts
```

Use `PLAYWRIGHT_CHANNEL=chrome` to select installed Chrome locally. CI defaults
to Playwright's managed Chromium. The test covers supervisor session start/stop,
inspector read-only controls, dead-letter replay, a separate evidence tab, and
390-pixel viewport overflow.

### Demo workflow

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
# P1 cold-start verification (2026-09-08)

## Worker backlog scheduling follow-up

Historical timeout session `6b08e9c6-2b87-4a74-8768-7b0ea844cfc7` showed Outbox
publication in approximately 0.2–0.8 seconds, but the first 58 tasks were evicted
by latest-frame-wins before execution. The Worker imposed its two-second idle
wait after every batch, even when processing queued messages. A controlled
three-batch regression failed before changing the loop to drain nonempty batches
immediately (with a cooperative yield); an empty-queue regression verifies that
backoff and responsive shutdown remain intact. Leases, ACK and fencing are unchanged.

After the fix, two real browser video/RAG workflows passed in 12.6s and 11.5s.
Database timings for sessions `99bdf4d4-e12d-4979-a37f-31655ec43a80` and
`d93f0aa8-4ed3-4025-b49e-ddf0425b4d3e`: first claim after session creation
2.129s/2.648s; mean task-created-to-claim latency 0.365s/0.508s (19/18 attempts).
Backend regression: 442 passed, 58 skipped. This confirms removal of an artificial
backlog throughput limit; it does not prove every historical timeout had that sole
cause, nor establish full cold-start/load SLO compliance. That historical run
still installed dependencies at container startup; the frozen-image change
above removes that step. Browser polling latency remains a separate concern.

## Browser acceptance follow-up

Verified with local Chrome, Vite on `127.0.0.1:18080`, and the real Compose API,
PostgreSQL, Redis, MinIO, ingestor, relay and ONNX Worker. All four Playwright tests
passed (13.7s); the two role/UI-contract tests mock HTTP, while the quality workflow
and new recorded-pipeline test use the real API. Frontend unit tests: 43 passed;
production build and E2E typecheck passed.

For local runs, the recorded-pipeline test is opt-in: set `E2E_RECORDED_SOURCE` to an existing
video path **inside the ingestor container**. In this run it was a synthetic MJPG
AVI at `/tmp/odp-browser-e2e.avi`. Run only against disposable demo data:

```sh
cd apps/web-frontend
E2E_BASE_URL=http://127.0.0.1:18080 \
E2E_RECORDED_SOURCE=/tmp/odp-browser-e2e.avi PLAYWRIGHT_CHANNEL=chrome \
npm run test:e2e -- --workers=1
```

In GitHub CI this test is mandatory. The E2E job waits for the ingestor's
dependencies, runs the independent Compose pipeline probe (including Worker
presence and durable effects), then creates and decodes a synthetic AVI inside
the ingestor container. It passes that path to Playwright and runs one browser
worker to keep the two real case workflows from interfering. Missing or blank
`E2E_RECORDED_SOURCE` in CI fails configuration loading instead of skipping the
test; `npm run test:e2e-gate` checks this behavior without a browser. An invalid
path, unavailable runtime, or failed inference fails acceptance. Compose logs,
failure screenshots and retained traces are uploaded for seven days. These
artifacts may contain demo evidence and must not be generated against production.
The fixed-output model still validates integration, not detection accuracy.

Coverage: supervisor login, UI session creation, successful independent inference,
browser loading of signed evidence images, stop request, and newly created case
review/resolution without page reload. Live alerts now trigger a case-list refresh;
a regression test failed before this fix and passes after it. The seeded RAG test
uses a unique event ID and explicitly selects an unhandled mock-model seed case,
avoiding interference from real inference cases in a reused database. A fresh
database is still required once those seeded pending cases have been consumed.

Limits: Nginx image build was blocked by Docker Hub token-request timeout, so
this is not Nginx-container acceptance. Live video display is still a placeholder.
Real inference cases created without product scope remain unable to retrieve
scoped advice; see the product-scope implementation below. Fixed-output ONNX verifies
the application chain, not visual-model accuracy. No production-readiness claim.

### Product scope and real-case RAG (2026-09-08)

Migration `0013_session_product_scope` adds nullable session `product_category`.
The supervisor/administrator form accepts a category matching knowledge metadata.
The API trims and bounds it (1–255 characters when supplied), includes it in
idempotency comparison, and persists it on the session. The Worker reads this
database-owned value into new cases; a changed category rotates the defect episode
to a new case instead of relabeling historical cases. Missing categories remain
NULL and continue to fail closed in RAG. Organization and line filters are unchanged.
This is operator-supplied classification, not a new product master-data catalog.

Evidence: API/effects product regressions failed before implementation; the frontend
product-entry test failed before the control existed. Backend 440 passed, 58 skipped;
frontend 43 passed, build/typecheck and changed-file Ruff passed. Real PostgreSQL
migrated to 0013. The recorded-pipeline browser test now supplies the product category
and verifies HIGH-confidence advice plus a versioned citation on the newly inferred
case before review/resolution (1 passed, 12.4s). The first cold run timed out waiting
for a successful task; a repeat after services settled passed. Startup latency and
load behavior are not accepted by this test. Older cases were not backfilled.

Compose now runs `storage-init` after MinIO becomes healthy. The API and P1
processes wait for this one-shot initializer to succeed. It creates the configured
artifact bucket if absent and never clears existing objects or changes bucket
access policy. Credentials in Compose are development-only; production bucket
provisioning must use deployment credentials, not request-time privileges.

Verified in an isolated Compose project: SQL/Alembic migrations and demo seeds
exit successfully; the six selected background processes start independently and
the ONNX Worker publishes its Redis presence key. A missing Stream on cold start
now skips retention for that iteration without trimming; wrong-type and other
Redis errors still surface. Real MinIO tests cover repeated initialization and
preservation of existing evidence; real Redis tests cover cold start and PEL safety.

The startup check above is **not** browser acceptance. Separate-process
video-to-case acceptance is recorded below. This development Compose file
does not persist MinIO `/data` across container replacement; it is not a production
deployment manifest.

## Separate-process video acceptance (2026-09-08)

In a disposable Compose project with the background services running, execute:

```sh
docker compose -p YOUR_TEST_PROJECT -f deploy/compose.yaml exec -T \
  -e ODP_ALLOW_COMPOSE_PROBE=disposable frame-ingestor \
  python /workspace/apps/web-backend/scripts/verify_compose_pipeline.py
```

The probe generates a temporary AVI inside the ingestor container, creates a
uniquely scoped database session, and only observes the independently running
ingestor, relay and ONNX Worker. It verifies a successful result, linked detection,
case, alert and audit record, MinIO evidence SHA-256, model SHA-256, and the matching
alert envelope published to Redis. It requests session stop and waits for STOPPED.
Recorded input intentionally loops, so this does not assert exactly one task.
Database/evidence facts remain for inspection; do not run against production.

Three consecutive runs passed after fixing concurrent OpenCV read/release during
session cancellation. Two deterministic regressions demonstrated premature release
before the fix and pass with native operations serialized by a thread lock.
Full backend regression: 438 passed, 58 skipped (external-service gated tests were
not enabled in that full run). This verifies infrastructure with a fixed-output
synthetic model, not model accuracy, browser workflow, crash recovery, or load SLOs.
A native read that never returns can still delay close; bounded RTSP I/O remains
an explicit follow-up rather than releasing a handle underneath an active read.

## Ingestor crash-recovery acceptance

The disposable crash drill exercises a real process boundary. It creates one
recorded session through the authenticated API, waits for a durable
artifact/task/result chain, kills only `frame-ingestor`, starts a fresh
ingestor instance, and verifies takeover under a higher ingestion generation
and a higher committed frame sequence. It then requests stop through the API
and requires the database row to remain `STOPPED` for longer than one
ingestion lease window. The probe is an observer: it never calls an ingestor or
worker loop directly.

Run it only in a disposable Compose project. The explicit opt-in is required,
and the state file contains only generated organization/session UUIDs, the
first generation, and the highest committed sequence; it never contains API
credentials or tokens.

```sh
docker compose -p YOUR_RECOVERY_PROJECT -f deploy/compose.yaml up -d --build
docker compose -p YOUR_RECOVERY_PROJECT -f deploy/compose.yaml exec -T \
  -e ODP_ALLOW_COMPOSE_PROBE=disposable \
  -e ODP_PROBE_WORKSPACE=/workspace/recordings frame-ingestor \
  python /workspace/apps/web-backend/scripts/verify_ingestor_recovery.py \
  prepare --state /workspace/recordings/ingestion-recovery-state.json
docker compose -p YOUR_RECOVERY_PROJECT -f deploy/compose.yaml kill -s SIGKILL frame-ingestor
docker compose -p YOUR_RECOVERY_PROJECT -f deploy/compose.yaml up -d frame-ingestor
# Verify imports from the prebuilt image before observing lease recovery.
docker compose -p YOUR_RECOVERY_PROJECT -f deploy/compose.yaml exec -T \
  -e ODP_ALLOW_COMPOSE_PROBE=disposable \
  -e ODP_PROBE_WORKSPACE=/workspace/recordings frame-ingestor \
  python /workspace/apps/web-backend/scripts/verify_ingestor_recovery.py \
  verify --state /workspace/recordings/ingestion-recovery-state.json
docker compose -p YOUR_RECOVERY_PROJECT -f deploy/compose.yaml down --volumes --remove-orphans
```

The recorded source is intentionally replayable: a new claim resumes from the
persisted `last_reserved_sequence`, so a restarted source must produce a
higher sequence and a newly linked result. This is at-least-once recovery
evidence, not an exactly-once assertion; duplicate delivery remains harmless
through the task/result idempotency and fencing contracts. The video path must
be visible inside the `frame-ingestor` container (use the recordings volume or
an explicit media mount). A local camera device or host-only path is not
portable across process containers and is not covered by this drill.

`STOPPED` is a durable session lifecycle outcome: it means the stop request
was accepted and no new claim can reopen that session. It is not a claim that
an operating-system camera or OpenCV/RTSP handle was physically closed at the
same instant. Native OpenCV reads and releases are serialized; cancellation
may leave a `to_thread` native operation running until it returns. For that
reason a forced handle release underneath an active read is deliberately not
used. A native read that never returns can still defer close, and bounded RTSP
I/O remains a separate operational follow-up.

The GitHub E2E job runs this drill before browser tests with a unique
job-scoped Compose project, a separate bounded runtime-import check after
the kill/restart, and always-on logs/cleanup for that project. No successful
GitHub crash-drill or browser result is recorded in this document yet; local
or CI claims must be added only after the corresponding external run and
diagnostics exist.

## Failed-upload cleanup semantics

The artifact reconciler treats `INGESTION_LEASE_LOST`,
`STORAGE_UPLOAD_FAILED`, and `ADMISSION_RESERVATION_EXPIRED` as the narrow
failed `PROCESSING` upload class eligible for object cleanup. It never applies
this path to `AVAILABLE` business evidence: evidence still requires its normal
retention deadline and a fresh reference check before deletion.

Each failed-processing row carries a durable `cleanup_next_attempt_at`. A
successful storage delete changes the row to `DELETED` but retains it as a
tombstone and schedules another bounded scan; a delete-provider failure leaves
the row failed and persists the same positive retry pacing. The reconciler
selects only due candidates in bounded, artifact-id-tiebroken batches, so one
failing delete or an older tombstone cannot permanently hide later eligible
objects. A tombstone records that the last deletion attempt completed; it is
not proof that an uncancellable old provider PUT has quiesced. If that PUT
recreates the immutable per-artifact key later, a subsequent due pass removes
it. Tombstones remain until a future explicit quiescence/retention policy
retires them; cleanup is eventual rather than immediate.

The retained row metadata and recurring HEAD/delete requests are an explicit
operational cost of this guarantee. Do not run the reconciler against
production storage while validating the recovery drill; use disposable
Compose data and retain its diagnostics. The local focused recovery and
migration evidence is recorded in the final-fix report; no GitHub CI result is
claimed here until an external run produces corresponding diagnostics.
