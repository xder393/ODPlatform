# Task 4 recovery — Phase A RED evidence

Production Task 4 edits were removed with targeted patches; committed Task 1–3 files were not reset or checked out. `git diff --exit-code` for `task_control.py` is clean. The only remaining changes are the two new test files and this report.

## RED

Command:

```text
uv run --directory apps/web-backend pytest tests/modules/tasks/test_recovery.py tests/persistence/test_task_recovery_transactions.py -q
```

Result: 9 collected, 5 passed, 4 failed.

Expected behavioral failures:

- `SimpleNamespace` has no `release_due_retries` operation.
- `odp_api.modules.tasks.recovery` is not present.
- `SqlAlchemyTaskControlRepository` has no `redispatch_stale_ready` operation (both status cases).

Phase C replaced scaffolds with a real SQLite repository fixture and literal persisted task/outbox assertions. The current RED run collects 3 tests and fails 3 times: `RecoveryService` import is missing, `release_due_retries` is missing on the real repository, and `expire_leases` is missing on the real repository. No production changes are present.
## Task 4 recovery control-plane report

### RED

`apps/web-backend/.venv/bin/python -m pytest apps/web-backend/tests/modules/tasks/test_recovery.py apps/web-backend/tests/persistence/test_task_recovery_transactions.py -q`

Initial result: `10 failed in 0.50s`. The failures were intentional and observable: `RecoveryService` and all scheduler/quarantine/replay APIs were absent, and `INVALID_INPUT`/`MODEL_CONFIGURATION` incorrectly entered `RETRY_WAIT`.

### GREEN and verification

- Focused Task 4: `11 passed in 0.50s`.
- Task 3 regressions: `12 passed, 4 skipped in 0.47s` (the skips require `ODP_POSTGRES_TEST_URL`).
- Exact full backend run with shared schemas: `PYTHONPATH=packages/shared-schemas/src apps/web-backend/.venv/bin/python -m pytest apps/web-backend/tests -q` → `200 passed, 8 skipped, 1 warning in 12.91s`.
- Changed-file Ruff: `All checks passed!`.
- `git diff --check`: no output.

### Implementation and self-review

Added typed `RecoverySummary`, `QuarantineResult`, `ReplayResult`, and `RecoveryService`. The SQLAlchemy repository now uses database `CURRENT_TIMESTAMP` for recovery scheduling, locks candidate rows with `FOR UPDATE SKIP LOCKED`, increments `dispatch_seq` only while inserting a new unique dispatch Outbox, closes expired attempts and clears camera anchors, and supports tenant-guarded quarantine and compatibility replay.

The real SQLite database tests cover: 1s/2s retry schedule and third-failure dead letter; immediate permanent failures; stale READY one-time redispatch; expired normal and cap attempts; 64KiB quarantine/tenant rollback/ack-after-commit; and BLOCKED-only replay with a new Outbox plus durable audit entry. Replay creates a new dispatch and does not reuse a Redis message. PostgreSQL-specific locking paths remain locally skipped solely because no `ODP_POSTGRES_TEST_URL` is configured.

### Post-review exact verification (2026-08-29)

Required full command: `uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests -q`.

Exact result: `200 passed, 8 skipped in 12.86s`; pytest emitted no warnings. Because no warning appeared under the required `httpx2` environment, no warning-source rerun or regression fix was needed.

Required focused command: `uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/tasks/test_recovery.py apps/web-backend/tests/persistence/test_task_recovery_transactions.py -q`.

Exact result: `11 passed in 0.40s`. `git diff --check` produced no output before the report-only commit.

### Fix Round 1 — recovery hardening (2026-08-29)

RED command: `uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/tasks/test_recovery.py apps/web-backend/tests/persistence/test_task_recovery_transactions.py apps/web-backend/tests/persistence/test_postgres_fencing.py -q`.

Exact RED result: collection failed with two `ImportError` failures because `SystemRecoveryScope` did not yet exist. The new coverage is `test_recovery_service_requires_a_typed_system_scope`, `test_recovery_repository_rejects_missing_or_invalid_system_scope`, `test_quarantine_rejects_non_ready_task_without_persisting_a_row`, `test_quarantine_duplicate_message_for_blocked_task_is_idempotently_ackable`, and PostgreSQL-gated `test_postgresql_due_retry_scheduler_skip_locked_creates_one_new_outbox_per_task`.

GREEN focused Task 4 command: `uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/tasks/test_recovery.py apps/web-backend/tests/persistence/test_task_recovery_transactions.py apps/web-backend/tests/persistence/test_postgres_fencing.py -q` → `18 passed, 5 skipped in 0.55s`. PostgreSQL tests are skipped only because `ODP_POSTGRES_TEST_URL` is unset.

Task 3 regression command: `uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/tasks/test_fenced_execution.py apps/web-backend/tests/persistence/test_postgres_fencing.py -q` → `12 passed, 5 skipped in 0.41s`.

Exact full backend command: `uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests -q` → `207 passed, 9 skipped in 13.15s` with no warnings. Ruff on all changed source/tests reported `All checks passed!`; `git diff --check` produced no output.

Self-review: quarantine now accepts only a tenant-matching READY task, except that a duplicate matching stream/message already durably quarantined for that task and tenant is idempotently ACKable. Non-READY and wrong-tenant requests create no row or state mutation. `SystemRecoveryScope` is required at both scheduler service and repository boundaries. Lease expiry locks camera anchors first using PostgreSQL `FOR UPDATE OF camera_inference_state SKIP LOCKED`, then locks/revalidates the tenant task and its attempt. The deferred audit-head race was not changed.
