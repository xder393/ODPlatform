# Task 3 report: ingestion admission/publication/compensation fencing

Date: 2026-09-10
Branch: `codex/ingestion-session-recovery`
Base: `84d6eb4`

## Scope delivered

- Required real `IngestionClaim` transport through `SelectedFrame`,
  `AdmissionRequest`, `complete_upload`, and `fail_upload`; removed permissive
  token fallbacks.
- Reserve now locks camera -> session, validates a live claim using a
  post-lock database timestamp, stores artifact generation, and advances the
  session sequence high-water in the same transaction.
- Completion/failure now locate an artifact without a lock, then lock camera ->
  session -> artifact and revalidate tenant, camera/session, generation, live
  lease, and exact reservation before changing state or creating task/outbox
  rows.
- Reconciliation applies the same generation/lease fence before promotion and
  terminates stale PENDING rows as `INGESTION_LEASE_LOST`, clearing only an
  exact reservation.
- Ownership-failed PROCESSING artifacts are retained as retryable cleanup
  candidates with deterministic object identity; cleanup rechecks references,
  live tasks, and exact reservations before marking `DELETED`.
- Minimal ingestor/runtime wiring now carries repository-issued claims into
  ingestion. Stop/lifecycle redesign remains Task 4.

## TDD evidence

The first regression was written against the real SQLite repositories and
seeded via `claim_available`.

RED command (before interface changes):

```text
PYTHONPATH=src:../../packages/shared-schemas/src /tmp/odp-lock-check.4s64E6/venv/bin/pytest -q tests/persistence/test_ingestion_admission_fencing.py::test_old_generation_cannot_complete_upload_after_session_reclaim
```

Observed RED: `TypeError: AdmissionRequest.__init__() got an unexpected keyword argument 'claim'`.

After making the claim mandatory and updating the test caller, the same command
remained RED with `Failed: DID NOT RAISE AdmissionRejected`; the stale
completion was still able to publish until the repository fence was added.

GREEN for that first regression:

```text
1 passed
```

The independent stale-reconciliation regression was likewise observed RED
(`AssertionError: assert not True`) before the generation check, then GREEN
after the transition fence. The cleanup race was written against persisted
FAILED/PROCESSING and reservation rows and now covers delete failure followed
by retry success.

## Verification

Focused Task 3 and related recovery suites:

```text
PYTHONPATH=src:../../packages/shared-schemas/src /tmp/odp-lock-check.4s64E6/venv/bin/pytest -q \
 tests/modules/ingestion/test_artifact_reconciler.py \
 tests/modules/ingestion/test_artifact_saga.py \
 tests/persistence/test_ingestion_admission_fencing.py \
 tests/persistence/test_artifact_reconciliation.py \
 tests/modules/ingestion/test_ingestor.py \
 tests/modules/ingestion/test_tenant_health.py \
 tests/modules/ingestion/test_ingestor_lifecycle.py \
 tests/modules/tasks/test_delivery_properties.py \
 tests/persistence/test_task_recovery_transactions.py
```

Result: `77 passed in 11.24s`.

Static/diff checks:

```text
/tmp/odp-lock-check.4s64E6/venv/bin/ruff check src tests
All checks passed!

git -c core.fsmonitor=false diff --check
clean (no output)
```

Full backend run:

```text
PYTHONPATH=src:../../packages/shared-schemas/src /tmp/odp-lock-check.4s64E6/venv/bin/pytest -q
```

The sandboxed run reached `471 passed, 61 skipped, 1 failed` in `29.75s`.
The sole failure was the pre-existing environment-sensitive
`tests/integration/test_redis_worker_liveness.py::test_refused_redis_connection_is_not_reported_as_worker_offline_only` failing to
bind its intentionally refused local ephemeral socket with
`PermissionError: [Errno 1] Operation not permitted`. The same test rerun with
local socket permission passed: `1 passed in 1.27s`.

The controller then reran the full backend suite with elevated local service
permissions:

```text
PYTHONPATH=src:../../packages/shared-schemas/src /tmp/odp-lock-check.4s64E6/venv/bin/pytest -q
```

Result: `472 passed, 61 skipped, 1 upstream Starlette warning in 32.73s`,
exit 0.

The local worktree environment had `ODP_POSTGRES_TEST_URL` unset, so its full
run skipped PostgreSQL-only tests and did not use the default `25433` service.
The controller then ran the dedicated `25434` database with:

```text
ODP_POSTGRES_TEST_URL=dedicated25434 PYTHONPATH=src:../../packages/shared-schemas/src /tmp/odp-lock-check.4s64E6/venv/bin/pytest -q tests/persistence/test_postgres_camera_serialization.py tests/persistence/test_postgres_ingestion_leases.py
```

Result: `3 passed in 1.18s`.

## Files changed

Production: `ports/tasks.py`, `modules/ingestion/artifacts.py`,
`modules/ingestion/reconciler.py`, `modules/ingestion/service.py`,
`adapters/persistence/task_control.py`, `processes/frame_ingestor.py`, and
`processes/runtime.py`.

Tests: the new
`tests/persistence/test_ingestion_admission_fencing.py`, plus claim fixture and
caller updates in artifact, ingestor, task-delivery, reconciliation,
PostgreSQL-serialization, and recorded-video tests.

## Self-review / concerns

- The shared `database_now` and `owns_live_session` helpers are used after the
  required locks for every admission/publication/compensation authorization.
- Stale completion, stale reconciliation, cross-camera/stopped claims,
  stale failure compensation, high-water preservation after row deletion, and
  cleanup retry/new-reservation preservation are covered by persisted SQLite
  assertions.
- The full suite's non-elevated socket failure is sandbox policy, confirmed by
  the elevated single-test pass.
- The controller's dedicated PostgreSQL serialization and lease checks passed;
  broader PostgreSQL concurrency/migration coverage remains represented by the
  existing skipped tests unless that environment is supplied to the full run.
- Legacy stop polling and broader lifecycle/recovery supervision remain
  intentionally out of scope for Task 3 and belong to Task 4.
