# Ingestion Session Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover abandoned ingestion sessions without accepting stale-owner writes, repeating persisted frame numbers, or reviving stopped sessions.

**Architecture:** PostgreSQL owns expiring session leases and monotonically increasing generations. Admission, upload completion and reconciliation enforce the same ownership boundary; source sequence high-water marks survive process restarts. Existing task fencing and business effects remain unchanged.

**Tech Stack:** Python, SQLAlchemy, Alembic, PostgreSQL, asyncio, OpenCV, MinIO, Redis, Docker Compose, pytest and Playwright.

**Spec:** `docs/superpowers/specs/2026-09-09-ingestion-session-recovery-design.md` (approved).

## Global Constraints

- 默认每 5 秒续约，租约 30 秒，轮询 1 秒；配置要求租约至少为续约间隔的三倍。
- 生产时间由 PostgreSQL clock_timestamp() 提供，在获得所需行锁之后取值。
- 新增跨资源事务遵循 camera anchor → session → artifact → task/outbox 的顺序。
- 认领、续约和用户停止操作仅锁 session，不得随后获取 camera 锁。
- STOP_REQUESTED 立即禁止新 reserve/promotion/续约；已受理推理任务允许完成。
- 不支持新旧协议混跑；必须停旧摄取和补偿进程后迁移，再统一启动新版。
- 未知文件 `apps/web-backend/:memory:.ses` 保留，不提交、不删除。
- 不削弱现有权限、任务 fencing、对象完整性校验或录制视频 CI 门禁。
- SQLite 只证明功能分支，PostgreSQL 独立事务证明竞争；外部测试 skip 不是通过。
- 本计划各任务依赖上一项接口。不能并行修改共享 admission/session 代码；可独立复核。
- 中间提交用于审查，不部署；最终全量回归和故障演练通过后才交用户审查。

## Working Context and Commands

Existing isolated workspace: `/Users/xder393/ODPlatform/.worktrees/p1-realtime-inference`.
Branch: `codex/ingestion-session-recovery`; design commit `a230f85`.
All paths below are repository-relative. Do not create another worktree inside this one.
Run backend commands from `apps/web-backend` with:

```sh
PYTHONPATH=src:../../packages/shared-schemas/src .venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
uv lock --check
git -c core.fsmonitor=false diff --check
```

Use the existing project dependencies; no new broker or dependency is required.
Each task records exact RED/GREEN commands and outputs in its review handoff, not invented evidence.

## File Responsibilities

- `adapters/persistence/task_models.py` and migration: durable lease/generation/high-water state.
- `ports/inspection_sessions.py`: immutable internal ownership tokens and session repository boundary.
- `adapters/persistence/inspection_sessions.py`: claim, heartbeat, stop and release transactions.
- New `adapters/persistence/ingestion_ownership.py`: shared locked-row validation and post-lock time helper; does not open transactions or acquire camera locks.
- `ports/tasks.py`, `modules/ingestion/artifacts.py`: transport internal claim through the upload saga.
- `adapters/persistence/task_control.py`: guarded reservation, promotion, cleanup and high-water mutation; no unrelated restructuring.
- `processes/frame_ingestor.py`, `processes/runtime.py`, frame source adapter: lifecycle and generation-aware source construction.
- Tests, Compose drill and CI: demonstrate boundary behavior in real processes.

## Task 1: Persist ownership and sequence state

**Files:** Modify `apps/web-backend/src/odp_api/adapters/persistence/task_models.py` and
`apps/web-backend/src/odp_api/ports/inspection_sessions.py`.
Create `apps/web-backend/alembic/versions/0014_ingestion_session_leases.py` and
`apps/web-backend/tests/persistence/test_ingestion_lease_migration.py`.

**Interfaces:** Produce the following frozen internal values, retaining the existing public session DTO:

```python
@dataclass(frozen=True, slots=True)
class IngestionClaim:
    organization_id: UUID
    camera_id: UUID
    session_id: UUID
    owner_instance_id: UUID
    generation: int

@dataclass(frozen=True, slots=True)
class ClaimedInspectionSession:
    session: InspectionSession
    claim: IngestionClaim
    initial_sequence: int
```

- [ ] Add migration tests that start at 0013, insert a session with artifact sequences 2 and 9, upgrade, and query actual rows. Assert `last_reserved_sequence == 9`, generation 0 and no owner/lease. Also cover an empty session and STOP_REQUESTED preservation.
- [ ] Run `pytest tests/persistence/test_ingestion_lease_migration.py -q`; record failure caused by absent migration/columns before implementation.
- [ ] Add nullable UUID owner, nullable timezone lease and nonnegative BIGINT generation/high-water fields; artifact generation defaults to 0. Use server defaults for numeric columns.
- [ ] Backfill using a correlated aggregate, not application-side iteration:

```sql
UPDATE inspection_sessions
SET last_reserved_sequence = COALESCE((
  SELECT MAX(frame_sequence) FROM frame_artifacts
  WHERE frame_artifacts.stream_session_id = inspection_sessions.session_id
    AND frame_artifacts.organization_id = inspection_sessions.organization_id
    AND frame_artifacts.camera_id = inspection_sessions.camera_id
), 0);
```

- [ ] Add a recovery candidate index on `(status, lease_expires_at, session_id)`. Implement schema downgrade only for stopped test environments; document that production rollback requires the spec's coordinated procedure.
- [ ] Run migration tests on SQLite and PostgreSQL plus `tests/persistence/test_p1_task_schema.py`; verify metadata and migrated schema agree.
- [ ] Commit only schema, DTO and migration tests: `feat: persist ingestion ownership generations`.

## Task 2: Guard session ownership transitions

**Files:** Modify session port/repository; create `adapters/persistence/ingestion_ownership.py` and
`tests/persistence/test_ingestion_leases.py`, `tests/persistence/test_postgres_ingestion_leases.py` under the backend.

**Interfaces:** Repository methods, with no caller-supplied production clock:

```python
claim_available(process_id: str, instance_id: UUID, limit: int) -> list[ClaimedInspectionSession]
renew(claim: IngestionClaim) -> bool
stop_candidates(instance_id: UUID, limit: int) -> list[IngestionClaim]
finish_stop(claim: IngestionClaim) -> bool
finalize_expired_stops(limit: int) -> int
release(claim: IngestionClaim) -> bool
fail_claim(claim: IngestionClaim, error_code: str, detail: str) -> bool
```

Repository accepts a validated lease duration (default 30 seconds) and injectable DB clock for tests.
`release` expires only its matching RUNNING lease; it never changes STOP_REQUESTED to RUNNING.
`finish_stop` accepts its matching STOP_REQUESTED owner even after expiry if not otherwise finalized.
`fail_claim` requires current unexpired RUNNING ownership, preventing stale failure publication.

- [ ] Reuse the real session seeding pattern from `tests/persistence/test_inspection_sessions.py`. Add a mutable injected DB clock and assert the literal boundary sequence below:

```python
first = repository.claim_available("same-name", uuid4(), 1)[0]
assert first.claim.generation == 1
assert repository.claim_available("same-name", uuid4(), 1) == []
clock.current += timedelta(seconds=30)
assert repository.renew(first.claim) is False
second = repository.claim_available("same-name", uuid4(), 1)[0]
assert second.claim.generation == 2
assert repository.renew(first.claim) is False
assert repository.renew(second.claim) is True
```

- [ ] Add cases for stop before claim, expired orphan stop, wrong tenant, wrong instance, wrong generation, release, late failure and invalid lease duration. Run both new suites and capture RED.
- [ ] Implement candidate selection with `FOR UPDATE SKIP LOCKED`; after each candidate lock read fresh DB time and recheck eligibility. Increment generation and return the frozen claim plus stored high-water.
- [ ] Implement shared `database_now(session)` using `clock_timestamp()` on PostgreSQL and UTC-normalized database time on SQLite. Shared `owns_live_session(row, claim, now)` checks all token fields, RUNNING and strict `lease_expires_at > now`.
- [ ] Implement all remaining transitions using locked rows and the shared predicate. `finalize_expired_stops` only affects STOP_REQUESTED with absent/expired ownership, clears owner/lease, and records STOPPED time.
- [ ] Add PostgreSQL tests with two independent session factories and a barrier before claim: total successful claims exactly one. For lock-delay expiry, hold the row lock past lease expiry and prove the waiting renew fails using post-lock time.
- [ ] Run focused tests with configured real PostgreSQL, then commit `feat: recover expired ingestion session leases`.

## Task 3: Fence reservation, publication and compensation

**Files:** Modify backend `ports/tasks.py`, `modules/ingestion/artifacts.py`,
`adapters/persistence/task_control.py`; extend `tests/modules/ingestion/test_artifact_saga.py`,
`tests/persistence/test_artifact_reconciliation.py`; create `tests/persistence/test_ingestion_admission_fencing.py`.

**Interfaces:** `SelectedFrame` and `AdmissionRequest` require `claim: IngestionClaim`.
`complete_upload(..., *, claim: IngestionClaim)` and `fail_upload(..., *, claim: IngestionClaim)`
receive the original caller token. No default token, missing-token compatibility path or process-ID fallback.
Shared ownership helper from Task 2 validates a session already locked by the transaction.

- [ ] Seed a valid claim with real database repository methods, reserve frame 10, advance clock and reclaim, then try old-token completion. Assert `INGESTION_LEASE_LOST` and zero new inference tasks/outbox records.
- [ ] Add independent cases for old-token reserve, cross-tenant/camera token, stopped session, old-token fail_upload, stale reconciliation and a newer reservation that cleanup must not release. Check actual persisted rows, not mock call counts.
- [ ] Run new suite and artifact suites, recording RED before changing production code.
- [ ] In reserve lock camera then session; validate token/time before artifact access. An existing same-generation/same-hash live reservation remains idempotent; otherwise require `frame_sequence > last_reserved_sequence`. Store artifact generation and update high-water in the same commit.
- [ ] In completion, use a non-locking artifact identity lookup only to locate the session; then lock camera → session → artifact and revalidate all identity fields. Do not acquire session after artifact.
- [ ] Apply the same live-generation checks to reconciliation immediately before promotion. Old/stopped/expired candidates become failed with `INGESTION_LEASE_LOST`; clear only an exact matching reservation. Already AVAILABLE artifacts remain untouched.
- [ ] Make object compensation retryable: extend the existing reconciliation candidate/cleanup contract to retain failed-upload object identity until deletion succeeds. Select only unreferenced PROCESSING artifacts failed for this ownership reason; storage deletion retries must not act on AVAILABLE or business-referenced evidence. Add real state-transition tests for deletion failure then success.
- [ ] Verify high-water remains after deleting old artifact rows and after failed transactions; same content in a new sequence is valid, same sequence/different content is rejected.
- [ ] Update all internal callers and test fixtures explicitly to create claims. Run focused suites plus `tests/persistence/test_task_recovery_transactions.py`; commit `fix: fence ingestion artifact publication by generation`.

## Task 4: Wire recovery into the live ingestor

**Files:** Modify backend `processes/frame_ingestor.py`, `processes/runtime.py`, `settings.py`,
`modules/ingestion/service.py`, `adapters/frame_sources/opencv.py`; extend
`tests/modules/ingestion/test_ingestor_lifecycle.py`, `test_capture_shutdown.py`, `test_ingestor.py`;
create `tests/modules/ingestion/test_session_recovery_runtime.py`.

**Interfaces:** Runtime creates one instance UUID per process startup. The source factory consumes
`ClaimedInspectionSession`; OpenCV sources accept keyword `initial_sequence: int = 0`.
`IngestionService.run` requires a claim and copies it into SelectedFrame; no user-facing API accepts claims.
Use settings `ingestion_heartbeat_seconds=5`, `ingestion_lease_seconds=30`, `ingestion_poll_seconds=1`.

- [ ] Add controlled async tests: block source iteration, make renew raise, verify ingestion exits and close occurs; return False from renew and assert identical shutdown. Confirm no old-token failure overwrites a reclaimed session.
- [ ] Add a source test with initial sequence 9 and two real/fake-capture decoded frames: assert sequences `[10, 11]`. Preserve existing read/release serialization test.
- [ ] Run new lifecycle/source cases and capture RED.
- [ ] Replace start-only polling with claim_available, respecting capacity. Track active tasks with claims, not only session IDs. Process owned stop candidates by cancelling and draining their task, closing its source, then finish_stop; separately finalize expired orphan stops.
- [ ] Race ingestion and heartbeat tasks with FIRST_COMPLETED. Observe exceptions, cancel and await the peer, close the source, then conditionally release/fail/finish. Do not let a detached failed heartbeat leave ingestion running. Preserve cancellation propagation.
- [ ] On normal process shutdown cancel/drain all active tasks; release only their matching RUNNING claims after safe close. SIGKILL remains a lease-expiry path. A blocking native read cannot justify unsafe concurrent release.
- [ ] Validate settings: positive finite intervals; lease >= 3 * heartbeat. Reject boolean/nonnumeric values consistently with existing settings conventions.
- [ ] Migrate `run_once` and test doubles to the new port, remove obsolete process-ID-only methods once no callers remain. Run full backend tests and Ruff; commit `feat: resume ingestion after owner loss`.

## Task 5: Prove recovery with independent processes and retain CI coverage

**Files:** Create `apps/web-backend/scripts/verify_ingestor_recovery.py`,
`apps/web-backend/tests/integration/test_ingestor_recovery_probe.py`; modify `.github/workflows/ci.yml`
and `deploy/README.md`; extend PostgreSQL role tests when new operations need explicit grants.

**Interfaces:** Probe requires `ODP_ALLOW_COMPOSE_PROBE=disposable`; accepts phase `prepare`, `verify`
and a JSON state file path inside the shared test workspace. State contains only generated demo
session/organization IDs, first generation and highest committed sequence, never credentials.
It creates sessions through existing API/repository authorization boundaries and observes database facts;
it never directly calls the ingestion/worker loop under test.

- [ ] Test the probe refusing absent opt-in before any mutation, malformed state rejection and nonzero exit when generation/sequence does not advance. Exercise the actual observer functions against a test database, not textual script assertions.
- [ ] Implement prepare to generate a unique persistent test video/session and wait for first committed result; save generation/high-water only after verifying artifact and result linkage. Use bounded waits and print last observed states on failure.
- [ ] Run a disposable Compose drill with the following process boundary (new script path above):

```sh
docker compose -f deploy/compose.yaml exec -T -e ODP_ALLOW_COMPOSE_PROBE=disposable frame-ingestor python /workspace/apps/web-backend/scripts/verify_ingestor_recovery.py prepare --state /workspace/ingestion-recovery-state.json
docker compose -f deploy/compose.yaml kill -s SIGKILL frame-ingestor
docker compose -f deploy/compose.yaml up -d frame-ingestor
docker compose -f deploy/compose.yaml exec -T -e ODP_ALLOW_COMPOSE_PROBE=disposable frame-ingestor python /workspace/apps/web-backend/scripts/verify_ingestor_recovery.py verify --state /workspace/ingestion-recovery-state.json
```

- [ ] Wait for restarted container dependency installation before invoking verify; bound installation wait separately from lease recovery wait. Verify same session, higher generation, higher sequence and new linked result; request stop and ensure it remains STOPPED beyond one lease window.
- [ ] Add the drill to existing E2E CI, with its own step timeout, before browser tests. Always preserve diagnostics and clean up only the job's Compose project; no skip/retry that converts failed assertions to success.
- [ ] Document coordinated upgrade, recorded replay semantics, device locality, stopped-vs-physically-closed distinction and deferred native-I/O bounds. Include actual CI evidence only after it exists.
- [ ] Run full backend with PostgreSQL/Redis/MinIO gates enabled, migration/role tests, frontend unit/build/typecheck/gate and mandatory browser E2E. Treat skipped concurrency/drill tests as incomplete acceptance.
- [ ] Commit `test: verify ingestion crash recovery in compose`; request independent review, fix findings, then submit a PR. Do not auto-merge; user final review remains required.

## Self-review Coverage Map

Spec 1–3: Task 1 types/schema, Task 2 ownership. Spec 4: Task 2 post-lock time and races.
Spec 5: Task 3 publication/compensation fencing and lock order. Spec 6: Tasks 1/3/4 high-water/source.
Spec 7: Tasks 2/4 stop, heartbeat failures and shutdown. Spec 8: Tasks 1/5 migration and deployment.
Spec 9: Tasks 2–5 PostgreSQL races, isolated drill and existing browser regression.
Spec 10: all tasks preserve scope. No production changes have been made by writing this plan.
