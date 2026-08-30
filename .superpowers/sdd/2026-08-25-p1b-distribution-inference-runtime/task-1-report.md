# P1B Task 1 report: versioned envelopes and generic Outbox Relay

## Implementation

Task 1 adds a JSON-only, versioned `EventEnvelope` to the shared schemas
package.  Envelope timestamps are normalized to UTC and serialized as compact,
deterministic RFC3339 JSON.  The inference event is validated as a
reference-only message containing exactly `task_id` and `dispatch_seq`; its
payload cannot carry an object key or other task data.  JSON primitives are
strictly typed so bytes and process-local objects cannot be coerced onto the
transport.

The backend now has an `OutboxPublisherPort`, an allowlisted Redis Streams
publisher, and an `OutboxRelay`.  The allowlist maps stable event types to
`odp:inference:tasks` and `odp:inspection:alerts`; no destination names are
used by the process layer.  Redis receives one canonical JSON `envelope`
field through untrimmed `XADD`; the publisher does not send `MAXLEN`.  Alert
payloads are revalidated against and reserialized through the P1A
`InspectionAlertCreated` contract, retaining numeric schema version `1` and
rejecting tenant mismatches or extra transport fields.

The relay claims a bounded batch with an expiring lease, commits that claim,
publishes outside the claim transaction, and marks publication through a
second guarded transaction.  Publish errors increment attempts and apply
capped exponential backoff without deleting the Outbox row.  A mark failure
is allowed to escape so a Redis-success/DB-failure crash window is replayed
after lease expiry.  Inference task authority is loaded with a tenant-matched
SQLAlchemy join; a missing task remains durable and becomes a retryable
construction failure.

P1A did not expose a repository for these Outbox operations, so the smallest
supporting adapter was added at
`apps/web-backend/src/odp_api/adapters/persistence/outbox.py`.  Claims are
cross-tenant only in the system-scoped relay selection; every state mutation
matches `organization_id`, claim owner, unexpired lease, and unpublished
state.  Production time decisions use database time; tests inject a clock.

## Files

- Modified `packages/shared-schemas/src/odp_schemas/events.py`.
- Added `apps/web-backend/src/odp_api/ports/events.py`.
- Added `apps/web-backend/src/odp_api/adapters/events/__init__.py` and
  `redis_streams.py`.
- Added `apps/web-backend/src/odp_api/processes/__init__.py` and
  `outbox_relay.py`.
- Added `apps/web-backend/src/odp_api/adapters/persistence/outbox.py` and
  exported it from `adapters/persistence/__init__.py`.
- Added `apps/web-backend/tests/modules/tasks/test_event_envelope.py`.
- Added `apps/web-backend/tests/integration/test_outbox_relay.py`.

## TDD evidence

The initial envelope test run was genuinely red at collection because the
new adapter import did not exist:

```text
ModuleNotFoundError: No module named 'odp_api.adapters.events'
```

The first Relay test run was likewise red at collection before its process
package existed:

```text
ModuleNotFoundError: No module named 'odp_api.processes'
```

After the minimal classes existed, behavior-first tests were made stricter
and failed before the corresponding guards were added:

```text
Failed: DID NOT RAISE <class 'pydantic_core._pydantic_core.ValidationError'>
Failed: DID NOT RAISE <class 'ValueError'>
```

The first is the inference payload/object-key case; the second is the P1A
alert payload contract case.  The final focused GREEN command is:

```bash
.venv/bin/python -m pytest \
  apps/web-backend/tests/modules/tasks/test_event_envelope.py \
  apps/web-backend/tests/integration/test_outbox_relay.py -q
```

```text
collected 19 items
17 passed, 2 skipped in 0.29s
```

The two skips are the explicit PostgreSQL competing-claim and real Redis
retention tests; `ODP_POSTGRES_TEST_URL` and `ODP_REDIS_TEST_URL` are absent
in this environment.

## Verification at checkpoint

```bash
.venv/bin/ruff check \
  packages/shared-schemas/src/odp_schemas/events.py \
  apps/web-backend/src/odp_api/ports/events.py \
  apps/web-backend/src/odp_api/adapters/events \
  apps/web-backend/src/odp_api/adapters/persistence/__init__.py \
  apps/web-backend/src/odp_api/adapters/persistence/outbox.py \
  apps/web-backend/src/odp_api/processes \
  apps/web-backend/tests/modules/tasks/test_event_envelope.py \
  apps/web-backend/tests/integration/test_outbox_relay.py
```

```text
All checks passed!
```

`git diff --check` passed with no output.  `uv lock --check` resolved all 28
packages successfully; no dependency or lockfile was changed.

Earlier checkpoint counts were superseded by the final verification below.

## Self-review and remaining concerns

- No Outbox row is deleted on publish or serialization failure; retry state is
  durable and capped backoff is based on the prior attempt count.
- Tenant IDs are included in the authoritative task join and all completion
  predicates.  Lease expiry and `SKIP LOCKED` are evaluated with database
  time in production, so stale workers cannot mark reclaimed rows.
- The Redis adapter emits only a string canonical envelope field, with no
  binary/object values and no `MAXLEN`; retention remains a separate concern.
- Publish-success/mark-failure duplicates are intentional and covered by the
  focused crash-window test.  The guarded mark can safely return false for a
  competing or stale claim.
- No real PostgreSQL or Redis integration was available locally.  CI now
  provisions both URL-gated services and should execute them against the
  committed tree.

## Final verification

All commands below were run after the implementation checkpoint commit
`e2899d1`.

Focused Task 1 suites:

```bash
.venv/bin/python -m pytest \
  apps/web-backend/tests/modules/tasks/test_event_envelope.py \
  apps/web-backend/tests/integration/test_outbox_relay.py -q
```

```text
collected 19 items
17 passed, 2 skipped in 0.31s
```

The skips are `test_postgresql_competing_relays_claim_each_outbox_once` and
`test_real_redis_publisher_keeps_stream_history_untrimmed`, because the local
environment has neither explicit test URL.

P1A regressions:

```bash
.venv/bin/python -m pytest \
  apps/web-backend/tests/modules/inspection/test_effects.py \
  apps/web-backend/tests/persistence/test_task_recovery_transactions.py \
  apps/web-backend/tests/persistence/test_p1_task_schema.py -q
```

```text
collected 52 items
52 passed in 1.78s
```

Full backend suite:

```bash
.venv/bin/python -m pytest apps/web-backend/tests -q
```

```text
collected 276 items
263 passed, 13 skipped, 1 warning in 20.31s
```

The one warning is the existing Starlette deprecation warning for importing
`httpx` through `starlette.testclient`.  The 13 skips include the two Task 1
URL-gated tests and the existing PostgreSQL/database-role integration skips.

Expanded lint and repository checks:

```bash
.venv/bin/ruff check packages/shared-schemas apps/web-backend/src apps/web-backend/tests
uv lock --check
git diff --check
```

Ruff reported `All checks passed!`; `uv lock --check` reported `Resolved 28
packages`; `git diff --check` passed with no output.

CI service coverage was inspected in `.github/workflows/ci.yml`.  The initial
inspection found PostgreSQL configured but no Redis service or
`ODP_REDIS_TEST_URL`; this was fixed in the follow-up commit by adding a
healthy `redis:7-alpine` service on port 6379 and
`ODP_REDIS_TEST_URL=redis://localhost:6379/0`.  YAML parsing then confirmed:

```text
backend-web services: postgres, redis
ODP_POSTGRES_TEST_URL: postgresql+psycopg://odp:odp@localhost:5432/odp
ODP_REDIS_TEST_URL: redis://localhost:6379/0
```

Thus the URL-gated PostgreSQL and Redis tests are configured to execute in CI;
their real external-service execution remains unverified on this host.

## Authoritative verification (repository-standard environment)

The `.venv` full-suite result above is retained for audit history only; its
single legacy Starlette warning is not authoritative.  The locked project
environment was rerun with:

```bash
uv run --project apps/web-backend --extra dev \
  --with-editable packages/shared-schemas pytest \
  apps/web-backend/tests/modules/tasks/test_event_envelope.py \
  apps/web-backend/tests/integration/test_outbox_relay.py -q
```

```text
17 passed, 2 skipped in 0.34s; warnings: 0
```

```bash
uv run --project apps/web-backend --extra dev \
  --with-editable packages/shared-schemas pytest apps/web-backend/tests -q
```

```text
263 passed, 13 skipped in 20.76s; warnings: 0
```

The same project environment ran expanded Ruff:

```bash
uv run --project apps/web-backend --extra dev \
  --with-editable packages/shared-schemas ruff check \
  packages/shared-schemas apps/web-backend/src apps/web-backend/tests
```

```text
All checks passed!
```

`git status --short` and `git diff --check` were both clean after these runs.
