# P1A Task Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the PostgreSQL task, admission, fencing, recovery, quarantine, and defect-episode control plane that makes realtime inference business effects recoverable and effectively-once.

**Architecture:** PostgreSQL owns task state and execution authority. Every camera uses one locked scheduling row; Worker ownership uses renewable leases plus monotonic fencing tokens; retries and stale READY recovery create versioned Outbox dispatches. Inspection results, Case deduplication, evidence promotion, Alert Outbox, and audit append commit together.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, pytest, Hypothesis, existing audit/case modules.

**Spec:** `docs/superpowers/specs/2026-08-25-p1-realtime-ai-inference-design.md`

## Global Constraints

- PostgreSQL is the only fact source for task state, execution authority, attempts, published results, inspection events, cases, outbox, and audit.
- Delivery is at-least-once; database constraints and fencing provide effectively-once business effects.
- Per camera: at most one `RUNNING`, at most two `READY`; admission, claim, renewal, and finalize lock the same camera state row.
- Lease duration is 20 seconds; renewal interval is 5 seconds; inference timeout is 10 seconds.
- `dispatch_seq`, `attempt_no`, and `fence_token` are independent monotonic concepts.
- Retryable work has at most three executed attempts; permanent errors do not retry mechanically.
- Unsupported schemas are quarantined, committed, and ACKed; they never remain poison messages in PEL.
- Repository queries always require `organization_id`; cross-tenant access fails closed.
- Use database time for lease decisions. SQLite may support local deterministic tests but cannot be used to prove PostgreSQL locking semantics.

---

### Task 1: Add the P1 domain vocabulary and database schema

**Files:**
- Modify: `apps/web-backend/src/odp_api/modules/tasks/models.py`
- Create: `apps/web-backend/src/odp_api/modules/tasks/commands.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/task_models.py`
- Modify: `apps/web-backend/alembic/env.py`
- Create: `apps/web-backend/alembic/versions/0007_realtime_inference_control_plane.py`
- Test: `apps/web-backend/tests/persistence/test_p1_task_schema.py`

**Interfaces:**
- Produces: `TaskStatus`, `ArtifactState`, `ArtifactLifecycle`, `FailureKind`, `TaskRecord`, `LeaseClaim`, `InferenceExecutionContract`, `PublishInferenceCommand`.
- Produces tables: `camera_inference_state`, `frame_artifacts`, `inference_tasks`, `inference_attempts`, `published_inference_results`, `outbox_events`, `message_quarantine`, `defect_episode`, `inspection_sessions`.

- [ ] **Step 1: Write a failing Alembic schema test**

```python
def test_p1_control_plane_schema_has_required_constraints(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'p1.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine, _ = create_engine_and_session(database_url)
    inspector = inspect(engine)
    expected = {
        "camera_inference_state", "frame_artifacts", "inference_tasks",
        "inference_attempts", "published_inference_results", "outbox_events",
        "message_quarantine", "defect_episode", "inspection_sessions",
    }
    assert expected <= set(inspector.get_table_names())
    assert {item["name"] for item in inspector.get_unique_constraints("inference_attempts")} >= {
        "uq_inference_attempt_task_number", "uq_inference_attempt_task_fence"
    }
    assert {item["name"] for item in inspector.get_unique_constraints("outbox_events")} >= {
        "uq_outbox_task_dispatch_event"
    }
```

- [ ] **Step 2: Run the test and verify the schema is absent**

Run: `pytest apps/web-backend/tests/persistence/test_p1_task_schema.py -q`

Expected: FAIL because revision `0007_realtime_inference_control_plane` and its tables do not exist.

- [ ] **Step 3: Define exact domain enums and immutable command objects**

```python
class TaskStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    DEAD_LETTER = "DEAD_LETTER"
    BLOCKED_COMPATIBILITY = "BLOCKED_COMPATIBILITY"
    SKIPPED_STALE = "SKIPPED_STALE"
    SKIPPED_BACKPRESSURE = "SKIPPED_BACKPRESSURE"

class FailureKind(StrEnum):
    RETRYABLE_INFRA = "RETRYABLE_INFRA"
    INVALID_INPUT = "INVALID_INPUT"
    MODEL_CONFIGURATION = "MODEL_CONFIGURATION"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"

@dataclass(frozen=True, slots=True)
class LeaseClaim:
    task_id: UUID
    organization_id: UUID
    artifact_id: UUID
    attempt_id: UUID
    attempt_no: int
    fence_token: int
    lease_owner: str
    lease_expires_at: datetime
```

`InferenceExecutionContract` must contain `model_release`, `model_sha256`, `onnxruntime_version`, `execution_provider`, `actual_input_shape`, `preprocessing_version`, `postprocessing_version`, `confidence_threshold`, `iou_threshold`, `nms_mode`, `nms_in_model`, and `class_map_version`. `PublishInferenceCommand` must contain the claim, frame SHA-256, detections tuple, stage durations, correlation ID, and database completion time.

- [ ] **Step 4: Add SQLAlchemy rows and migration revision**

Use `Base` from `adapters.persistence.models`. Define named unique/check constraints matching the spec, including:

```python
class InferenceTaskRow(Base):
    __tablename__ = "inference_tasks"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_inference_task_tenant_key"),
        CheckConstraint("dispatch_seq >= 1", name="ck_inference_task_dispatch_positive"),
        CheckConstraint("attempt_count >= 0", name="ck_inference_task_attempt_nonnegative"),
    )
    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    camera_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    artifact_id: Mapped[UUID] = mapped_column(ForeignKey("frame_artifacts.artifact_id"))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Define the other rows with these exact columns:

- `camera_inference_state`: tenant/camera composite unique key, nullable `running_task_id`, `ready_count`, nullable `reservation_id`/`reservation_expires_at`, `last_admitted_at`, and integer `version`.
- `frame_artifacts`: IDs for tenant/camera/session, positive `frame_sequence`, `captured_at`, nullable immutable `object_key`, SHA-256, nullable content length, state, lifecycle, retention, error code, and timestamps; unique tenant/camera/session/sequence.
- `inference_attempts`: attempt/task/tenant/worker IDs, `attempt_no`, `fence_token`, start/finish, outcome, error, and duration; named unique task/attempt and task/fence constraints.
- `published_inference_results`: unique task/attempt IDs, Artifact ID, every execution-contract field listed in Step 3, JSON detections/stage durations, frame SHA-256, and `published_at`.
- `outbox_events`: Outbox/aggregate/task IDs, nullable `dispatch_seq`, event type/schema/payload, availability, claim owner/expiry, publish attempts/time, error, and creation time; named unique task/dispatch/event constraint.
- `message_quarantine`: stream/message/event IDs, event type/schema, bounded binary payload, error, nullable task/tenant IDs, status, and timestamps.
- `defect_episode`: tenant/camera/defect type/non-null spatial zone (`GLOBAL` fallback), current Case ID, expiry, and timestamps; unique tenant/camera/defect/zone.
- `inspection_sessions`: session/tenant/camera/line IDs, source type, sanitized URI, nullable secret reference, status, tenant-scoped idempotency key, heartbeat/error, and timestamps.

Extend `inspection_events` with nullable `source_result_id` and `evidence_artifact_id`, then create unique `source_result_id` after existing rows remain valid. Import `task_models` in `alembic/env.py` so offline and runtime metadata include the rows.

- [ ] **Step 5: Verify clean SQLite and PostgreSQL DDL**

Run: `pytest apps/web-backend/tests/persistence/test_p1_task_schema.py apps/web-backend/tests/persistence/test_alembic_startup.py -q`

Expected: PASS, including PostgreSQL offline DDL rendering.

- [ ] **Step 6: Commit the schema slice**

```bash
git add apps/web-backend/src/odp_api/modules/tasks apps/web-backend/src/odp_api/adapters/persistence/task_models.py apps/web-backend/alembic apps/web-backend/tests/persistence/test_p1_task_schema.py
git commit -m "feat: add realtime inference control schema"
```

---

### Task 2: Implement camera admission before object upload

**Files:**
- Modify: `apps/web-backend/src/odp_api/ports/tasks.py`
- Create: `apps/web-backend/src/odp_api/modules/tasks/admission.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/task_control.py`
- Test: `apps/web-backend/tests/modules/tasks/test_camera_admission.py`
- Test: `apps/web-backend/tests/persistence/test_postgres_camera_serialization.py`

**Interfaces:**
- Consumes: `ArtifactState`, `ArtifactLifecycle`, `TaskStatus` from Task 1.
- Produces: `AdmissionRequest`, `AdmissionReservation`, `AdmissionRejected`, `CameraAdmissionPort.reserve()`, `complete_upload()`, `fail_upload()`.

- [ ] **Step 1: Write failing policy tests for sampling and overload rejection**

```python
def test_sampler_selects_two_frames_per_second_from_ten_fps():
    sampler = FrameSampler(target_fps=2)
    selected = [index for index in range(10) if sampler.accept(index / 10)]
    assert selected == [0, 5]

def test_admission_rejects_before_upload_when_worker_is_unhealthy():
    decision = AdmissionPolicy(frame_ttl_seconds=2).evaluate(
        ready_count=0, oldest_ready_age_seconds=0, worker_healthy=False,
        redis_available=True, frame_age_seconds=0.1,
    )
    assert decision.reason == "WORKER_UNHEALTHY"
```

- [ ] **Step 2: Run the policy tests and verify failure**

Run: `pytest apps/web-backend/tests/modules/tasks/test_camera_admission.py -q`

Expected: FAIL because admission types and policy do not exist.

- [ ] **Step 3: Implement deterministic sampling and admission policy**

`AdmissionPolicy.evaluate()` must reject when the frame TTL budget is exhausted, Redis is unavailable, no Worker is healthy, or the database oldest READY age is already at the freshness cutoff. It must not use `XLEN` as its sole signal.

```python
@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    organization_id: UUID
    camera_id: UUID
    stream_session_id: UUID
    frame_sequence: int
    captured_at: datetime
    content_sha256: str
    correlation_id: UUID

class CameraAdmissionPort(Protocol):
    def reserve(self, request: AdmissionRequest, now: datetime) -> AdmissionReservation: ...
    def complete_upload(self, reservation_id: UUID, object_key: str, content_length: int, now: datetime) -> TaskRecord: ...
    def fail_upload(self, reservation_id: UUID, error_code: str, now: datetime) -> None: ...
```

- [ ] **Step 4: Write a failing PostgreSQL concurrency test**

Start two transactions against the same camera with a barrier. Assert both reservations serialize, no more than two READY tasks remain, and any evicted row changed only from READY to `SKIPPED_BACKPRESSURE`. Mark the test with the existing `ODP_POSTGRES_TEST_URL` skip convention.

- [ ] **Step 5: Implement `SqlAlchemyTaskControlRepository.reserve()`**

In one transaction: upsert and `SELECT ... FOR UPDATE` `camera_inference_state`; conditionally update the oldest task with `WHERE status = 'READY'`; insert `FrameArtifact(PENDING)` as the admission reservation. `complete_upload()` locks the same row, changes Artifact to AVAILABLE, creates `InferenceTask(READY, dispatch_seq=1)`, and inserts the matching Task Outbox atomically.

- [ ] **Step 6: Run unit and PostgreSQL admission tests**

Run: `pytest apps/web-backend/tests/modules/tasks/test_camera_admission.py apps/web-backend/tests/persistence/test_postgres_camera_serialization.py -q`

Expected: PASS; PostgreSQL test must execute in CI, not skip there.

- [ ] **Step 7: Commit camera admission**

```bash
git add apps/web-backend/src/odp_api/ports/tasks.py apps/web-backend/src/odp_api/modules/tasks/admission.py apps/web-backend/src/odp_api/adapters/persistence/task_control.py apps/web-backend/tests/modules/tasks/test_camera_admission.py apps/web-backend/tests/persistence/test_postgres_camera_serialization.py
git commit -m "feat: serialize per-camera frame admission"
```

---

### Task 3: Implement fenced claim, lease renewal, and finalize

**Files:**
- Modify: `apps/web-backend/src/odp_api/ports/tasks.py`
- Create: `apps/web-backend/src/odp_api/modules/tasks/execution.py`
- Modify: `apps/web-backend/src/odp_api/adapters/persistence/task_control.py`
- Test: `apps/web-backend/tests/modules/tasks/test_fenced_execution.py`
- Test: `apps/web-backend/tests/persistence/test_postgres_fencing.py`

**Interfaces:**
- Produces: `TaskExecutionPort.claim(task_id, organization_id, worker_id, now) -> LeaseClaim | None`.
- Produces: `renew(claim, now) -> LeaseClaim | None`, `complete_no_defect(command)`, `record_failure(claim, failure, now)`.
- Lease constants: `LEASE_SECONDS = 20`, `RENEW_INTERVAL_SECONDS = 5`, `INFERENCE_TIMEOUT_SECONDS = 10`.

- [ ] **Step 1: Write failing claim and duplicate-delivery tests**

```python
def test_duplicate_delivery_during_live_lease_does_not_create_attempt(repository, ready_task, now):
    first = repository.claim(ready_task.task_id, ready_task.organization_id, "worker-a", now)
    duplicate = repository.claim(ready_task.task_id, ready_task.organization_id, "worker-b", now)
    assert first is not None
    assert duplicate is None
    assert repository.attempt_count(ready_task.task_id) == 1
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `pytest apps/web-backend/tests/modules/tasks/test_fenced_execution.py -q`

Expected: FAIL because the execution port is not implemented.

- [ ] **Step 3: Implement claim under the camera row lock**

Claim must lock `camera_inference_state`, require no live `running_task_id`, conditionally move READY to RUNNING, increment `attempt_count` and `fence_token`, set a 20-second database-time lease, set the camera running task, and append exactly one Attempt in the same transaction.

- [ ] **Step 4: Write failing renewal and stale-finalize tests**

```python
def test_renewal_keeps_fence_token_and_extends_lease(repository, claim, now):
    renewed = repository.renew(claim, now + timedelta(seconds=5))
    assert renewed is not None
    assert renewed.fence_token == claim.fence_token
    assert renewed.lease_expires_at > claim.lease_expires_at

def test_expired_worker_cannot_publish_result(repository, expired_claim, publish_command):
    with pytest.raises(StaleLease):
        repository.publish_success(replace(publish_command, claim=expired_claim))
```

- [ ] **Step 5: Implement guarded renewal and finalize**

Every update must match task ID, tenant, owner, token, RUNNING status, and unexpired lease according to `SELECT now()`/`CURRENT_TIMESTAMP`. Finalize locks the camera row, writes Attempt outcome and Task terminal state, releases `running_task_id`, and rejects a zero-row guarded update with `StaleLease`.

- [ ] **Step 6: Prove two Workers cannot run the same camera concurrently**

Run: `pytest apps/web-backend/tests/persistence/test_postgres_fencing.py -q`

Expected: PASS for concurrent claim, renewal, expired lease, and late finalize cases.

- [ ] **Step 7: Commit fenced execution**

```bash
git add apps/web-backend/src/odp_api/ports/tasks.py apps/web-backend/src/odp_api/modules/tasks/execution.py apps/web-backend/src/odp_api/adapters/persistence/task_control.py apps/web-backend/tests/modules/tasks/test_fenced_execution.py apps/web-backend/tests/persistence/test_postgres_fencing.py
git commit -m "feat: add renewable fenced inference leases"
```

---

### Task 4: Add retry scheduling, stale READY redispatch, and schema quarantine

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/tasks/recovery.py`
- Modify: `apps/web-backend/src/odp_api/adapters/persistence/task_control.py`
- Test: `apps/web-backend/tests/modules/tasks/test_recovery.py`
- Test: `apps/web-backend/tests/persistence/test_task_recovery_transactions.py`

**Interfaces:**
- Produces: `RecoveryService.run_once(now) -> RecoverySummary`.
- Produces repository operations `release_due_retries`, `redispatch_stale_ready`, `expire_leases`, `quarantine_message`, `replay_compatibility`.
- `REDISPATCH_AFTER_SECONDS = 10`; retry delays are 1 and 2 seconds before the third executed failure becomes Dead Letter.

- [ ] **Step 1: Write failing retry responsibility tests**

```python
def test_worker_failure_waits_and_scheduler_creates_next_dispatch(repository, claim, now):
    repository.record_failure(claim, FailureKind.RETRYABLE_INFRA, "minio timeout", now)
    waiting = repository.get(claim.task_id, claim.organization_id)
    assert waiting.status is TaskStatus.RETRY_WAIT
    assert waiting.dispatch_seq == 1
    assert repository.release_due_retries(now + timedelta(seconds=1)) == 1
    ready = repository.get(claim.task_id, claim.organization_id)
    assert ready.status is TaskStatus.READY
    assert ready.dispatch_seq == 2
```

- [ ] **Step 2: Run the recovery tests and verify failure**

Run: `pytest apps/web-backend/tests/modules/tasks/test_recovery.py -q`

Expected: FAIL because scheduler transitions do not exist.

- [ ] **Step 3: Implement retry and stale READY transactions**

`record_failure()` writes `RETRY_WAIT + next_attempt_at` under current fencing. Scheduler uses `FOR UPDATE SKIP LOCKED`; each due retry or READY older than 10 seconds receives `dispatch_seq + 1` and a new unique Task Outbox. Permanent errors become Dead Letter immediately. Backpressure/stale statuses never increment model failure counters.

- [ ] **Step 4: Write a failing poison-message test**

```python
def test_unsupported_schema_is_quarantined_and_ackable(repository, ready_task, now):
    result = repository.quarantine_message(
        stream="inference.tasks", message_id="17-0", event_id=uuid4(),
        event_type="vision.inference.requested.v9", schema_version=9,
        raw_payload=b'{"task_id":"bad-version"}', task_id=ready_task.task_id,
        organization_id=ready_task.organization_id, now=now,
    )
    assert result.ack_after_commit is True
    assert repository.get(ready_task.task_id, ready_task.organization_id).status is TaskStatus.BLOCKED_COMPATIBILITY
```

- [ ] **Step 5: Implement quarantine and explicit replay**

Cap stored raw payload at 64 KiB. Insert quarantine record and guarded Task transition in one transaction. `replay_compatibility()` requires BLOCKED status, changes it to READY, increments `dispatch_seq`, inserts a new Outbox, and writes an audit command; it never reuses the old Redis message.

- [ ] **Step 6: Run recovery transaction tests**

Run: `pytest apps/web-backend/tests/modules/tasks/test_recovery.py apps/web-backend/tests/persistence/test_task_recovery_transactions.py -q`

Expected: PASS for retry, third-attempt dead letter, lost-Stream redispatch, quarantine, and compatibility replay.

- [ ] **Step 7: Commit recovery control**

```bash
git add apps/web-backend/src/odp_api/modules/tasks/recovery.py apps/web-backend/src/odp_api/adapters/persistence/task_control.py apps/web-backend/tests/modules/tasks/test_recovery.py apps/web-backend/tests/persistence/test_task_recovery_transactions.py
git commit -m "feat: recover durable inference dispatches"
```

---

### Task 5: Commit inference result, defect episode, Case, evidence, alert, and audit atomically

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/inspection/effects.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/inspection_effects.py`
- Modify: `apps/web-backend/src/odp_api/adapters/persistence/unit_of_work.py`
- Modify: `apps/web-backend/src/odp_api/adapters/persistence/repositories.py`
- Test: `apps/web-backend/tests/modules/inspection/test_effects.py`
- Test: `apps/web-backend/tests/persistence/test_postgres_case_deduplication.py`

**Interfaces:**
- Consumes: `PublishInferenceCommand`, current `AuditService`, P0 Case/Event rows.
- Produces: `InspectionEffectService.publish(command) -> PublishedEffect`.
- Produces: `PublishedEffect(result_id, event_id | None, case_id | None, alert_outbox_id | None)`.

- [ ] **Step 1: Write failing no-defect and defect-effect tests**

```python
def test_no_defect_publishes_result_without_case(service, valid_command):
    effect = service.publish(replace(valid_command, detections=()))
    assert effect.event_id is None
    assert effect.case_id is None

def test_same_result_cannot_create_two_events(service, defect_command):
    first = service.publish(defect_command)
    second = service.publish(defect_command)
    assert second.event_id == first.event_id
    assert second.case_id == first.case_id
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest apps/web-backend/tests/modules/inspection/test_effects.py -q`

Expected: FAIL because the effect service and persistence adapter do not exist.

- [ ] **Step 3: Implement the fenced effect transaction**

Lock camera state and Task, validate owner/token/unexpired lease, finish Attempt, insert unique Published Result, and set Task SUCCEEDED. For a defect, lock/upsert the `defect_episode` key `(organization_id, camera_id, defect_type, spatial_zone)`, link an open unexpired episode or create a Case, insert unique Inspection Event, promote Artifact to EVIDENCE, append durable alert fact and Alert Outbox, and append hash-chain audit before commit.

- [ ] **Step 4: Write the PostgreSQL concurrent episode test**

Use two task claims and a `Barrier` to publish the same defect window concurrently. Assert two Results and Events but exactly one Case and one active episode. Also assert every Event has its own Alert Outbox while the UI deduplicates by Case/alert ID.

- [ ] **Step 5: Run effect and audit regression tests**

Run: `pytest apps/web-backend/tests/modules/inspection/test_effects.py apps/web-backend/tests/persistence/test_postgres_case_deduplication.py apps/web-backend/tests/persistence/test_case_audit_uow.py -q`

Expected: PASS with no regression to manual Case transitions or audit ordering.

- [ ] **Step 6: Commit atomic business effects**

```bash
git add apps/web-backend/src/odp_api/modules/inspection/effects.py apps/web-backend/src/odp_api/adapters/persistence/inspection_effects.py apps/web-backend/src/odp_api/adapters/persistence/unit_of_work.py apps/web-backend/src/odp_api/adapters/persistence/repositories.py apps/web-backend/tests/modules/inspection apps/web-backend/tests/persistence/test_postgres_case_deduplication.py
git commit -m "feat: publish fenced inspection effects"
```

---

### Task 6: Add control-plane property tests and preserve P0 compatibility

**Files:**
- Modify: `apps/web-backend/pyproject.toml`
- Modify: `apps/web-backend/uv.lock`
- Test: `apps/web-backend/tests/modules/tasks/test_delivery_properties.py`
- Modify: `apps/web-backend/tests/modules/tasks/test_service.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes all P1A ports and repositories.
- Produces a CI gate proving effectively-once effects under duplicate delivery orders.

- [ ] **Step 1: Add Hypothesis and write the failing property test**

Add `hypothesis>=6.112,<7` to the backend `dev` extra and refresh the lock.

```python
@given(st.lists(st.sampled_from(["deliver", "renew", "expire", "complete"]), min_size=1, max_size=20))
def test_any_duplicate_delivery_order_has_at_most_one_published_result(actions, harness):
    for action in actions:
        harness.apply(action)
    assert harness.published_result_count() <= 1
    assert harness.inspection_event_count() <= 1
```

- [ ] **Step 2: Run the property test and verify it exposes missing harness behavior**

Run: `pytest apps/web-backend/tests/modules/tasks/test_delivery_properties.py -q`

Expected: FAIL until the harness drives the real repository transitions and stale operations are rejected.

- [ ] **Step 3: Complete the deterministic repository harness**

Use a migrated SQLite database for state-order generation and retain PostgreSQL concurrency tests for locking behavior. Every action must call the same application/repository methods used by runtime code; do not create a second fake state machine.

- [ ] **Step 4: Keep legacy local task tests passing during migration**

Update P0 assertions only where the Frozen P1 vocabulary intentionally replaces `PENDING/RETRYING/FAILED` with `READY/RETRY_WAIT/SKIPPED_*`. Do not remove SQLite/local adapters until P1B switches all deployed process composition.

- [ ] **Step 5: Expand backend CI lint and test commands**

Change the narrow Ruff command to:

```yaml
- name: Lint web backend
  run: ruff check apps/web-backend/src apps/web-backend/tests
```

Keep `pytest apps/web-backend/tests -q` and PostgreSQL service environment unchanged.

- [ ] **Step 6: Run the P1A verification set**

Run: `ruff check apps/web-backend/src apps/web-backend/tests`

Run: `pytest apps/web-backend/tests/modules/tasks apps/web-backend/tests/modules/inspection apps/web-backend/tests/persistence -q`

Expected: all tests pass; PostgreSQL-marked tests execute when `ODP_POSTGRES_TEST_URL` is set.

- [ ] **Step 7: Commit the P1A control-plane gate**

```bash
git add apps/web-backend/pyproject.toml apps/web-backend/uv.lock apps/web-backend/tests .github/workflows/ci.yml
git commit -m "test: prove inference control-plane invariants"
```
