# Task 4 report — durable real-time inspection alerts

Status: complete (fix round 1).

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

## Fix round 1

- RED: tenant-scoped duplicate, authorization-before-limit, and deterministic
  Redis XADD/XREAD wake-up tests failed against the initial adapter.
- Redis now requires bounded XADD and blocking XREAD capabilities, uses
  `MAXLEN ~ 10000` / `BLOCK 15000`, and re-queries durable facts after a wake.
- Idempotency is unique per `(organization_id, event_id)` with a compatible
  SQLite table rebuild for the original unnamed unique constraint.
- Focused: 17 passed. Full backend: 123 passed, 1 existing skip.

## Fix round 1 completion

- WebSocket delivery now races the next subscription item with `receive()` so
  an idle disconnect cancels and closes pending iterator tasks immediately;
  the active-connections gauge is decremented in all exits.
- Backlog is paged in 100-item chunks and reconciliation metrics increment per
  sent backlog item; live events remain excluded from that counter.
- Added idle gauge-lifecycle and concurrent duplicate-publish coverage.
- Focused: 19 passed. Full backend: 125 passed, 1 existing skip.

## Fix round 2 (migration/interface)

- Restored immutable 0004 migration contents and moved tenant-scoped uniqueness
  conversion entirely into 0005; PostgreSQL looks up the actual old unique
  constraint name while SQLite rebuilds the table safely.
- The feed port now declares line authorization filtering, and provider cursor
  parsing rejects negative/malformed cursors consistently.
- Focused: 19 passed. Full backend: 125 passed, 1 existing skip.
