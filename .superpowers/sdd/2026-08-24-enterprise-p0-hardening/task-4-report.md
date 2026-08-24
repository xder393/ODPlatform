# Task 4 report — durable real-time inspection alerts

Status: complete.

Commit: `feat: stream durable inspection alerts in realtime` (recorded after this report).

## Delivered

- Database-backed `inspection_alerts` facts carry monotonic opaque cursors and
  are deduplicated by `event_id` before any subscriber notification.
- Cursor reconciliation returns `{items, next_cursor}` with bounded limits;
  per-alert tenant and line authorization remains enforced for REST and the
  continuous ticket-authenticated WebSocket.
- SQLite subscriptions always query durable state after a condition wake-up or
  timeout. Active/reconnect/reconciled metrics contain no identity labels.
- Docker/production composition wraps the same durable fact store with a
  bounded Redis stream publication transport; local composition stays SQLite.

## TDD evidence

- RED: the new persistence contract first failed collection because
  `odp_api.adapters.notifications.sqlite_feed` did not exist.
- GREEN focused command:
  `../../.venv-runtime/bin/pytest tests/persistence/test_alert_feed_contract.py tests/integration/test_live_notification_api.py tests/integration/test_notification_api.py -q`
  — 14 passed, one pre-existing TestClient deprecation warning.
- Full backend command: `../../.venv-runtime/bin/pytest tests -q` — 120
  passed, 1 existing PostgreSQL-gated skip, and the same TestClient warning.
- `git diff --check` passed.

## Files

- Added SQLite durable feed, Redis durable-feed wrapper, and Alembic 0004.
- Updated notification ports/router, runtime composition, stream client and
  metrics; migrated existing notification assertions to the cursor envelope.
- Added provider contract and same-socket integration coverage.

## Concerns

- Live Redis/Compose verification remains intentionally deferred to Task 5 as
  specified. The local fake verifies bounded stream publication compatibility;
  no live Redis server was used in this task.
