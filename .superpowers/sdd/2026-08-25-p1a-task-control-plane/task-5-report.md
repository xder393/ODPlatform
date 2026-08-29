# Task 5 report

Initial RED: focused collection failed because `inspection_effects` was absent.
SQLite behavioral suite is GREEN (6 passed). Fixtures construct same-org
composite keys at creation time. PostgreSQL concurrency coverage is gated by
`ODP_POSTGRES_TEST_URL`, with independent sessions, barrier, futures, and
cleanup. PostgreSQL/full-suite verification requires that integration URL.

## Verification evidence

Implementation includes the published-effect contract/service, the single
transactional SQLAlchemy adapter, SQLite behavioral tests, and the gated
PostgreSQL concurrency test. Existing UoW/repositories needed no changes:
publication owns one SQLAlchemy session transaction and does not nest commits.

Initial RED command: `pytest apps/web-backend/tests/modules/inspection/test_effects.py -q`.
The system executable was unavailable (`zsh: command not found: pytest`); the
backend environment then collected zero tests and failed with
`ModuleNotFoundError: No module named 'odp_api.adapters.persistence.inspection_effects'`.

Focused/UoW command:
`uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/inspection/test_effects.py apps/web-backend/tests/persistence/test_postgres_case_deduplication.py apps/web-backend/tests/persistence/test_case_audit_uow.py -q`
Result: `11 passed, 2 skipped in 1.06s`.

Task 3 regression command:
`uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/tasks/test_fenced_execution.py apps/web-backend/tests/persistence/test_postgres_fencing.py -q`.
It was included in the full run: Task 3 local tests passed (PostgreSQL-gated
tests skipped).

Full backend command:
`uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests -q`
Result: `217 passed, 10 skipped in 13.25s`, no warnings.

Ruff command on every changed Python file reported `All checks passed!`;
`git diff --check` reported no output.

The PostgreSQL test uses two independent claims/sessions, a Barrier and
futures with propagated exceptions, and asserts two results/events, one case,
one active episode, and two alert outbox rows. It is skipped locally because
`ODP_POSTGRES_TEST_URL` is unset.

Self-review confirms camera-first locking; tenant/owner/fence/attempt guards;
DB completion time; duplicate comparison of hash, detections, and execution;
no nested commits; rollback/no-effect behavior; predecessor-linked audit hash;
and one alert outbox row per event. The failure hook is adapter constructor
injection used only by tests and is not part of the production service API.

Concern: PostgreSQL locking and unique-key concurrency remain CI-gated.
