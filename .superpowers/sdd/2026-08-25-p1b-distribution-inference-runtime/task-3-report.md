# P1B Task 3 — Redis 7 PEL-safe retention and database recovery

## Delivery

- Implementation and tests: `e3713f6bee3aec36f893c0645a7e293e917cc24a`
- Formal-review fixes and regressions: `755014a94a1dde2be5e912acc0475d76078cc842`
- Base: `73b02d611dea24d8d6453ee7fda8d6f0f7f8a75a`
- Scope is limited to Task 3. No Compose wiring, push, merge, or Task 4 work is included.

## RED → GREEN evidence

The initial tests-first command was:

```text
PYTHONPATH=apps/web-backend/src:packages/shared-schemas/src .venv/bin/pytest \
  apps/web-backend/tests/modules/tasks/test_stream_retention.py \
  apps/web-backend/tests/modules/tasks/test_recovery.py \
  apps/web-backend/tests/persistence/test_task_recovery_transactions.py \
  apps/web-backend/tests/integration/test_redis7_pel_retention.py \
  apps/web-backend/tests/persistence/test_postgres_recovery_scheduler.py -q
```

It produced `18 failed, 29 passed, 3 skipped in 1.17s`. The failures were
load-bearing: eleven retention cases failed because the module/contracts did
not exist; `RecoverySummary` still exposed only three counters; the scheduler
and advisory-lock boundary did not exist; and the repository did not expose
Outbox-claim, quarantine-count, or Artifact-reservation recovery. The three
skips were the explicit Redis/PostgreSQL URL gates.

A later tests-first hardening batch moved the local Task 3 selection from 60
tests to `56 passed, 4 failed`. The four RED cases proved the destructive
Gateway-adapter boundary, structured scheduler failure reporting, fail-closed
handling of a corrupt READY row carrying lease markers, and preservation of a
PENDING Evidence Artifact. After the corresponding minimal fixes, local Task 3
reported `60 passed` with four URL-gated service tests skipped; the real
Redis/PostgreSQL Task 3 gate reported `4 passed in 0.39s`.

## Implemented boundaries

- `SafeTrimPlanner` parses Redis IDs as numeric `(milliseconds, sequence)`
  pairs. Empty, malformed, incomplete, or internally inconsistent group/Pending
  data returns no watermark.
- Retention reads every consumer group's `XINFO GROUPS` and `XPENDING` summary,
  chooses smallest Pending before last-delivered progress, caps progress by a
  positive UTC retention floor, and emits only exact
  `XTRIM <stream> MINID = <safe-id>`.
- The inference and canonical alert streams are explicit policies. Temporary
  Gateway cleanup requires both the `odp-alert-gateway:` allowlist and a typed,
  timezone-aware expiry registry; cleanup happens before the alert watermark
  read and never applies to inference groups.
- Retention and Recovery Scheduler contain no `XREADGROUP` or `XAUTOCLAIM` path.
  Worker processes remain the sole Redis delivery owners.
- Recovery Scheduler runs on a two-second cadence and offloads synchronous
  SQLAlchemy work. Its nonblocking, session-level PostgreSQL advisory lock pins
  one connection across the complete six-part multi-transaction sweep and is
  released in `finally`, including error and async cancellation paths.
- Recovery now reports and bounds due retries, stale READY redispatches,
  expired leases, expired unpublished Outbox claims, quarantine count, and
  elapsed Artifact reservations. Mutations use database time, explicit
  `SystemRecoveryScope`, guarded states, deterministic limits, and camera-first
  locking where camera admission state changes.
- Outbox repair only clears expired claims on unpublished rows. Quarantine is
  count-only. Artifact repair clears elapsed reservation anchors, fails only
  non-Evidence PENDING Artifacts, and preserves AVAILABLE/Evidence data.
- Both process loops isolate failed iterations, expose structured summaries,
  and support cooperative shutdown. Task 7 remains responsible for deployed
  process composition and credentials.

## Fresh service verification

The final focused gate used dedicated task-owned PostgreSQL 16 and Redis 7
containers. To avoid reusing accumulated state, it created a new database
`odp_task3_focus_head_sol_20260901`, upgraded it to
`0009_bound_message_quarantine_error (head)`, and used empty Redis logical DB
14. No existing database or Redis data was dropped or reset.

The exact 13-file selection collected 176 tests:

```text
apps/web-backend/tests/integration/test_outbox_relay.py
apps/web-backend/tests/integration/test_redis7_pel_retention.py
apps/web-backend/tests/integration/test_redis_worker_recovery.py
apps/web-backend/tests/modules/inspection/test_effects.py
apps/web-backend/tests/modules/tasks/test_event_envelope.py
apps/web-backend/tests/modules/tasks/test_inference_consumer.py
apps/web-backend/tests/modules/tasks/test_recovery.py
apps/web-backend/tests/modules/tasks/test_stream_retention.py
apps/web-backend/tests/persistence/test_alembic_startup.py
apps/web-backend/tests/persistence/test_p1_task_schema.py
apps/web-backend/tests/persistence/test_postgres_case_deduplication.py
apps/web-backend/tests/persistence/test_postgres_recovery_scheduler.py
apps/web-backend/tests/persistence/test_task_recovery_transactions.py
```

Result: `176 passed in 47.96s`, with zero skips.

An earlier run against a blank, non-migrated database produced
`172 passed, 4 failed in 48.38s`. All four traces were
`DuplicateTable: relation "actors" already exists`: earlier metadata fixtures
had created tables without an Alembic version row, so later migration-owning
tests attempted 0001 again. The hypothesis was verified without a code change
by initializing a second fresh database to Alembic head; the unchanged
176-test command then passed completely.

The prior isolated CI-shaped service gate on the same Task 3 diff collected the
entire backend and produced `376 passed`, zero skips, and one existing
Starlette/httpx deprecation warning in `67.59s`. The final independent local
no-service rerun collected the same 376 tests and produced
`353 passed, 23 explicitly service-gated skips, 1 existing warning in 23.50s`.

## Static and repository gates

- `ruff check apps/web-backend/src apps/web-backend/tests packages/shared-schemas/src`
  → `All checks passed!`
- `python -m compileall -q apps/web-backend/src packages/shared-schemas/src`
  → exit 0
- `UV_CACHE_DIR=/private/tmp/odp-task3-uv-cache uv lock --project apps/web-backend --check`
  → `Resolved 51 packages in 14ms`
- `git diff --cached --check` before the implementation commit → exit 0
- Frozen-boundary source scan found `XTRIM ... MINID =`, `XINFO GROUPS`,
  `XPENDING`, and allowlisted `XGROUP DESTROY`; it found no Task 3
  `XREADGROUP`, `XAUTOCLAIM`, or `MAXLEN` path.

## Limitations and deferred items

- PostgreSQL 16 and Redis 7 service semantics were exercised with dedicated
  test-owned resources in the final gate recorded below. Compose wiring and
  deployed credential provisioning remain Task 7 responsibilities.
- Compose wiring and persistence/source of deployed Gateway expiry leases are
  intentionally deferred to Task 7; Task 3 supplies the typed registry and
  destructive-operation boundary only.
- The existing Starlette/httpx deprecation warning is outside this Task 3 diff.
- The two already accepted Task 2 minors (consumer UUID/BUSYGROUP hardening and
  the unused Worker retry constant/third-failure regression) remain deferred to
  final P1B review.

## Self-review conclusion

The implementation matches the initially frozen Task 3 ownership and
data-safety boundaries. The formal review and its resulting fixes are recorded
below; Task 4 must not begin from this report alone.

## Formal review fix round 1

Review range:
`52597913c3e49c27715b310a9d10659721b50922..755014a94a1dde2be5e912acc0475d76078cc842`.
Seven Important findings were accepted and fixed without rewriting the original
Task 3 commits:

1. repeated cancellation could let `run_once_async` return while its thread was
   still using the session advisory lock;
2. the destructive Gateway adapter accepted an untyped group name instead of
   requiring registry-issued expiry authority;
3. a smallest Pending ID beyond the observed last-delivered ID did not fail
   closed;
4. lease expiry joined and rechecked the camera admission anchor without
   binding `camera_id`;
5. a capacity-blocked retry prefix could consume the candidate limit and starve
   later eligible cameras;
6. the real PostgreSQL suite did not prove lock exclusion while scheduler A was
   blocked inside a repository operation; and
7. PostgreSQL recovery tests could create ORM metadata and sweep residual rows
   on a shared test database.

### Review RED evidence

- Four local regressions together produced `4 failed in 2.40s`: the outer task
  completed after a second cancellation while its worker/lock remained live;
  Pending `2000-0` beyond last-delivered was incorrectly selected as the trim
  watermark; the expiry registry returned a raw `str`; and a blocked prefix at
  the scan limit prevented a later free camera's retry from being released.
- The real Redis 7 rewind regression produced `1 failed in 0.10s`: after
  `XGROUP SETID` rewound last-delivered below Pending, the planner returned
  `2000-0`, trimmed one entry, and deleted the `1000-0` payload.
- Three real PostgreSQL isolation/concurrency regressions produced
  `2 failed, 1 error in 0.43s`: lease expiry mutated through the wrong camera
  anchor, the repeated-cancel scheduler task completed before its advisory-lock
  worker, and the fresh migrated database fixture was not yet available.
- A separate materialized-count regression produced `1 failed in 0.31s`,
  proving that `CameraInferenceState.ready_count` could not safely serve as the
  pre-limit capacity predicate when actual READY rows disagreed.

### Review GREEN implementation

- Cancellation now repeatedly shields and drains the thread-backed worker,
  re-raising cancellation only after the worker's `finally` has released the
  PostgreSQL advisory lock.
- Gateway cleanup now requires an `ExpiredGatewayGroup` capability validated by
  the typed expiry registry. Raw and live values are rejected before Redis I/O.
- Trim planning rejects `smallest_pending_id > last_delivered_id`; the real
  Redis `XGROUP SETID` rewind case preserves the older payload.
- Lease expiry binds organization, task, and camera on both the admission-state
  join and locked task recheck.
- Retry candidate selection uses a correlated count of authoritative READY task
  rows before the bounded limit, not the materialized camera counter. The
  existing camera-first lock and synchronized final recheck remain authoritative
  for the mutation, including missing-state and stale-counter cases.
- Real PostgreSQL recovery tests create a uniquely named database, upgrade it to
  Alembic head, and terminate/drop only that database in `finally`, including
  migration/setup failure paths. They no longer call `Base.metadata.create_all`
  or sweep unrelated residual fixtures.

### Review verification

- Local affected selection: `65 passed in 1.32s`.
- Repeated-cancel regression: `1 passed in 0.23s`.
- Retry starvation plus stale-count regressions: `2 passed in 0.29s`.
- Real Redis 7 retention regressions: `2 passed in 0.10s`.
- Isolated real PostgreSQL recovery file: `6 passed in 1.51s`; a direct query
  found zero remaining `odp_task3_recovery_%` databases afterward.
- PostgreSQL recovery followed immediately by Alembic startup tests:
  `12 passed in 2.14s`, proving no schema-without-version contamination.
- The original 176-test service selection plus nine review regressions used a
  fresh Alembic-head PostgreSQL database and empty Redis database and produced
  `185 passed in 49.31s`, with zero skips.
- The final CI-shaped full backend gate used fresh database
  `odp_task3_fixgreen_full_20260902_b`, runtime-role bootstrap and grants,
  knowledge and Alembic migrations, seed data, and empty Redis logical DB 11.
  It collected 385 tests and produced `385 passed, 1 warning in 69.73s`, with
  zero skips. The sole warning is the existing Starlette/httpx deprecation.
- The no-service full backend gate produced
  `358 passed, 27 explicitly service-gated skips in 24.07s`.
- Full Ruff, compileall, `uv lock --check`, and diff-check gates passed. The
  final frozen-boundary scan found no Task 3 producer-side `MAXLEN` and no
  recovery/retention ownership of `XREADGROUP` or `XAUTOCLAIM`.

### Review conclusion and remaining limitation

Line-by-line self-review found no remaining Important Task 3 defect. The
optional minor improvement to retain/report already successful Gateway-group
destructions when a later cleanup operation raises remains deferred; it does not
weaken the expiry authority or trim-safety boundaries delivered here. Task 4,
push, merge, and Compose integration remain outside this checkpoint.

## Follow-up fix round: cancellation drain and recovery-scan indexes

This follow-up addresses the additional review findings without
changing the frozen Task 3 ownership boundaries.

### Exact RED evidence captured before production changes

- The scheduler barrier regression, run with
  `PYTHONPATH=apps/web-backend/src:packages/shared-schemas/src .venv/bin/pytest apps/web-backend/tests/modules/tasks/test_recovery.py -q`,
  collected nine tests and produced `1 failed, 8 passed in 2.29s`. The
  failure was `assert task.done() is False`: the outer task was already
  cancelled while the lock-owning worker was still blocked.
- The schema/migration index regressions collected three tests and produced
  `2 failed, 1 skipped`. Both local failures showed the expected partial
  indexes were absent; the PostgreSQL plan test was skipped because
  `ODP_POSTGRES_TEST_URL` was not set.
- The exact Gateway annotation regression collected one test and failed because
  `get_type_hints(...)["capability"]` was `ExpiredGatewayGroup | object`, not
  exactly `ExpiredGatewayGroup`.

### Follow-up GREEN implementation

- `RecoveryScheduler.run_once_async()` now uses a non-Task executor Future and
  repeatedly shields/drains it after cancellation, so cancel-all-Tasks cannot
  make the outer coroutine finish before the advisory-lock-owning sweep's
  `finally` block. The stable barrier also asserts that the only new asyncio
  Task is the outer task and cleans up on assertion failure.
- Revision `0010_recovery_query_indexes` and `InferenceTaskRow` metadata add
  status-scoped partial indexes for due retry ordering and READY
  organization/camera probes. `release_due_retries()` uses a bounded correlated
  second-row `EXISTS`/`OFFSET 1` predicate before its fair limit while retaining
  camera-first locking and the authoritative `_sync_ready_count()` recheck.
- `RedisRetentionAdapter.destroy_group()` now exposes the exact
  `ExpiredGatewayGroup` annotation while retaining runtime rejection of raw or
  live capabilities. The PostgreSQL fixture marks cleanup authority before
  `CREATE DATABASE` and uses `DROP DATABASE IF EXISTS` in `finally`.

The affected local selection produced `70 passed, 7 skipped in 1.55s`; the full
backend suite produced `362 passed, 28 skipped, 1 warning in 23.43s`. The
warning is the existing Starlette/httpx deprecation. Ruff, compileall,
`uv lock --check`, and `git diff --check` passed. The new PostgreSQL plan test
loads 100,000 rows with two READY rows, runs `ANALYZE`, leaves the planner at
default settings, and asserts both partial-index names plus no `Seq Scan` in
the JSON plan.

## Final configured service gate for the follow-up

The controller reran the service gates with the correct isolation and runtime
configuration after two intermediate environment-misconfigured attempts. Those
intermediate attempts are not code failures and are excluded from the final
counts.

- Dedicated Redis 7 retention regression: `2 passed`.
- Fresh isolated PostgreSQL 16 `test_postgres_recovery_scheduler.py`:
  `7 passed in 2.47s`. This includes the 100,000-row/2-READY/default-planner
  plan regression, which selected both status-scoped partial indexes and had
  no `Seq Scan`.
- Correctly isolated CI-shaped full backend gate: `390 passed, 1 warning in
  71.45s`, zero skips. It used an owner test database, a runtime app-role
  database URL, and Redis 7. The sole warning remains the existing
  Starlette/httpx deprecation.

Current-diff self-review confirms that the executor Future is not a
discoverable asyncio Task, repeated cancellation drains the lock-owning sweep
before re-raising cancellation, the retry scan is bounded before its fair
limit, and the final camera-first lock plus authoritative `_sync_ready_count()`
recheck remains intact. Migration `0010`, ORM metadata, upgrade/downgrade
coverage, schema predicates, the exact Gateway capability annotation, and the
CREATE DATABASE cleanup-authority guard are all covered by tests. The optional
minor improvement to retain/report already successful Gateway destructions
after a later cleanup error remains deferred; the error is still isolated and
visible to the retention process.

## Follow-up fix round 2: executor cancellation authority

The independent follow-up review found two additional cancellation paths in
the first index/cancellation fix: cancelling the asyncio wrapper Future
directly could make the scheduler appear finished while its executor thread
still held the session advisory lock, and a worker exception during
cancellation draining could mask the original `CancelledError`. These were
treated as load-bearing because the scheduler contract requires the lock
lifetime to cover the complete sweep, including cancellation and error paths.

### RED evidence

- The direct-wrapper regression cancelled the `asyncio.wrap_future()` wrapper
  while the sweep was blocked and observed the outer task finishing before the
  release event.
- The worker-error regression cancelled the outer task, released the blocked
  sweep, and observed the worker `RuntimeError` escape instead of the original
  `CancelledError`.

### Implementation and GREEN evidence

- `RecoveryScheduler` now owns a bounded single-worker executor and retains the
  underlying `concurrent.futures.Future` as the completion authority. Asyncio
  wrappers and cancellation signals are notification mechanisms only; direct
  wrapper cancellation cannot make the scheduler drain stop early.
- Cancellation draining waits on a non-raising completion signal, observes the
  worker result after the underlying Future is done, logs worker failures at
  debug level, and re-raises the outer cancellation. The long-running `run`
  loop closes the executor in `finally`; standalone async tests close it in
  cleanup paths.
- New cancellation coverage (including direct wrapper cancellation and
  cancellation plus worker failure) passes: `11 passed in 0.26s`.
- The no-service full backend rerun after this round passes `364 tests` with
  `28` explicit service-gated skips and the existing Starlette/httpx warning.
- Ruff, compileall, `uv lock --check`, and diff-check pass. The preceding
  service gate remains the verified `390 passed, 1 warning, zero skips` run on
  PostgreSQL 16/Redis 7 before this cancellation-only round; this round could
  not repeat network-backed execution after the Codex usage limit blocked
  escalation.

The earlier index/migration fix remains unchanged and retains its PostgreSQL
7-test and Redis 2-test evidence. No Task 4, Compose wiring, push, or merge is
included in this round.

## Controller service revalidation — 2026-09-07

Docker and approval access were restored. The latest cancellation changes were
verified against PostgreSQL 16 and Redis 7, rather than relying on earlier runs.

- Focused cancellation suite: `11 passed in 0.29s`.
- Fresh isolated database `odp_task3_final_20260907`, migrated to Alembic head:
  complete backend suite `392 passed, 1 warning in 70.86s`, zero skips.
  This includes all seven PostgreSQL scheduler tests and both Redis retention tests.
- Ruff, compileall, `uv lock --project apps/web-backend --check`, and
  `git diff --check` passed.
- An initial run against the previously used test database produced
  `391 passed, 1 failed`: the case-transition fixture reused a case already in
  `IN_REVIEW`. The fresh isolated run resolved that fixture-state contamination
  without changing production code. Integration reruns must use a fresh test DB.
- The sole warning remains the existing Starlette/httpx deprecation.
- Independent Luna Max scoped review of `2247df4..914e950`: APPROVE,
  no blocking findings. Reviewer ran the 11 focused tests and 100 iterations
  each of repeated cancellation and cancellation with worker failure.
  Standalone async callers must close the scheduler; `run()` does so automatically.

Implementation commit: `914e950`. Task 3 is complete after this scoped review
and the fresh service gate. P1 as a whole remains under development.
