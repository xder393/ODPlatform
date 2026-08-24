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

## Final authoritative regression evidence

- `test_async_redis_client_uses_separate_connect_and_read_timeouts` intercepts
  `Redis.from_url` and asserts URL, 1-second connect timeout, 20-second socket
  timeout and decoded responses.
- `test_alembic_upgrades_original_0004_alert_data_to_tenant_scoped_uniqueness`
  upgrades a real temporary SQLite database from original 0004 to head, then
  verifies old data remains and composite event uniqueness works.
- Final focused feed suite: 15 passed. Final backend suite: 133 passed, 1
  existing PostgreSQL-gated skip. `git diff --check` passed.

## Fix round 2 completion

- Redis subscriptions now use a dedicated `redis.asyncio` connection with a
  one-second connect timeout and 20-second socket timeout, while `XREAD BLOCK`
  remains 15 seconds. Cancellation closes that connection through `aclose()`.
- Subscription captures stream high-water with `XREVRANGE`, queries durable
  facts, then reads from that stable stream ID (or `0-0` when empty), avoiding
  the `$` lost-wakeup window.
- Added the Redis runtime dependency and regenerated `uv.lock`.
- Full backend: 125 passed, 1 existing skip.

## Fix round 2 acceptance tests

- `test_redis_highwater_interleaving_publishes_before_first_xread` proves an
  event inserted after high-water capture and before the first durable query
  is returned without waiting for XREAD timeout.
- `test_redis_timeout_requeries_and_cancellation_closes_client` and
  `test_redis_xread_failure_propagates_and_closes_client` prove timeout wake,
  cancellation cleanup, and error propagation.
- Focused: 22 passed. Full backend: 128 passed, 1 existing skip.

## Final round-2 regression coverage

- `test_bad_cursor_does_not_consume_ticket` proves a rejected negative cursor
  leaves the same one-time ticket usable by a valid connection.
- `test_backlog_over_100_counts_every_reconciled_event` verifies pagination and
  the exact 101-event reconciliation metric delta.
- `test_barrier_forces_duplicate_unique_race_and_returns_winner` synchronizes
  both absent checks with `threading.Barrier` before the unique collision.
- Focused: 18 passed. Full backend: 131 passed, 1 existing skip.

## Fix round 3 final checks

- `test_redis_highwater_interleaving_publishes_before_first_xread` now proves
  the first durable query is empty, XREAD starts from captured `8-0`, and a
  fact published during XREAD is returned by the next durable query.
- Cancellation test blocks XREAD on an unset event and proves cancellation was
  delivered plus `aclose()` executed. WebSocket task cleanup now uses gather
  with `return_exceptions=True` before unconditional iterator close.
- Final authoritative verification: focused persistence feed tests 13 passed;
  full backend 131 passed, 1 existing PostgreSQL-gated skip; `git diff --check`
  passed.

## Fix round 2 (migration/interface)

- Restored immutable 0004 migration contents and moved tenant-scoped uniqueness
  conversion entirely into 0005; PostgreSQL looks up the actual old unique
  constraint name while SQLite rebuilds the table safely.
- The feed port now declares line authorization filtering, and provider cursor
  parsing rejects negative/malformed cursors consistently.
- Focused: 19 passed. Full backend: 125 passed, 1 existing skip.

## Final metadata-drift verification

- RED: fresh SQLite `alembic upgrade head` followed by `alembic check` detected
  an un-migrated `ix_inspection_alerts_event_id` index.
- GREEN: removed the redundant model-only index; composite `(organization_id,
  event_id)` remains the idempotency key.
- `test_fresh_alert_feed_migrations_match_sqlalchemy_metadata` now executes a
  real fresh upgrade plus check. Final focused: 16 passed; `alembic check` has
  no new operations; full backend: 137 passed, 1 existing skip.
