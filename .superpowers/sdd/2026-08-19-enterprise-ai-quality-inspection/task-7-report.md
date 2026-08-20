# Task 7 report: resilient asynchronous task processing

Implemented idempotent, bounded asynchronous work for the web backend.

## Delivered

- `TaskService.enqueue(task_type, idempotency_key, payload)` persists task records and returns the existing record for duplicate `frame_id:model_version` keys.
- Task records use the required task state set: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `RETRYING`, and `DEAD_LETTER`.
- Vision work is bounded by a 30-second `asyncio.wait_for` deadline. Failures retry at one- and two-second exponential delays, then enter `DEAD_LETTER` on the third failed attempt.
- Stale vision frames older than two seconds are marked with the distinct frame disposition `SKIPPED` while their task state remains within the required task-status set (`FAILED`).
- The task metrics port emits `task_queue_depth` and `task_dead_letter_total`; exhausted failures publish a durable-shaped dead-letter alert event.
- Added a Redis Stream task queue adapter that accepts an injected Redis command surface, so it is testable without a live Redis service.
- The notification router now depends on an alert-feed port. The existing in-process repository is preserved, and a Redis Stream inspection-alert feed is available without changing the public `updated_after` query contract. The adapter accepts redis-py byte fields as well as local string fakes.

## Verification

- RED: `apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/tasks/test_service.py -v` initially failed because the task service did not exist. Redis adapter and notification-stream tests were also observed failing before their implementations.
- GREEN: `apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/tasks/test_service.py -v` passed: 5 tests.
- Regression suite: `apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v` passed: 39 tests (one pre-existing Starlette deprecation warning).
- Syntax check: `apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api` passed.
- Scoped lint: `uvx ruff check --select F,I,UP` for the task and notification source files passed.
- Whitespace check: `git diff --check` passed.
