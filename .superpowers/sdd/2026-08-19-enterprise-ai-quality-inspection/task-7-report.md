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

## Review fix round 1

- `create_app` now composes `RedisStreamInspectionAlertFeed` for publishing the fixture alert and for the existing REST reconciliation and WebSocket reads. Docker sets `ODP_ENVIRONMENT=docker`, so this composition uses the dependency-free Redis Socket Stream client against the Compose Redis service; local non-Docker development uses a durable SQLite stream, never an in-memory runtime feed.
- Runtime task records now use `SQLiteTaskRepository`, whose unique idempotency key is protected by `BEGIN IMMEDIATE`. The Compose `task-state` volume persists `/data/odp-tasks.sqlite3` across API restarts. Runtime work is published through `RedisStreamTaskQueue`, and dead-letter alerts use their own Redis Stream.
- The task deadline regression intercepts the unavoidable `asyncio.wait_for` boundary and proves the service sends the required 30-second value. The retry metric now reads `TaskQueuePort.depth()` after enqueueing the retry, rather than counting active records before the queue transition.

### Fix-round verification

```text
apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v
44 passed, 1 warning

apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api
exit 0

uvx ruff check --select F,I,UP <changed task/runtime files>
All checks passed!

ruby -e 'require "yaml"; YAML.load_file("deploy/compose.yaml")'
exit 0

git diff --check
exit 0
```

## Fix round 3

- Inspection-alert publication no longer performs a durable claim before a separate append. Redis runtime clients execute one Lua `EVAL` script that performs `SET ... NX` and `XADD` atomically; the durable local SQLite stream client executes the equivalent claim-plus-insert in one transaction.
- The alert adapter retains its simple-client fallback only for deterministic unit-test doubles. Both configured runtime clients support the atomic operation.
- The failure-injection regression models a crash/error at the former post-claim/pre-append boundary. The atomic operation rolls back the claim, and retry/restart produces exactly one stream event; repeated publication remains deduplicated.

### Fix-round verification

```text
apps/web-backend/.venv/bin/pytest apps/web-backend/tests/integration/test_notification_api.py -v
7 passed, 1 warning

apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v
49 passed, 1 warning

apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api
exit 0

uvx ruff check --select F,I,UP <changed notification stream files>
All checks passed!

git diff --check
exit 0
```

`docker compose -f deploy/compose.yaml config` could not run in this environment because the `docker` executable is not installed; YAML parsing above confirmed the Compose file is syntactically valid.

## Fix round 2

- Queue depth now uses the durable task-state outstanding-work ledger (`PENDING` and `RETRYING`) instead of Redis Stream `XLEN` history. Retries remain one outstanding unit; successful and completed work no longer inflate the metric.
- `TaskRecord.published_at` provides an outbox marker. A queue publication failure leaves the durable record unpublished, and duplicate enqueue plus runtime startup reconciliation republishes it safely when the transport recovers.
- The demo inspection fixture uses a stable event UUID. Stream clients expose atomic `SET NX`-style event claims, making fixture publication idempotent across app compositions and restarts.
- The direct Redis Stream client now applies `ssl.create_default_context().wrap_socket(..., server_hostname=host)` for `rediss://` URLs.

### Fix-round verification

```text
apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/tasks/test_service.py apps/web-backend/tests/integration/test_notification_api.py -v
17 passed, 1 warning

apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v
48 passed, 1 warning

apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api
exit 0

uvx ruff check --select F,I,UP <changed task/runtime files>
All checks passed!

git diff --check
exit 0
```

## Fix round 4

- Replaced the Redis alert feed's terminal `SET NX` claim with a recoverable append-once client boundary. Redis claims now move through `pending` and `published` states; an `XADD` command error leaves `pending` durable, and a retry or a newly composed feed completes the append. A `published` marker continues to suppress successful duplicates, while the legacy `"1"` marker remains treated as already published to avoid duplicating previously successful events during upgrade.
- `RedisSocketStreamClient.xadd_once` uses `redis.pcall` so the script records `pending`, returns the `XADD` error without assuming script rollback, and marks the event `published` only after `XADD` succeeds. `SQLiteStreamClient.xadd_once` retains the equivalent claim-plus-append SQLite transaction.
- Removed the unsafe alert-feed fallback that performed `SET NX` followed by a separate `XADD`. Clients without the runtime append-once capability receive an ordinary append; both configured runtime clients implement append-once.
- Replaced the rollback-assuming regression fake with a Redis-like failure injector. The test asserts that the first failure leaves the claim intact as `pending` and leaves the stream empty, reconstructs the feed to model a restart, then proves retry plus duplicate retry produce exactly one stream event and a `published` marker.

### Fix-round verification

```text
apps/web-backend/.venv/bin/pytest apps/web-backend/tests/integration/test_notification_api.py::test_alert_publication_recovers_a_persisted_claim_after_xadd_fails -v
RED: 1 failed; persisted claim was "1" instead of the required recoverable "published" result after retry
GREEN: 1 passed, 1 warning

apps/web-backend/.venv/bin/pytest apps/web-backend/tests/modules/tasks/test_service.py apps/web-backend/tests/integration/test_notification_api.py -v
18 passed, 1 warning

apps/web-backend/.venv/bin/pytest apps/web-backend/tests -v
49 passed, 1 warning

apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src/odp_api
exit 0

uvx ruff check --select F,I,UP apps/web-backend/src/odp_api/adapters/notifications/redis_stream.py apps/web-backend/src/odp_api/adapters/redis_stream.py apps/web-backend/tests/integration/test_notification_api.py
All checks passed!

git diff --check
exit 0
```
