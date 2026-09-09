# P1C Delivery, Observability, and Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the realtime inference system to inspectors and operators, add complete metrics/traces, and enforce deterministic E2E, recovery, and canonical performance acceptance.

**Architecture:** REST remains the business reconciliation path and WebSocket remains best-effort wakeup. Alert Outbox events wake per-instance Gateway consumers, while durable database cursors recover missed events. OpenTelemetry Collector/Tempo provide traces; Prometheus/Grafana/Alertmanager handle operational metrics only. PR CI proves correctness with deterministic fixtures; strict P95 runs on a fixed benchmark host.

**Tech Stack:** FastAPI, React 19, TypeScript 7, Redis Streams, OpenTelemetry, Tempo, Prometheus, Grafana, Alertmanager, Playwright, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-25-p1-realtime-ai-inference-design.md`

**Prerequisite:** Complete P1A and `docs/superpowers/plans/2026-08-25-p1b-distribution-inference-runtime.md` first; this plan exposes and validates their running system.

## Global Constraints

- WebSocket is best-effort. PostgreSQL REST cursor recovery is authoritative.
- Business defects use `inspection.alerts → Realtime Gateway → WebSocket`; operational incidents use `Prometheus → Alertmanager`.
- Long-lived JWTs never appear in WebSocket query strings; one-time 60-second tickets remain required.
- Evidence presigned URLs expire after 60 seconds and require tenant/line authorization.
- `correlation_id`, organization/camera/task/case/artifact IDs belong in logs/traces, never Prometheus labels.
- Hosted PR runners use a relaxed latency guardrail. Strict 4-camera, 8 inference/s, P95 < 2s for 10 minutes runs only on the canonical benchmark host.
- `alert_e2e_latency = websocket_delivered_at - frame_captured_at`; recorded-video capture time is current playback wall-clock.
- Success rate denominator contains only terminal tasks that actually entered model execution.

---

### Task 1: Replace direct alert publication with durable alert wakeups

**Files:**
- Create: `apps/web-backend/src/odp_api/adapters/notifications/redis_gateway_feed.py`
- Modify: `apps/web-backend/src/odp_api/ports/notifications.py`
- Modify: `apps/web-backend/src/odp_api/modules/notifications/router.py`
- Modify: `apps/web-backend/src/odp_api/main.py`
- Test: `apps/web-backend/tests/persistence/test_alert_feed_contract.py`
- Test: `apps/web-backend/tests/integration/test_live_notification_api.py`

**Interfaces:**
- Consumes: durable `inspection_alerts` database rows from P1A and `odp:inspection:alerts` from P1B Relay.
- Produces: `RedisGatewayInspectionAlertFeed.subscribe(after_cursor)` using a per-instance consumer group.
- Keeps: existing REST `/api/v1/inspection-events` and ticket-authenticated `/ws/inspection-events` contracts.

- [ ] **Step 1: Write a failing no-direct-publish test**

```python
def test_runtime_effect_path_never_xadds_alert_directly(app, relay_spy):
    app.state.inspection_effect_service.publish(defect_command())
    assert relay_spy.direct_xadd_calls == []
    assert relay_spy.pending_event_types == ["inspection.alert.created.v1"]
```

- [ ] **Step 2: Run the notification tests and verify failure**

Run: `pytest apps/web-backend/tests/persistence/test_alert_feed_contract.py apps/web-backend/tests/integration/test_live_notification_api.py -q`

Expected: FAIL because current runtime feed exposes `publish()` and uses direct bounded XADD.

- [ ] **Step 3: Split durable facts from Redis wakeups**

Keep database `list()` as the only reconciliation source. The new Gateway adapter is read-only: it creates `odp-realtime:<instance-id>` at `$`, consumes alert wakeups for this instance, then re-queries durable facts after its current database cursor. It ACKs after local delivery/requery, closes and destroys its ephemeral group on graceful shutdown, and registers group expiry metadata for Retention Controller cleanup.

- [ ] **Step 4: Preserve disconnect and cursor recovery behavior**

Keep the router's backlog-first loop, authorization checks, bounded batch size, cancellation cleanup, and one-time ticket authentication. Add slow-client send timeout; close with code 1013 so the frontend reconnects and calls REST.

- [ ] **Step 5: Run alert API regression tests**

Run: `pytest apps/web-backend/tests/persistence/test_alert_feed_contract.py apps/web-backend/tests/integration/test_notification_api.py apps/web-backend/tests/integration/test_live_notification_api.py apps/web-backend/tests/integration/test_websocket_tickets.py -q`

Expected: PASS for live wakeup, duplicate wakeup, reconnect recovery, authorization, and cancellation cleanup.

- [ ] **Step 6: Commit durable realtime wakeups**

```bash
git add apps/web-backend/src/odp_api/adapters/notifications/redis_gateway_feed.py apps/web-backend/src/odp_api/ports/notifications.py apps/web-backend/src/odp_api/modules/notifications/router.py apps/web-backend/src/odp_api/main.py apps/web-backend/tests/persistence/test_alert_feed_contract.py apps/web-backend/tests/integration/test_live_notification_api.py
git commit -m "feat: deliver durable inference alerts realtime"
```

---

### Task 2: Add inspection-session, task-diagnostic, replay, and evidence APIs

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/inspection_sessions/models.py`
- Create: `apps/web-backend/src/odp_api/modules/inspection_sessions/service.py`
- Create: `apps/web-backend/src/odp_api/modules/inspection_sessions/router.py`
- Create: `apps/web-backend/src/odp_api/modules/tasks/router.py`
- Create: `apps/web-backend/src/odp_api/modules/artifacts/router.py`
- Modify: `apps/web-backend/src/odp_api/modules/identity/service.py`
- Modify: `apps/web-backend/src/odp_api/main.py`
- Test: `apps/web-backend/tests/integration/test_inspection_sessions_api.py`
- Test: `apps/web-backend/tests/integration/test_task_diagnostics_api.py`
- Test: `apps/web-backend/tests/integration/test_evidence_api.py`

**Interfaces:**
- Produces endpoints frozen in spec section 11.1 plus `GET /api/v1/auth/me` for the authenticated actor's role and authorized lines.
- Consumes P1A Task queries/replay, P1B Ingestor control and object presigning.

- [ ] **Step 1: Write failing role and tenant API tests**

```python
def test_inspector_cannot_start_session(client, inspector_headers, camera_id):
    response = client.post("/api/v1/inspection-sessions", headers=inspector_headers,
        json={"camera_id": str(camera_id), "source_type": "RECORDED", "source_ref": "scratch-loop"})
    assert response.status_code == 403

def test_leader_replay_creates_new_task(client, leader_headers, dead_task):
    response = client.post(f"/api/v1/inference-tasks/{dead_task.task_id}:replay", headers=leader_headers)
    assert response.status_code == 201
    assert response.json()["task_id"] != str(dead_task.task_id)
```

- [ ] **Step 2: Run API tests and verify 404 failures**

Run: `pytest apps/web-backend/tests/integration/test_inspection_sessions_api.py apps/web-backend/tests/integration/test_task_diagnostics_api.py apps/web-backend/tests/integration/test_evidence_api.py -q`

Expected: FAIL because routers are absent.

- [ ] **Step 3: Implement session lifecycle and idempotency**

`POST /api/v1/inspection-sessions` requires leader/admin, authorized line/camera, and `Idempotency-Key`. Store sanitized source URI plus `secret_ref` as `START_REQUESTED` in PostgreSQL; P1B Ingestor guarded-claims that row. Stop is an idempotent `RUNNING/START_REQUESTED → STOP_REQUESTED` transition. List/get are tenant and line scoped. `GET /api/v1/auth/me` returns only actor ID, role, organization ID, and authorized line IDs.

- [ ] **Step 4: Implement task diagnostics and replay**

List filters: status, camera, created range, error code; cap limit at 100. Detail returns dispatch history and Attempts but never credentials/object bytes. Dead Letter replay creates a new Task/Outbox and audit entry. Compatibility replay guarded-transitions the same blocked Task to READY with incremented dispatch.

- [ ] **Step 5: Implement evidence URL authorization**

Load Artifact through Case/Event association and tenant/line scope, require EVIDENCE lifecycle, then return a 60-second presigned URL. A direct artifact UUID from another organization returns 404, not 403, to avoid enumeration.

- [ ] **Step 6: Run API and audit tests**

Run: `pytest apps/web-backend/tests/integration/test_inspection_sessions_api.py apps/web-backend/tests/integration/test_task_diagnostics_api.py apps/web-backend/tests/integration/test_evidence_api.py apps/web-backend/tests/modules/audit/test_hash_chain.py -q`

Expected: PASS for RBAC, tenant isolation, idempotency, replay audit, and URL TTL.

- [ ] **Step 7: Commit operational APIs**

```bash
git add apps/web-backend/src/odp_api/modules/inspection_sessions apps/web-backend/src/odp_api/modules/tasks/router.py apps/web-backend/src/odp_api/modules/artifacts apps/web-backend/src/odp_api/modules/identity/service.py apps/web-backend/src/odp_api/main.py apps/web-backend/tests/integration/test_inspection_sessions_api.py apps/web-backend/tests/integration/test_task_diagnostics_api.py apps/web-backend/tests/integration/test_evidence_api.py
git commit -m "feat: expose realtime inspection operations"
```

---

### Task 3: Add the realtime operations UI

**Files:**
- Modify: `apps/web-frontend/src/api/types.ts`
- Modify: `apps/web-frontend/src/api/client.ts`
- Create: `apps/web-frontend/src/features/operations/InspectionOperations.tsx`
- Create: `apps/web-frontend/src/features/operations/InspectionOperations.test.tsx`
- Create: `apps/web-frontend/src/features/operations/TaskDiagnostics.tsx`
- Create: `apps/web-frontend/src/features/operations/TaskDiagnostics.test.tsx`
- Modify: `apps/web-frontend/src/features/workbench/RealtimeWorkbench.tsx`
- Modify: `apps/web-frontend/src/app.css`

**Interfaces:**
- Consumes Task 2 REST endpoints.
- Produces leader/admin session controls, queue/task status, dead-letter replay, and evidence preview.

- [ ] **Step 1: Write failing role-visible UI tests**

```tsx
it("shows session controls to a leader and hides them from an inspector", async () => {
  const { rerender } = render(<InspectionOperations role="LEADER" />);
  expect(await screen.findByRole("button", { name: "启动实时检测" })).toBeVisible();
  rerender(<InspectionOperations role="INSPECTOR" />);
  expect(screen.queryByRole("button", { name: "启动实时检测" })).toBeNull();
});
```

- [ ] **Step 2: Run frontend tests and verify failure**

Run: `npm test -- InspectionOperations.test.tsx TaskDiagnostics.test.tsx`

Workdir: `apps/web-frontend`

Expected: FAIL because operations components do not exist.

- [ ] **Step 3: Add typed API methods**

Add `CurrentActor`, `InspectionSession`, `InferenceTaskSummary`, `InferenceAttemptSummary`, and `EvidenceUrlResponse`. Implement `getCurrentActor`, `startInspectionSession`, `stopInspectionSession`, `listInferenceTasks`, `getInferenceTask`, `replayInferenceTask`, and `getEvidenceUrl` through `apiFetch`; use `CurrentActor.role` rather than decoding JWTs in the browser.

- [ ] **Step 4: Build accessible session and task panels**

Show source health, camera, input/sampled FPS, oldest READY age, Worker health, task status, attempt timeline, error code, model version, and replay button. Use text plus color for severity. Virtualize or cap diagnostics to 100 rows; refresh at a bounded interval and stop polling when hidden.

- [ ] **Step 5: Integrate evidence and realtime latency display**

Case detail requests evidence URL only when opened, handles expiry by refetching, and never stores the URL in localStorage. Display alert age and “实时/已通过游标补偿” source without changing Case truth.

- [ ] **Step 6: Run frontend unit/build tests**

Run: `npm test && npm run build && npm run typecheck:e2e`

Workdir: `apps/web-frontend`

Expected: PASS.

- [ ] **Step 7: Commit operations UI**

```bash
git add apps/web-frontend/src
git commit -m "feat: add realtime inspection operations UI"
```

---

### Task 4: Add OpenTelemetry/Tempo and bounded-cardinality metrics

**Files:**
- Modify: `apps/web-backend/pyproject.toml`
- Modify: `apps/web-backend/uv.lock`
- Create: `apps/web-backend/src/odp_api/observability/runtime.py`
- Create: `apps/web-backend/src/odp_api/observability/p1_metrics.py`
- Modify: `apps/web-backend/src/odp_api/observability/tracing.py`
- Modify: `deploy/compose.yaml`
- Create: `deploy/observability/otel-collector.yml`
- Create: `deploy/observability/tempo.yml`
- Modify: `deploy/observability/grafana/provisioning/datasources/datasource.yml`
- Modify: `deploy/observability/grafana/dashboards/quality-inspection.json`
- Modify: `deploy/observability/alerts.yml`
- Test: `apps/web-backend/tests/test_p1_observability.py`

**Interfaces:**
- Produces OTLP traces from API, Relay, Ingestor, Worker, Scheduler, Redis, MinIO, and PostgreSQL.
- Produces process-local `/metrics` endpoints with enum-only labels.

- [ ] **Step 1: Add observability dependencies and write failing cardinality tests**

Add `prometheus-client>=0.21,<1`, `opentelemetry-sdk>=1.29,<2`, `opentelemetry-exporter-otlp-proto-grpc>=1.29,<2`, `opentelemetry-instrumentation-fastapi>=0.50b0,<1`, `opentelemetry-instrumentation-sqlalchemy>=0.50b0,<1`, and `opentelemetry-instrumentation-redis>=0.50b0,<1`.

```python
def test_p1_metric_labels_reject_entity_ids():
    with pytest.raises(ValueError):
        P1Metrics().counter("task_total", labels={"task_id": str(uuid4())})

def test_trace_carries_correlation_without_metric_label(registry, tracer):
    record_pipeline_stage("inference", 0.12, correlation_id=uuid4())
    assert "correlation_id" not in registry.render()
    assert tracer.last_span.attributes["odp.correlation_id"]
```

- [ ] **Step 2: Run observability tests and verify failure**

Run: `pytest apps/web-backend/tests/test_p1_observability.py -q`

Expected: FAIL because P1 metrics and OTLP runtime are absent.

- [ ] **Step 3: Implement traces and metric allowlists**

Propagate W3C `traceparent`/`tracestate` fields plus existing correlation ID through the P1B Event Envelope. Emit spans for admission, upload, queue wait, fetch, inference, fenced commit, outbox publish, and Gateway delivery. Allow metric labels only from `{task_type, outcome, error_code, runtime, model_release}` with model release capped to configured values.

- [ ] **Step 4: Add Collector, Tempo, and Grafana datasource**

Collector accepts OTLP gRPC/HTTP and exports traces to Tempo. Grafana provisions Prometheus and Tempo, with trace-to-metric links. Add health checks and persistent Tempo volume to Compose. Expose process-local Prometheus endpoints on API 8000, Workers 9101/9102, Relay 9103, Ingestor 9104, Scheduler 9105, and Retention Controller 9106; add every target to `prometheus.yml`.

- [ ] **Step 5: Separate business and operational alert rules**

Alertmanager rules cover dead letters, model configuration, Outbox age > 30 seconds, Worker absence, PEL growth, PENDING Artifact > 5 minutes, and sustained canonical alert latency. No rule publishes `inspection.alert.created.v1` or targets inspector WebSockets.

- [ ] **Step 6: Validate observability stack**

Run: `pytest apps/web-backend/tests/test_p1_observability.py apps/web-backend/tests/test_metrics.py -q`

Run: `docker compose -f deploy/compose.yaml config --quiet`

Expected: PASS; Grafana has both datasources and no prohibited label names exist in dashboard queries.

- [ ] **Step 7: Commit observability**

```bash
git add apps/web-backend/pyproject.toml apps/web-backend/uv.lock apps/web-backend/src/odp_api/observability deploy/compose.yaml deploy/observability apps/web-backend/tests/test_p1_observability.py
git commit -m "feat: trace and monitor realtime inference"
```

---

### Task 5: Replace the development event trigger with deterministic video E2E

**Files:**
- Create: `apps/web-backend/scripts/build_inspection_video.py`
- Create: `apps/web-backend/tests/fixtures/video/scratch-loop.mp4`
- Modify: `apps/web-frontend/e2e/quality-workflow.spec.ts`
- Create: `apps/web-frontend/e2e/realtime-recovery.spec.ts`
- Modify: `apps/web-backend/tests/e2e/test_quality_workflow.py`
- Modify: `deploy/compose.yaml`

**Interfaces:**
- Consumes the complete P1 runtime.
- Produces a deterministic recorded-video path that generates a real Task, Attempt, Result, Event, Case, Outbox, Redis alert, and WebSocket frame.

- [ ] **Step 1: Generate a deterministic four-camera video fixture**

The script creates a two-second 10 FPS 640×480 MP4 with a stable synthetic scratch region and no external assets. Run:

`python apps/web-backend/scripts/build_inspection_video.py`

Commit the small resulting fixture and its SHA-256 manifest.

- [ ] **Step 2: Rewrite the E2E test before changing runtime seed behavior**

Remove the call to `/api/v1/dev/inspection-events`. Start or use the seeded recorded-video session, capture the WebSocket frame, then assert the related Task detail includes at least one Attempt and a Published Result using the configured Mock model.

```ts
await expect.poll(async () => {
  const tasks = await page.evaluate(async () => {
    const token = localStorage.getItem("odp_token");
    return fetch("/api/v1/inference-tasks?status=SUCCEEDED", {
      headers: { Authorization: `Bearer ${token}` },
    }).then((response) => response.json());
  });
  return tasks.items.length;
}).toBeGreaterThan(0);
```

- [ ] **Step 3: Run E2E and verify it fails without the new seeded session**

Run: `npm run test:e2e`

Workdir: `apps/web-frontend`

Expected: FAIL because Compose does not yet auto-start the deterministic recorded session.

- [ ] **Step 4: Seed and start four recorded sessions in Compose**

Create three normal/no-defect camera sessions and one scratch camera. Use current loop wall-clock timestamps. Keep the development trigger behind its existing opt-in flag for focused tests, but remove it from P1 acceptance evidence.

- [ ] **Step 5: Add browser recovery assertions**

Restart Gateway/API after an alert commit, reconnect with a fresh one-time ticket, and assert REST cursor recovery supplies any missed alert. Assert no long-lived JWT appears in captured WebSocket URLs.

- [ ] **Step 6: Run deterministic workflow**

Run: `docker compose -f deploy/compose.yaml up -d --build`

Run: `E2E_BASE_URL=http://localhost:8080 npm run test:e2e`

Workdir for second command: `apps/web-frontend`

Expected: all Playwright specs pass without invoking the development event trigger.

- [ ] **Step 7: Commit the real pipeline E2E**

```bash
git add apps/web-backend/scripts/build_inspection_video.py apps/web-backend/tests/fixtures apps/web-backend/tests/e2e apps/web-frontend/e2e deploy/compose.yaml
git commit -m "test: exercise realtime video inspection end to end"
```

---

### Task 6: Add fault-matrix and canonical benchmark harnesses

**Files:**
- Create: `apps/web-backend/scripts/p1_fault_matrix.py`
- Create: `apps/web-backend/scripts/p1_benchmark.py`
- Create: `apps/web-backend/tests/e2e/test_p1_fault_matrix.py`
- Create: `docs/benchmarks/p1/README.md`
- Test output: `artifacts/p1-benchmark/report.json` (generated, not committed by default)

**Interfaces:**
- Produces executable recovery scenarios and canonical JSON benchmark schema.
- Benchmark manifest records every field required by spec section 14.2.

- [ ] **Step 1: Write failing report-schema tests**

```python
def test_benchmark_report_requires_execution_manifest():
    report = BenchmarkReport.model_validate_json(run_fixture_benchmark())
    assert report.model_sha256
    assert report.input_shape == [1, 3, 640, 640]
    assert report.cpu.model
    assert report.cpu.cores > 0
    assert report.onnxruntime_version
    assert report.execution_provider == "CPUExecutionProvider"
    assert report.worker_count == 2
```

- [ ] **Step 2: Run harness tests and verify failure**

Run: `pytest apps/web-backend/tests/e2e/test_p1_fault_matrix.py -q`

Expected: FAIL because harness scripts and report models do not exist.

- [ ] **Step 3: Implement fault scenarios**

Automate: Worker crash before commit, crash after commit/before ACK, Redis restart/stream loss, stale READY redispatch, duplicate Outbox publish, old Worker finalize, unsupported schema quarantine, PEL-safe trim, MinIO upload/DB failure, and concurrent same-defect Case dedup. Each scenario queries PostgreSQL invariants and exits nonzero on duplicate Result/Case or recovery over 30 seconds.

- [ ] **Step 4: Implement benchmark calculations**

Run four cameras at 10 FPS input/2 FPS inference for configurable duration. Record terminal executed tasks, success rate, sampling/backpressure/stale/dead-letter rates, backlog slope, and stage histograms. Compute alert E2E from wall-clock capture to Gateway delivery. Reject a report missing model/runtime/CPU/Worker/pre/postprocessing/NMS/class-map manifest fields.

- [ ] **Step 5: Run short local correctness mode**

Run: `python apps/web-backend/scripts/p1_fault_matrix.py --compose-file deploy/compose.yaml`

Run: `python apps/web-backend/scripts/p1_benchmark.py --duration 60 --latency-guardrail-ms 5000 --output artifacts/p1-benchmark/report.json`

Expected: all fault scenarios pass; no backlog growth or duplicate Case; hosted-style latency remains below the relaxed 5-second guardrail.

- [ ] **Step 6: Document canonical release invocation**

Canonical host command:

```bash
python apps/web-backend/scripts/p1_benchmark.py \
  --duration 600 \
  --cameras 4 \
  --inference-fps 2 \
  --latency-guardrail-ms 2000 \
  --require-success-rate 0.99 \
  --output artifacts/p1-benchmark/report.json
```

- [ ] **Step 7: Commit fault and benchmark evidence**

```bash
git add apps/web-backend/scripts/p1_fault_matrix.py apps/web-backend/scripts/p1_benchmark.py apps/web-backend/tests/e2e/test_p1_fault_matrix.py docs/benchmarks/p1/README.md
git commit -m "test: validate inference recovery and capacity"
```

---

### Task 7: Make P1 correctness a CI and delivery gate

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `deploy/README.md`
- Modify: `README.md`
- Create: `docs/p1-realtime-inspection-runbook.md`

**Interfaces:**
- Consumes all P1A/P1B/P1C commands.
- Produces PR correctness gates and a human-reviewable demo/recovery runbook.

- [ ] **Step 1: Add PR jobs with stable scopes**

Backend job runs full Ruff, unit/property/PostgreSQL tests, ONNX smoke, and real Redis/MinIO integration. Add `redis:7-alpine` as a service and start MinIO with `minio/minio server /data`; export their test URLs explicitly. Compose E2E runs recorded video, Playwright, fault-matrix short mode, four-camera short load, 30-second recovery, no-backlog, and no-duplicate-Case checks. Use relaxed 5-second latency guardrail on hosted runners.

- [ ] **Step 2: Add artifact collection**

Always upload Compose logs, traces, Prometheus snapshot, fault report, benchmark JSON, and Playwright trace on failure. Do not upload frame bytes or credentials.

- [ ] **Step 3: Update the demo and incident runbook**

Document startup, seeded accounts, starting/stopping sessions, viewing task attempts/evidence, Grafana/Tempo navigation, replay authorization, Redis/Worker failure drills, expected recovery, and safe teardown. Clearly label inspection alerts versus Alertmanager operations alerts.

- [ ] **Step 4: Run the complete local verification**

Run: `ruff check apps/web-backend/src apps/web-backend/tests packages/shared-schemas/src`

Run: `pytest -q`

Run: `npm test && npm run build && npm run typecheck:e2e`

Workdir for npm command: `apps/web-frontend`

Run: `docker compose -f deploy/compose.yaml config --quiet`

Run: `docker compose -f deploy/compose.yaml up -d --build && E2E_BASE_URL=http://localhost:8080 npm --prefix apps/web-frontend run test:e2e`

Expected: every command exits 0.

- [ ] **Step 5: Run short fault and load acceptance**

Run: `python apps/web-backend/scripts/p1_fault_matrix.py --compose-file deploy/compose.yaml`

Run: `python apps/web-backend/scripts/p1_benchmark.py --duration 60 --latency-guardrail-ms 5000 --output artifacts/p1-benchmark/report.json`

Expected: recovery ≤ 30 seconds, no duplicate Case, no sustained backlog, valid report.

- [ ] **Step 6: Commit CI and delivery documentation**

```bash
git add .github/workflows/ci.yml deploy/README.md README.md docs/p1-realtime-inspection-runbook.md
git commit -m "ci: gate realtime inference delivery"
```
