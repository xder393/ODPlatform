# P1B Task 2 — Inference consumer and Worker recovery loop

## Checkpoint status

This checkpoint covers the unit-level implementation and verification of the
Redis inference consumer and Worker orchestration. The real Redis/PostgreSQL
recovery tests were intentionally not executed in this bounded checkpoint;
they remain required before Task 2 can be considered fully verified.

## TDD evidence

The first test run was deliberately made before the production implementation
and failed during collection with:

```text
ModuleNotFoundError: No module named 'odp_api.processes.inference_worker'
```

The implementation was then added behind the existing TaskExecutionPort and
InspectionEffectService boundaries. The unit suite now exercises the real
Worker orchestration rather than a duplicate task state machine.

## Implemented behavior

- Redis consumer-group setup tolerates `BUSYGROUP` only, preserves a stable
  process UUID consumer name, parses new deliveries and `XAUTOCLAIM` response
  shapes, and returns deliberate ACK counts.
- Worker delivery parsing is bounded by the canonical 64 KiB envelope limit;
  malformed, oversized, unsupported-schema, and unsupported-event deliveries
  are quarantined before ACK when authoritative metadata is available.
- Tenant/task data is reloaded from the authoritative task port. Terminal or
  stale dispatch references ACK without creating an Attempt; a live foreign
  lease remains pending.
- Claimed work starts a 5-second renewal loop, enforces a 10-second inference
  deadline, classifies timeout/model/input/infra failures, and records failure
  before ACK.
- Successful work commits through `InspectionEffectService.publish()` before
  ACK. Cancellation and lost/stale leases do not ACK. Renewal tasks are
  stopped and awaited on every exit path.
- Worker-owned recovery uses `XAUTOCLAIM` with a 20-second idle threshold and
  feeds reclaimed deliveries through the same processing path.

## Verification

Command:

```text
uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas pytest apps/web-backend/tests/modules/tasks/test_inference_consumer.py -q
```

Result: `8 passed in 0.23s`.

Command:

```text
uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas ruff check apps/web-backend/src/odp_api/modules/tasks/consumer.py apps/web-backend/src/odp_api/processes/inference_worker.py apps/web-backend/tests/modules/tasks/test_inference_consumer.py
```

Result: `All checks passed!`. The staged diff also passes `git diff --cached
--check`.

## Remaining verification and concerns

- `tests/integration/test_redis_worker_recovery.py` was not run in this
  checkpoint, as requested. It must run against the CI-provisioned
  `ODP_POSTGRES_TEST_URL` and `ODP_REDIS_TEST_URL` services and verify Worker A
  cancellation, Worker B `XAUTOCLAIM` recovery, commit-before-ACK redelivery,
  and the one-RUNNING-per-camera invariant.
- P1A fencing/effects regressions, the expanded Ruff scope, the full backend
  suite, and the lockfile check remain for the parent integration checkpoint.
- Production MinIO and ONNX adapters are intentionally not hard-coded here;
  artifact loading and inference remain injected ports for Tasks 4/6/7.

## Integration checkpoint (2026-08-30)

The recovery fixture initially produced a genuine PostgreSQL RED: Alembic head
enforced `frame_artifacts.stream_session_id`, but the fixture added parent and
child rows together while these models intentionally have no ORM relationships.
The Task 2 fixture now flushes `inspection_sessions` before its child rows. The
first isolated real run then passed both recovery tests in `41.03s`; after
adding the final ACK/PEL/Case assertions, the same run passed `2 passed in
41.00s` against PostgreSQL `0008_numeric_outbox_schema_version (head)` and
Redis 7 on isolated local service ports.

The two crash windows are now explicit: Worker A cancellation leaves a Redis
PEL entry and no ACK, Worker B `XAUTOCLAIM`s it and produces exactly one Result
and one Case across two fenced Attempts; the separate post-commit crash is
redelivered as a terminal duplicate, ACKed, removes the PEL entry, preserves
one Result/Case, and creates no new Attempt. Each test uses independent
SQLAlchemy sessions through the repository/effect adapters, propagates the
awaited process exception, and bounds recovery with the 20-second idle wait
inside the documented 30-second budget.

The bounded no-service run collected all Task 2 tests cleanly:

```text
8 passed, 2 skipped in 0.38s
```

The three existing P1A PostgreSQL files were temporarily edited while running
the broad verification matrix: parent-FK flushes exposed a fixture ordering
issue, the fencing command hash exposed a fixture mismatch, and camera
admission needed a retry for its documented transient `ADMISSION_IN_PROGRESS`
race. Those edits were verification-only and unrelated to Task 2, so they were
reverted. The retained Task 2-only fixture change is the parent-first flush
required for this recovery integration test to run against PostgreSQL.

Changed-file PostgreSQL tests correctly collect and skip without CI services:
`8 skipped in 0.27s`. Task 2 production, unit, and integration-test Ruff
checks pass (`All checks passed!`).

The parent integration checkpoint still needs to rerun the broad P1A/full
backend matrix, expanded repository Ruff, and `uv lock --check` against a
fresh CI-style database after this narrowed diff. No final Task 2 completion is
claimed in this checkpoint.

## Final verification checkpoint (2026-08-30)

Verification was rerun from clean `HEAD` (`9339aac`) with the existing
isolated services checked healthy (`pg_isready`, Redis `PONG`) and Alembic
reporting `0008_numeric_outbox_schema_version (head)`. The Task 2 unit plus
URL-gated integration command produced `8 passed, 2 skipped in 0.35s`; the
single permitted real-service recovery rerun produced `2 passed in 40.91s`.

The Task 1/P1A envelope, relay, effects, recovery, and fencing command
collected 97 tests and produced `92 passed, 5 failed in 10.26s`. All five
failures are unchanged `test_postgres_fencing.py` fixture inserts rejected by
PostgreSQL because parent `inspection_sessions` rows are not flushed before
child artifacts. They are pre-existing baseline warnings and no P1A files
were changed in this checkpoint. Without the CI PostgreSQL URL, those three
PostgreSQL test files collect as `8 skipped in 0.27s`.

The fresh Alembic-head full backend command collected 290 tests and produced
`282 passed, 7 failed, 1 skipped in 63.29s`: the same five fencing fixture
failures, the existing camera-admission race test, and the existing case
replay FK-order fixture. The one local skip is the runtime-role test requiring
`ODP_POSTGRES_APP_TEST_URL`; CI provisions that separate role. These failures
are outside the Task 2 diff and are retained as explicit verification
warnings.

Expanded Ruff over `apps/web-backend/src`, `apps/web-backend/tests`, and
`packages/shared-schemas/src` passed (`All checks passed!`); `uv lock --check`
passed (`Resolved 51 packages`); and `git diff --check` passed. A final review
of the 1,040-line Worker found no pytest/unittest imports, fake state machine,
or test-only production machinery. Its size is justified by the typed adapter
ports, bounded/quarantine parsing, multiple injected adapter call-shapes,
lease renewal/fencing lifecycle, failure normalization, and Redis response
normalization. No production duplication/refactor was warranted.

Task 2 remains `DONE_WITH_CONCERNS` until the pre-existing PostgreSQL fixture
warnings are resolved or explicitly accepted by the parent integration gate.
