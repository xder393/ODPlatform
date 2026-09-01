# P1B Task 3 — Redis 7 PEL-safe retention and database recovery

## Delivery

- Implementation and tests: `e3713f6bee3aec36f893c0645a7e293e917cc24a`
- Base: `73b02d611dea24d8d6453ee7fda8d6f0f7f8a75a`
- Scope is limited to Task 3. No Compose wiring, push, merge, or Task 4 work is included.

## RED → GREEN evidence

The initial tests-first command was:

```text
PYTHONPATH=apps/web-backend/src:packages/shared-schemas/src .venv/bin/pytest \
  apps/web-backend/tests/modules/tasks/test_stream_retention.py \
  apps/web-backend/tests/modules/tasks/test_recovery.py \
  apps/web-backend/tests/persistence/test_task_recovery_transactions.py \
  apps/web-backend/tests/integration/test_redis7_pel_retention.py \
  apps/web-backend/tests/persistence/test_postgres_recovery_scheduler.py -q
```

It produced `18 failed, 29 passed, 3 skipped in 1.17s`. The failures were
load-bearing: eleven retention cases failed because the module/contracts did
not exist; `RecoverySummary` still exposed only three counters; the scheduler
and advisory-lock boundary did not exist; and the repository did not expose
Outbox-claim, quarantine-count, or Artifact-reservation recovery. The three
skips were the explicit Redis/PostgreSQL URL gates.

A later tests-first hardening batch moved the local Task 3 selection from 60
tests to `56 passed, 4 failed`. The four RED cases proved the destructive
Gateway-adapter boundary, structured scheduler failure reporting, fail-closed
handling of a corrupt READY row carrying lease markers, and preservation of a
PENDING Evidence Artifact. After the corresponding minimal fixes, local Task 3
reported `60 passed` with four URL-gated service tests skipped; the real
Redis/PostgreSQL Task 3 gate reported `4 passed in 0.39s`.

## Implemented boundaries

- `SafeTrimPlanner` parses Redis IDs as numeric `(milliseconds, sequence)`
  pairs. Empty, malformed, incomplete, or internally inconsistent group/Pending
  data returns no watermark.
- Retention reads every consumer group's `XINFO GROUPS` and `XPENDING` summary,
  chooses smallest Pending before last-delivered progress, caps progress by a
  positive UTC retention floor, and emits only exact
  `XTRIM <stream> MINID = <safe-id>`.
- The inference and canonical alert streams are explicit policies. Temporary
  Gateway cleanup requires both the `odp-alert-gateway:` allowlist and a typed,
  timezone-aware expiry registry; cleanup happens before the alert watermark
  read and never applies to inference groups.
- Retention and Recovery Scheduler contain no `XREADGROUP` or `XAUTOCLAIM` path.
  Worker processes remain the sole Redis delivery owners.
- Recovery Scheduler runs on a two-second cadence and offloads synchronous
  SQLAlchemy work. Its nonblocking, session-level PostgreSQL advisory lock pins
  one connection across the complete six-part multi-transaction sweep and is
  released in `finally`, including error and async cancellation paths.
- Recovery now reports and bounds due retries, stale READY redispatches,
  expired leases, expired unpublished Outbox claims, quarantine count, and
  elapsed Artifact reservations. Mutations use database time, explicit
  `SystemRecoveryScope`, guarded states, deterministic limits, and camera-first
  locking where camera admission state changes.
- Outbox repair only clears expired claims on unpublished rows. Quarantine is
  count-only. Artifact repair clears elapsed reservation anchors, fails only
  non-Evidence PENDING Artifacts, and preserves AVAILABLE/Evidence data.
- Both process loops isolate failed iterations, expose structured summaries,
  and support cooperative shutdown. Task 7 remains responsible for deployed
  process composition and credentials.

## Fresh service verification

The final focused gate used dedicated task-owned PostgreSQL 16 and Redis 7
containers. To avoid reusing accumulated state, it created a new database
`odp_task3_focus_head_sol_20260901`, upgraded it to
`0009_bound_message_quarantine_error (head)`, and used empty Redis logical DB
14. No existing database or Redis data was dropped or reset.

The exact 13-file selection collected 176 tests:

```text
apps/web-backend/tests/integration/test_outbox_relay.py
apps/web-backend/tests/integration/test_redis7_pel_retention.py
apps/web-backend/tests/integration/test_redis_worker_recovery.py
apps/web-backend/tests/modules/inspection/test_effects.py
apps/web-backend/tests/modules/tasks/test_event_envelope.py
apps/web-backend/tests/modules/tasks/test_inference_consumer.py
apps/web-backend/tests/modules/tasks/test_recovery.py
apps/web-backend/tests/modules/tasks/test_stream_retention.py
apps/web-backend/tests/persistence/test_alembic_startup.py
apps/web-backend/tests/persistence/test_p1_task_schema.py
apps/web-backend/tests/persistence/test_postgres_case_deduplication.py
apps/web-backend/tests/persistence/test_postgres_recovery_scheduler.py
apps/web-backend/tests/persistence/test_task_recovery_transactions.py
```

Result: `176 passed in 47.96s`, with zero skips.

An earlier run against a blank, non-migrated database produced
`172 passed, 4 failed in 48.38s`. All four traces were
`DuplicateTable: relation "actors" already exists`: earlier metadata fixtures
had created tables without an Alembic version row, so later migration-owning
tests attempted 0001 again. The hypothesis was verified without a code change
by initializing a second fresh database to Alembic head; the unchanged
176-test command then passed completely.

The prior isolated CI-shaped service gate on the same Task 3 diff collected the
entire backend and produced `376 passed`, zero skips, and one existing
Starlette/httpx deprecation warning in `67.59s`. The final independent local
no-service rerun collected the same 376 tests and produced
`353 passed, 23 explicitly service-gated skips, 1 existing warning in 23.50s`.

## Static and repository gates

- `ruff check apps/web-backend/src apps/web-backend/tests packages/shared-schemas/src`
  → `All checks passed!`
- `python -m compileall -q apps/web-backend/src packages/shared-schemas/src`
  → exit 0
- `UV_CACHE_DIR=/private/tmp/odp-task3-uv-cache uv lock --project apps/web-backend --check`
  → `Resolved 51 packages in 14ms`
- `git diff --cached --check` before the implementation commit → exit 0
- Frozen-boundary source scan found `XTRIM ... MINID =`, `XINFO GROUPS`,
  `XPENDING`, and allowlisted `XGROUP DESTROY`; it found no Task 3
  `XREADGROUP`, `XAUTOCLAIM`, or `MAXLEN` path.

## Limitations and deferred items

- Service tests remain explicitly URL-gated for ordinary local runs; CI/Task 7
  must continue provisioning PostgreSQL 16 and Redis 7 URLs. Their real service
  semantics were exercised in this checkpoint.
- Compose wiring and persistence/source of deployed Gateway expiry leases are
  intentionally deferred to Task 7; Task 3 supplies the typed registry and
  destructive-operation boundary only.
- The existing Starlette/httpx deprecation warning is outside this Task 3 diff.
- The two already accepted Task 2 minors (consumer UUID/BUSYGROUP hardening and
  the unused Worker retry constant/third-failure regression) remain deferred to
  final P1B review.

## Self-review conclusion

The implementation matches every frozen Task 3 ownership and data-safety
boundary. No open Task 3 defect was found in the final line-by-line review.
Independent code review is still required before the controller marks the task
complete or begins Task 4.
