# P1B Distribution and Inference Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the PostgreSQL P1A control plane to MinIO, Redis Streams, independent recovery-aware Workers, recorded/RTSP sources, and deterministic Mock/real ONNX inference.

**Architecture:** A generic Outbox Relay maps stable event types to Redis destinations. Frame Ingestor samples and reserves capacity before MinIO upload. Workers use Redis consumer groups for delivery but must obtain a PostgreSQL fenced lease before execution; Worker loops own `XAUTOCLAIM`, while separate Scheduler and Retention processes repair database state and safely trim Redis 7 streams.

**Tech Stack:** Python 3.12, asyncio, redis-py 5/6, Redis 7, PostgreSQL 16, MinIO Python SDK, OpenCV headless, NumPy, ONNX Runtime CPU, Docker Compose, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-p1-realtime-ai-inference-design.md`

**Prerequisite:** Complete `docs/superpowers/plans/2026-08-25-p1a-task-control-plane.md` first; this plan imports its domain types, repositories, fencing, recovery, and effect transaction.

## Global Constraints

- PostgreSQL remains authoritative; Redis messages carry references, never frame bytes or business state.
- Stream event types are stable domain names; domain code never stores Redis stream names.
- Worker ACK occurs only after the database transaction commits.
- Worker recovery uses `XAUTOCLAIM` with `min-idle-time=20s`; Recovery Scheduler never consumes inference messages.
- Redis 7 retention uses exact PEL-safe `XTRIM MINID`; approximate `MAXLEN` is prohibited for inference and alert consumer-group streams.
- Decode 10 FPS, sample/admit 2 FPS before MinIO upload; per camera keep one RUNNING and two READY.
- Processing Artifact retention is database-controlled; Evidence retention defaults to configurable 90 days.
- Inference timeout is 10 seconds; lease renewal every 5 seconds; recovery loop every 2 seconds.
- Mock output is deterministic. ONNX replay is promised only under the persisted execution contract.

---

### Task 1: Define versioned event envelopes and a generic Outbox Relay

**Files:**
- Modify: `packages/shared-schemas/src/odp_schemas/events.py`
- Create: `apps/web-backend/src/odp_api/ports/events.py`
- Create: `apps/web-backend/src/odp_api/adapters/events/redis_streams.py`
- Create: `apps/web-backend/src/odp_api/processes/outbox_relay.py`
- Test: `apps/web-backend/tests/modules/tasks/test_event_envelope.py`
- Test: `apps/web-backend/tests/integration/test_outbox_relay.py`

**Interfaces:**
- Consumes: P1A `outbox_events` and task repository.
- Produces: `EventEnvelope`, `OutboxPublisherPort.publish(envelope) -> str`, `OutboxRelay.run_batch(limit, now) -> RelayBatchResult`.
- Destination mapping: `vision.inference.requested.v1 → odp:inference:tasks`; `inspection.alert.created.v1 → odp:inspection:alerts`.

- [ ] **Step 1: Write failing envelope serialization tests**

```python
def test_inference_event_contains_only_reference_fields():
    envelope = EventEnvelope.model_validate({
        "event_id": str(uuid4()), "event_type": "vision.inference.requested.v1",
        "schema_version": 1, "occurred_at": "2026-08-25T12:00:00Z",
        "correlation_id": str(uuid4()), "organization_id": str(uuid4()),
        "aggregate_id": str(uuid4()), "payload": {"task_id": str(uuid4()), "dispatch_seq": 2},
    })
    body = envelope.model_dump(mode="json")
    assert set(body["payload"]) == {"task_id", "dispatch_seq"}
    assert "object_key" not in str(body)
```

- [ ] **Step 2: Run the envelope test and verify failure**

Run: `pytest apps/web-backend/tests/modules/tasks/test_event_envelope.py -q`

Expected: FAIL because `EventEnvelope` does not exist.

- [ ] **Step 3: Implement the Pydantic envelope and destination allowlist**

```python
EVENT_DESTINATIONS = {
    "vision.inference.requested.v1": "odp:inference:tasks",
    "inspection.alert.created.v1": "odp:inspection:alerts",
}

class EventEnvelope(BaseModel):
    event_id: UUID
    event_type: str
    schema_version: int = Field(ge=1)
    occurred_at: datetime
    correlation_id: UUID
    organization_id: UUID
    aggregate_id: UUID
    traceparent: str | None = None
    tracestate: str | None = None
    payload: dict[str, JsonValue]
```

`RedisOutboxPublisher` rejects event types outside the allowlist and calls unbounded `XADD stream * envelope <canonical-json>`. Retention is a separate process; publication must not use `MAXLEN`.

- [ ] **Step 4: Write a failing Relay crash-window test**

```python
def test_relay_republishes_when_redis_succeeded_before_db_mark(repository, redis, relay, now):
    event = repository.insert_ready_outbox(now)
    repository.fail_next_mark_published()
    with pytest.raises(InjectedCommitFailure):
        relay.run_batch(10, now)
    relay.run_batch(10, now + timedelta(seconds=1))
    assert redis.event_ids.count(str(event.outbox_id)) == 2
```

- [ ] **Step 5: Implement Relay claim, publish, and mark flow**

Claim rows with `FOR UPDATE SKIP LOCKED`, a bounded claim lease, and configured batch size. Publish outside the claiming transaction; mark `published_at` in a second guarded transaction. On publish error, increment attempts and set capped exponential `available_at`; never discard the Outbox row.

- [ ] **Step 6: Run Relay tests**

Run: `pytest apps/web-backend/tests/modules/tasks/test_event_envelope.py apps/web-backend/tests/integration/test_outbox_relay.py -q`

Expected: PASS for routing, duplicate publish, Redis outage, claim expiry, and unknown event rejection.

- [ ] **Step 7: Commit event distribution**

```bash
git add packages/shared-schemas apps/web-backend/src/odp_api/ports/events.py apps/web-backend/src/odp_api/adapters/events apps/web-backend/src/odp_api/processes/outbox_relay.py apps/web-backend/tests/modules/tasks/test_event_envelope.py apps/web-backend/tests/integration/test_outbox_relay.py
git commit -m "feat: relay transactional outbox events"
```

---

### Task 2: Build the inference consumer and Worker recovery loop

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/tasks/consumer.py`
- Create: `apps/web-backend/src/odp_api/processes/inference_worker.py`
- Test: `apps/web-backend/tests/modules/tasks/test_inference_consumer.py`
- Test: `apps/web-backend/tests/integration/test_redis_worker_recovery.py`

**Interfaces:**
- Consumes: P1A `TaskExecutionPort`, `InspectionEffectService`; P1B `EventEnvelope`.
- Produces: `RedisInferenceConsumer.read_new()`, `claim_stale()`, `ack(message_id)` and `InferenceWorker.process(message)`.
- Consumer group: `odp-inference-workers`; consumer name is a stable process UUID for one process lifetime.

- [ ] **Step 1: Write failing ACK-order and duplicate-message tests**

```python
async def test_worker_acks_only_after_effect_commit(worker, message, db, redis):
    db.block_effect_commit()
    task = asyncio.create_task(worker.process(message))
    await db.effect_started.wait()
    assert redis.acked == []
    db.release_effect_commit()
    await task
    assert redis.acked == [message.message_id]

async def test_terminal_duplicate_is_acked_without_new_attempt(worker, terminal_message, repository):
    before = repository.attempt_count(terminal_message.task_id)
    await worker.process(terminal_message)
    assert repository.attempt_count(terminal_message.task_id) == before
```

- [ ] **Step 2: Run consumer tests and verify failure**

Run: `pytest apps/web-backend/tests/modules/tasks/test_inference_consumer.py -q`

Expected: FAIL because consumer and Worker loops do not exist.

- [ ] **Step 3: Implement Redis group operations with redis-py asyncio**

On startup, call `await client.xgroup_create("odp:inference:tasks", "odp-inference-workers", id="0-0", mkstream=True)` and tolerate BUSYGROUP. `read_new()` calls:

```python
await client.xreadgroup(
    groupname="odp-inference-workers",
    consumername=self.consumer_name,
    streams={"odp:inference:tasks": ">"},
    count=10,
    block=2_000,
)
```

`claim_stale()` calls `xautoclaim` with `min_idle_time=20_000` and transfers messages to the same Worker consumer name.

- [ ] **Step 4: Implement Worker message handling**

Parse at most 64 KiB. Unsupported versions call P1A quarantine, then ACK after commit. Known messages load Task by tenant, acquire a fenced claim, and start a 5-second renewal coroutine. A rejected claim is ACKed only if the Task is terminal/blocked; a live lease owned elsewhere remains Pending for later recovery. Run inference with `asyncio.timeout(10)` and publish the effect before ACK.

- [ ] **Step 5: Prove `XAUTOCLAIM` and DB fencing interact correctly**

The real Redis/PostgreSQL integration test must kill/cancel Worker A after claim, wait for configured recovery, let Worker B `XAUTOCLAIM`, and assert one Published Result and one Case. Add a second crash point after commit/before ACK and assert redelivery only ACKs.

- [ ] **Step 6: Run Worker tests**

Run: `pytest apps/web-backend/tests/modules/tasks/test_inference_consumer.py apps/web-backend/tests/integration/test_redis_worker_recovery.py -q`

Expected: PASS, with recovery below 30 seconds under configured test timings.

- [ ] **Step 7: Commit the Worker loop**

```bash
git add apps/web-backend/src/odp_api/modules/tasks/consumer.py apps/web-backend/src/odp_api/processes/inference_worker.py apps/web-backend/tests/modules/tasks/test_inference_consumer.py apps/web-backend/tests/integration/test_redis_worker_recovery.py
git commit -m "feat: execute fenced Redis inference work"
```

---

### Task 3: Implement Redis 7 PEL-safe retention and database recovery processes

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/tasks/retention.py`
- Create: `apps/web-backend/src/odp_api/processes/stream_retention.py`
- Create: `apps/web-backend/src/odp_api/processes/recovery_scheduler.py`
- Test: `apps/web-backend/tests/modules/tasks/test_stream_retention.py`
- Test: `apps/web-backend/tests/integration/test_redis7_pel_retention.py`

**Interfaces:**
- Produces: `SafeTrimPlanner.safe_min_id(groups, retention_floor_id) -> str | None`.
- Produces: `StreamRetentionController.run_once() -> TrimSummary` and process entrypoint.
- Consumes: P1A `RecoveryService.run_once()`.

- [ ] **Step 1: Write failing safe-watermark tests**

```python
def test_trim_watermark_never_crosses_smallest_pending_id():
    groups = (
        GroupProgress(last_delivered_id="90-0", smallest_pending_id="40-0"),
        GroupProgress(last_delivered_id="80-0", smallest_pending_id=None),
    )
    assert SafeTrimPlanner.safe_min_id(groups, "70-0") == "40-0"

def test_no_group_progress_means_no_trim():
    assert SafeTrimPlanner.safe_min_id((), "70-0") is None
```

- [ ] **Step 2: Run retention unit tests and verify failure**

Run: `pytest apps/web-backend/tests/modules/tasks/test_stream_retention.py -q`

Expected: FAIL because no safe planner exists.

- [ ] **Step 3: Implement exact Redis 7 trimming**

Read `XINFO GROUPS`; for every active group read `XPENDING` summary. Compute the minimum of each group's smallest Pending ID or last-delivered ID, then cap it by the time-derived minimum retention floor. Execute only `XTRIM <stream> MINID = <safe-id>`. Do not call `MAXLEN`. Remove expired Gateway instance groups before calculating the next alert-stream watermark.

- [ ] **Step 4: Write the real Redis payload-preservation test**

Create messages, deliver one without ACK, ACK later entries, run retention, and assert `XRANGE` still returns the Pending entry payload. After ACK, rerun retention and assert it becomes removable.

- [ ] **Step 5: Wire Recovery Scheduler cadence**

`recovery_scheduler.py` calls P1A recovery every 2 seconds, uses a database advisory lock so only one active scheduler performs a sweep, and reports expired leases, due retries, stale READY redispatches, stuck Outbox claims, quarantine count, and stale Artifact reservations.

- [ ] **Step 6: Run retention and recovery tests**

Run: `pytest apps/web-backend/tests/modules/tasks/test_stream_retention.py apps/web-backend/tests/integration/test_redis7_pel_retention.py apps/web-backend/tests/modules/tasks/test_recovery.py -q`

Expected: PASS with Pending payload retained and acknowledged history bounded.

- [ ] **Step 7: Commit retention and recovery processes**

```bash
git add apps/web-backend/src/odp_api/modules/tasks/retention.py apps/web-backend/src/odp_api/processes/stream_retention.py apps/web-backend/src/odp_api/processes/recovery_scheduler.py apps/web-backend/tests/modules/tasks/test_stream_retention.py apps/web-backend/tests/integration/test_redis7_pel_retention.py
git commit -m "feat: recover and safely trim Redis streams"
```

---

### Task 4: Implement MinIO Artifact Saga and cleanup

**Files:**
- Create: `apps/web-backend/src/odp_api/ports/storage.py`
- Create: `apps/web-backend/src/odp_api/adapters/storage/minio.py`
- Create: `apps/web-backend/src/odp_api/modules/ingestion/artifacts.py`
- Create: `apps/web-backend/src/odp_api/processes/artifact_reconciler.py`
- Modify: `apps/web-backend/pyproject.toml`
- Modify: `apps/web-backend/uv.lock`
- Test: `apps/web-backend/tests/modules/ingestion/test_artifact_saga.py`
- Test: `apps/web-backend/tests/integration/test_minio_artifacts.py`

**Interfaces:**
- Produces: `ObjectStoragePort.put`, `head`, `get`, `delete`, `presign_get`.
- Produces: `ArtifactSaga.ingest(selected_frame, health, now) -> TaskRecord | AdmissionRejected`.
- Object key: `organizations/{organization_id}/artifacts/{artifact_id}`; no lifecycle-by-prefix rule.

- [ ] **Step 1: Add MinIO dependency and write failing Saga-order test**

Add `minio>=7.2,<8` and refresh `uv.lock`.

```python
def test_rejected_frame_never_uploads(saga, unhealthy_workers, storage):
    result = saga.ingest(frame(), unhealthy_workers, NOW)
    assert result.reason == "WORKER_UNHEALTHY"
    assert storage.put_calls == []

def test_upload_is_verified_before_task_becomes_ready(saga, storage, repository):
    task = saga.ingest(frame(), healthy_dependencies(), NOW)
    assert storage.calls[:2] == ["put", "head"]
    assert repository.get(task.task_id, task.organization_id).status is TaskStatus.READY
```

- [ ] **Step 2: Run Saga tests and verify failure**

Run: `pytest apps/web-backend/tests/modules/ingestion/test_artifact_saga.py -q`

Expected: FAIL because storage port and Saga do not exist.

- [ ] **Step 3: Implement MinIO adapter and Saga compensation**

Upload immutable bytes with SHA-256 metadata; verify key, size, and digest using HEAD before completing the P1A reservation. Upload failure marks Artifact FAILED and releases admission. DB completion failure leaves a PENDING orphan candidate. Presigned evidence URLs expire after 60 seconds and are created only after tenant/resource authorization.

- [ ] **Step 4: Implement Artifact Reconciler and Cleaner**

For old PENDING rows, HEAD the object: complete only when metadata matches, otherwise fail and release reservation. Cleaner reads PostgreSQL lifecycle/retention and refuses deletion when an Inspection Event references the Artifact. Evidence retention setting defaults to 90 days; MinIO bucket lifecycle must remain disabled for processing prefixes.

- [ ] **Step 5: Run real MinIO integration tests**

Run: `pytest apps/web-backend/tests/modules/ingestion/test_artifact_saga.py apps/web-backend/tests/integration/test_minio_artifacts.py -q`

Expected: PASS for success, upload failure, DB failure orphan, evidence promotion, presign TTL, and protected cleanup.

- [ ] **Step 6: Commit Artifact infrastructure**

```bash
git add apps/web-backend/src/odp_api/ports/storage.py apps/web-backend/src/odp_api/adapters/storage apps/web-backend/src/odp_api/modules/ingestion apps/web-backend/src/odp_api/processes/artifact_reconciler.py apps/web-backend/pyproject.toml apps/web-backend/uv.lock apps/web-backend/tests/modules/ingestion apps/web-backend/tests/integration/test_minio_artifacts.py
git commit -m "feat: persist inference frame artifacts"
```

---

### Task 5: Add recorded-video, RTSP, and camera ingestion

**Files:**
- Create: `apps/web-backend/src/odp_api/ports/frame_sources.py`
- Create: `apps/web-backend/src/odp_api/ports/inspection_sessions.py`
- Create: `apps/web-backend/src/odp_api/adapters/frame_sources/opencv.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/inspection_sessions.py`
- Create: `apps/web-backend/src/odp_api/modules/ingestion/service.py`
- Create: `apps/web-backend/src/odp_api/processes/frame_ingestor.py`
- Modify: `apps/web-backend/pyproject.toml`
- Modify: `apps/web-backend/uv.lock`
- Test: `apps/web-backend/tests/modules/ingestion/test_ingestor.py`

**Interfaces:**
- Produces: `FrameEnvelope(camera_id, session_id, sequence, captured_at, jpeg_bytes, sha256)`.
- Produces source adapters `RecordedVideoSource`, `RtspSource`, `LocalCameraSource` implementing async iteration.
- Produces `InspectionSessionPort.claim_start_requests(process_id, limit)` and heartbeat/stop/failure transitions backed by the P1A `inspection_sessions` table.
- Consumes: `ArtifactSaga.ingest()` from Task 4.

- [ ] **Step 1: Add OpenCV/NumPy and write a failing wall-clock replay test**

Add `opencv-python-headless>=4.10,<5` and `numpy>=1.26,<3`.

```python
async def test_recorded_video_uses_current_loop_wall_clock(fake_capture, clock):
    source = RecordedVideoSource("fixture.mp4", clock=clock)
    first = await anext(source.frames())
    clock.advance(seconds=5)
    replayed = await source.restart_and_read_first()
    assert replayed.captured_at == clock.now()
    assert replayed.captured_at != first.captured_at
```

- [ ] **Step 2: Run ingestion tests and verify failure**

Run: `pytest apps/web-backend/tests/modules/ingestion/test_ingestor.py -q`

Expected: FAIL because frame source adapters do not exist.

- [ ] **Step 3: Implement sanitized source configuration and adapters**

Persist only sanitized URI plus `secret_ref`; resolve credentials at process startup from a Secret Provider. The Ingestor polls and guarded-claims `START_REQUESTED` database sessions, heartbeats RUNNING sessions, and observes `STOP_REQUESTED` without an in-memory API control channel. Recorded video loops deterministically. RTSP reconnect uses capped backoff and emits source-health state. Local camera uses explicit device index. Encode selected frames as JPEG only after 2 FPS sampling.

- [ ] **Step 4: Implement degraded preflight and admission-before-upload**

For every decoded frame: sample, query bounded health snapshot, reject before encoding/upload when camera window, oldest READY age, Worker health, Redis availability, or TTL budget is unacceptable; otherwise call Artifact Saga. Report sampling, admission, and rejection reasons separately.

- [ ] **Step 5: Run ingestion tests**

Run: `pytest apps/web-backend/tests/modules/ingestion/test_ingestor.py -q`

Expected: PASS for 10→2 FPS selection, wall-clock timestamps, RTSP reconnect, secret sanitization, degraded stop, and no-upload rejection.

- [ ] **Step 6: Commit source ingestion**

```bash
git add apps/web-backend/src/odp_api/ports/frame_sources.py apps/web-backend/src/odp_api/ports/inspection_sessions.py apps/web-backend/src/odp_api/adapters/frame_sources apps/web-backend/src/odp_api/adapters/persistence/inspection_sessions.py apps/web-backend/src/odp_api/modules/ingestion apps/web-backend/src/odp_api/processes/frame_ingestor.py apps/web-backend/pyproject.toml apps/web-backend/uv.lock apps/web-backend/tests/modules/ingestion/test_ingestor.py
git commit -m "feat: ingest sampled realtime video frames"
```

---

### Task 6: Implement deterministic Mock and real ONNX adapters

**Files:**
- Modify: `apps/web-backend/src/odp_api/ports/vision.py`
- Modify: `apps/web-backend/src/odp_api/adapters/vision/mock.py`
- Create: `apps/web-backend/src/odp_api/adapters/vision/onnx.py`
- Create: `apps/web-backend/scripts/build_fixture_onnx.py`
- Create: `apps/web-backend/tests/fixtures/models/tiny-detector.onnx`
- Modify: `apps/web-backend/pyproject.toml`
- Modify: `apps/web-backend/uv.lock`
- Test: `apps/web-backend/tests/modules/inspection/test_vision_contract.py`
- Test: `apps/web-backend/tests/integration/test_onnx_smoke.py`

**Interfaces:**
- Produces: `VisionInferencePort.inspect(frame, contract) -> VisionResult` with detections tuple and full execution contract.
- Supports output modes `post_nms_xyxy` and `yolo_raw`; the latter applies versioned NumPy NMS.

- [ ] **Step 1: Add ONNX dependencies and write failing contract parity test**

Add runtime `onnxruntime>=1.20,<2`; add dev `onnx>=1.17,<2`; refresh the lock.

```python
@pytest.mark.parametrize("adapter", [DeterministicMockVisionAdapter(), onnx_fixture_adapter()])
def test_vision_adapters_emit_complete_execution_contract(adapter, jpeg_frame):
    result = adapter.inspect(jpeg_frame, CONTRACT)
    assert result.execution.model_sha256 == CONTRACT.model_sha256
    assert result.execution.actual_input_shape == (1, 3, 640, 640)
    assert result.execution.preprocessing_version == "letterbox-rgb-v1"
    assert result.execution.postprocessing_version == "nms-v1"
```

- [ ] **Step 2: Run the contract test and verify failure**

Run: `pytest apps/web-backend/tests/modules/inspection/test_vision_contract.py -q`

Expected: FAIL because current `VisionResult` lacks the complete contract.

- [ ] **Step 3: Expand the port and deterministic Mock**

Represent each detection as immutable `Detection(class_id, class_name, confidence, xyxy, spatial_zone)`. Persist both confidence and IoU thresholds, NMS mode, `nms_in_model`, class map version, runtime version/provider, actual input shape, and preprocessing/postprocessing versions.

- [ ] **Step 4: Generate and commit a tiny deterministic ONNX fixture**

`build_fixture_onnx.py` creates a constant-output graph with input `[1,3,640,640]` and output `[1,1,6] = [x1,y1,x2,y2,score,class_id]`. Run it once and commit the resulting small model so CI does not download network assets.

Run: `python apps/web-backend/scripts/build_fixture_onnx.py`

- [ ] **Step 5: Implement ONNX preprocessing, inference, and postprocessing**

Verify model file SHA before creating `InferenceSession(providers=["CPUExecutionProvider"])`. Implement RGB letterbox normalization and both supported output modes. NMS must be deterministic for equal scores by sorting on score then original index. Reject model/config mismatch as `MODEL_CONFIGURATION`.

- [ ] **Step 6: Run parity and ONNX smoke tests**

Run: `pytest apps/web-backend/tests/modules/inspection/test_vision_contract.py apps/web-backend/tests/integration/test_onnx_smoke.py -q`

Expected: PASS without network or GPU.

- [ ] **Step 7: Commit inference adapters**

```bash
git add apps/web-backend/src/odp_api/ports/vision.py apps/web-backend/src/odp_api/adapters/vision apps/web-backend/scripts/build_fixture_onnx.py apps/web-backend/tests/fixtures/models/tiny-detector.onnx apps/web-backend/pyproject.toml apps/web-backend/uv.lock apps/web-backend/tests/modules/inspection/test_vision_contract.py apps/web-backend/tests/integration/test_onnx_smoke.py
git commit -m "feat: run reproducible ONNX inference"
```

---

### Task 7: Compose independent runtime processes

**Files:**
- Modify: `apps/web-backend/src/odp_api/settings.py`
- Modify: `apps/web-backend/src/odp_api/database_roles.py`
- Create: `apps/web-backend/src/odp_api/processes/common.py`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/README.md`
- Test: `apps/web-backend/tests/integration/test_p1_process_settings.py`
- Test: `apps/web-backend/tests/integration/test_p1_compose_contract.py`
- Test: `apps/web-backend/tests/persistence/test_database_roles.py`

**Interfaces:**
- Consumes all P1B process entrypoints.
- Produces Compose services `frame-ingestor`, `outbox-relay`, `inference-worker-1`, `inference-worker-2`, `recovery-scheduler`, `artifact-reconciler`, `stream-retention`.

- [ ] **Step 1: Write failing settings validation tests**

```python
def test_worker_requires_postgres_redis_minio_and_model():
    with pytest.raises(ValidationError):
        WorkerSettings(environment="docker", model_path="")

def test_recovery_budget_is_under_thirty_seconds():
    settings = WorkerSettings.valid_test_instance()
    assert settings.lease_seconds + settings.recovery_loop_seconds + settings.scheduling_margin_seconds <= 30
```

- [ ] **Step 2: Run settings tests and verify failure**

Run: `pytest apps/web-backend/tests/integration/test_p1_process_settings.py -q`

Expected: FAIL because process settings do not exist.

- [ ] **Step 3: Add fail-closed process settings and readiness**

Use distinct settings classes sharing validated database/Redis/MinIO fields. Worker readiness requires database, Redis, MinIO, model load, model SHA, and CPUExecutionProvider checks. Graceful shutdown stops reads, cancels renewal, and leaves uncommitted work unACKed.

- [ ] **Step 4: Add Compose services without changing API responsibility**

Use one built backend image/command surface for all processes. Extend `database_roles.py` with least-privilege `odp_api`, `odp_worker`, `odp_relay`, and `odp_scheduler` grants, then give each process its own role URL. Mount fixed video/model fixtures read-only. Add health checks and `depends_on` only for startup prerequisites, not as a recovery mechanism.

- [ ] **Step 5: Validate Compose process contract**

Run: `docker compose -f deploy/compose.yaml config --quiet`

Run: `pytest apps/web-backend/tests/integration/test_p1_process_settings.py apps/web-backend/tests/integration/test_p1_compose_contract.py -q`

Expected: PASS; service commands point to importable modules and no deployed service uses SQLite task state.

- [ ] **Step 6: Run P1B regression tests**

Run: `ruff check apps/web-backend/src apps/web-backend/tests packages/shared-schemas/src`

Run: `pytest apps/web-backend/tests -q`

Expected: all backend tests pass.

- [ ] **Step 7: Commit runtime composition**

```bash
git add apps/web-backend/src/odp_api/settings.py apps/web-backend/src/odp_api/database_roles.py apps/web-backend/src/odp_api/processes deploy/compose.yaml deploy/README.md apps/web-backend/tests/integration/test_p1_process_settings.py apps/web-backend/tests/integration/test_p1_compose_contract.py apps/web-backend/tests/persistence/test_database_roles.py
git commit -m "feat: compose realtime inference processes"
```
