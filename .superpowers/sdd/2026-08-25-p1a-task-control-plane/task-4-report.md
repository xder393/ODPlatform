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
