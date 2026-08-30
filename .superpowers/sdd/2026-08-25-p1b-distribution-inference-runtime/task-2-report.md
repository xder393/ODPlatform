# P1B Task 2 — Inference consumer and Worker recovery loop

## Checkpoint status

This checkpoint covers the unit-level implementation and verification of the
Redis inference consumer and Worker orchestration. The real Redis/PostgreSQL
recovery tests were intentionally not executed in this bounded checkpoint;
they remain required before Task 2 can be considered fully verified.

## TDD evidence

The first test run was deliberately made before the production implementation
and failed during collection with:

```text
ModuleNotFoundError: No module named 'odp_api.processes.inference_worker'
```

The implementation was then added behind the existing TaskExecutionPort and
InspectionEffectService boundaries. The unit suite now exercises the real
Worker orchestration rather than a duplicate task state machine.

## Implemented behavior

- Redis consumer-group setup tolerates `BUSYGROUP` only, preserves a stable
  process UUID consumer name, parses new deliveries and `XAUTOCLAIM` response
  shapes, and returns deliberate ACK counts.
- Worker delivery parsing is bounded by the canonical 64 KiB envelope limit;
  malformed, oversized, unsupported-schema, and unsupported-event deliveries
  are quarantined before ACK when authoritative metadata is available.
- Tenant/task data is reloaded from the authoritative task port. Terminal or
  stale dispatch references ACK without creating an Attempt; a live foreign
  lease remains pending.
- Claimed work starts a 5-second renewal loop, enforces a 10-second inference
  deadline, classifies timeout/model/input/infra failures, and records failure
  before ACK.
- Successful work commits through `InspectionEffectService.publish()` before
  ACK. Cancellation and lost/stale leases do not ACK. Renewal tasks are
  stopped and awaited on every exit path.
- Worker-owned recovery uses `XAUTOCLAIM` with a 20-second idle threshold and
  feeds reclaimed deliveries through the same processing path.

## Verification

Command:

```text
uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas pytest apps/web-backend/tests/modules/tasks/test_inference_consumer.py -q
```

Result: `8 passed in 0.23s`.

Command:

```text
uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas ruff check apps/web-backend/src/odp_api/modules/tasks/consumer.py apps/web-backend/src/odp_api/processes/inference_worker.py apps/web-backend/tests/modules/tasks/test_inference_consumer.py
```

Result: `All checks passed!`. The staged diff also passes `git diff --cached
--check`.

## Remaining verification and concerns

- `tests/integration/test_redis_worker_recovery.py` was not run in this
  checkpoint, as requested. It must run against the CI-provisioned
  `ODP_POSTGRES_TEST_URL` and `ODP_REDIS_TEST_URL` services and verify Worker A
  cancellation, Worker B `XAUTOCLAIM` recovery, commit-before-ACK redelivery,
  and the one-RUNNING-per-camera invariant.
- P1A fencing/effects regressions, the expanded Ruff scope, the full backend
  suite, and the lockfile check remain for the parent integration checkpoint.
- Production MinIO and ONNX adapters are intentionally not hard-coded here;
  artifact loading and inference remain injected ports for Tasks 4/6/7.

## Integration checkpoint (2026-08-30)

The recovery fixture initially produced a genuine PostgreSQL RED: Alembic head
enforced `frame_artifacts.stream_session_id`, but the fixture added parent and
child rows together while these models intentionally have no ORM relationships.
The Task 2 fixture now flushes `inspection_sessions` before its child rows. The
first isolated real run then passed both recovery tests in `41.03s`; after
adding the final ACK/PEL/Case assertions, the same run passed `2 passed in
41.00s` against PostgreSQL `0008_numeric_outbox_schema_version (head)` and
Redis 7 on isolated local service ports.

The two crash windows are now explicit: Worker A cancellation leaves a Redis
PEL entry and no ACK, Worker B `XAUTOCLAIM`s it and produces exactly one Result
and one Case across two fenced Attempts; the separate post-commit crash is
redelivered as a terminal duplicate, ACKed, removes the PEL entry, preserves
one Result/Case, and creates no new Attempt. Each test uses independent
SQLAlchemy sessions through the repository/effect adapters, propagates the
awaited process exception, and bounds recovery with the 20-second idle wait
inside the documented 30-second budget.

The bounded no-service run collected all Task 2 tests cleanly:

```text
8 passed, 2 skipped in 0.38s
```

The three existing P1A PostgreSQL files were temporarily edited while running
the broad verification matrix: parent-FK flushes exposed a fixture ordering
issue, the fencing command hash exposed a fixture mismatch, and camera
admission needed a retry for its documented transient `ADMISSION_IN_PROGRESS`
race. Those edits were verification-only and unrelated to Task 2, so they were
reverted. The retained Task 2-only fixture change is the parent-first flush
required for this recovery integration test to run against PostgreSQL.

Changed-file PostgreSQL tests correctly collect and skip without CI services:
`8 skipped in 0.27s`. Task 2 production, unit, and integration-test Ruff
checks pass (`All checks passed!`).

The parent integration checkpoint still needs to rerun the broad P1A/full
backend matrix, expanded repository Ruff, and `uv lock --check` against a
fresh CI-style database after this narrowed diff. No final Task 2 completion is
claimed in this checkpoint.

## Final verification checkpoint (2026-08-30)

Verification was rerun from clean `HEAD` (`9339aac`) with the existing
isolated services checked healthy (`pg_isready`, Redis `PONG`) and Alembic
reporting `0008_numeric_outbox_schema_version (head)`. The Task 2 unit plus
URL-gated integration command produced `8 passed, 2 skipped in 0.35s`; the
single permitted real-service recovery rerun produced `2 passed in 40.91s`.

The Task 1/P1A envelope, relay, effects, recovery, and fencing command
collected 97 tests and produced `92 passed, 5 failed in 10.26s`. All five
failures are unchanged `test_postgres_fencing.py` fixture inserts rejected by
PostgreSQL because parent `inspection_sessions` rows are not flushed before
child artifacts. They are pre-existing baseline warnings and no P1A files
were changed in this checkpoint. Without the CI PostgreSQL URL, those three
PostgreSQL test files collect as `8 skipped in 0.27s`.

The fresh Alembic-head full backend command collected 290 tests and produced
`282 passed, 7 failed, 1 skipped in 63.29s`: the same five fencing fixture
failures, the existing camera-admission race test, and the existing case
replay FK-order fixture. The one local skip is the runtime-role test requiring
`ODP_POSTGRES_APP_TEST_URL`; CI provisions that separate role. These failures
are outside the Task 2 diff and are retained as explicit verification
warnings.

Expanded Ruff over `apps/web-backend/src`, `apps/web-backend/tests`, and
`packages/shared-schemas/src` passed (`All checks passed!`); `uv lock --check`
passed (`Resolved 51 packages`); and `git diff --check` passed. A final review
of the 1,040-line Worker found no pytest/unittest imports, fake state machine,
or test-only production machinery. Its size is justified by the typed adapter
ports, bounded/quarantine parsing, multiple injected adapter call-shapes,
lease renewal/fencing lifecycle, failure normalization, and Redis response
normalization. No production duplication/refactor was warranted.

Task 2 remains `DONE_WITH_CONCERNS` until the pre-existing PostgreSQL fixture
warnings are resolved or explicitly accepted by the parent integration gate.

## PostgreSQL fixture correction and final verification (2026-08-30)

The controller required the seven PostgreSQL failures to be reproduced and
resolved because PostgreSQL is now provisioned in CI. On the clean checkpoint
before these corrections, the exact command collected eight tests and
reported `7 failed, 1 passed in 1.49s`:

| Failure | Root cause | Minimal correction |
| --- | --- | --- |
| Camera admission race | The second concurrent `reserve()` legitimately returned the port's `ADMISSION_IN_PROGRESS` rejection while the first upload reservation was active; the test assumed both calls would return immediately. | Retry only that documented rejection after a `Condition` notification from the successful competing `complete_upload()`, with a five-second monotonic deadline. All other errors propagate; the two-READY/capacity and eviction assertions remain unchanged. |
| Case compatibility replay | `InspectionSessionRow` and child artifact/task rows were added together despite intentionally absent ORM relationships, so the Alembic-head FK could flush a child first. | Flush each parent `InspectionSessionRow` before adding its children. |
| Five fencing tests | The shared `_create_ready_tasks` fixture had the same parent/child flush ordering defect. | Flush the parent before adding camera state, artifacts, and tasks. |
| Expired-fence publish assertion after the FK fix | `_command()` supplied `"a" * 64`, while the fixture's authoritative sequence-1 artifact hash is `f"{1:064x}"`; publish therefore stopped at a hash conflict before exercising stale-fence rejection. | Use the sequence-1 artifact hash in `_command()`. |

The three corrected P1A files are only test fixtures/contracts:

- `apps/web-backend/tests/persistence/test_postgres_camera_serialization.py`
- `apps/web-backend/tests/persistence/test_postgres_case_deduplication.py`
- `apps/web-backend/tests/persistence/test_postgres_fencing.py`

The intermediate TDD runs were `6 passed, 1 failed` after the parent flushes
(the remaining failure was the intentional hash mismatch), then `5 passed`
for fencing after the hash correction. The complete corrected PG set passed
`8 passed in 0.62s`; the camera test also passed in five consecutive runs.

The final CI-shaped verification used a fresh disposable PostgreSQL database,
Alembic `0008_numeric_outbox_schema_version (head)`, the bootstrapped/granted
`odp_app` runtime role, and Redis 7:

| Verification | Result |
| --- | --- |
| Task 2 unit + real Redis/PostgreSQL recovery | `10 passed in 41.14s` |
| Task 1/P1A envelope, relay, fencing, effects, recovery regressions | `94 passed in 2.78s` |
| Full `apps/web-backend/tests` | `290 passed in 62.14s` |
| Expanded Ruff (`apps/web-backend/src`, `apps/web-backend/tests`, `packages/shared-schemas/src`) | `All checks passed!` |
| `uv lock --check` | `Resolved 28 packages in 2ms` |
| `git diff --check` | passed |

The no-service safety check collected 18 URL-gated/unit tests and reported
`8 passed, 10 skipped in 0.43s`: the ten skips are the two Redis/PostgreSQL
Task 2 integration tests plus the eight PostgreSQL fixture tests, all with
explicit environment-based skip reasons. With the CI-shaped services, the
full suite had zero skips and zero warnings. The 1,040-line Worker was
reviewed again: it contains no pytest/unittest imports, fake state machine,
or test-only machinery; its size remains attributable to typed adapter
normalization, bounded parsing/quarantine, lease renewal/fencing, failure
classification, and Redis response handling. No production refactor was
warranted. Task 2 is now `DONE` pending parent integration.

For completeness, an immediate repeat without resetting the seeded disposable
database was discarded after one expected state-contamination failure
(`289 passed, 1 failed`): the preceding seed-backed case test left its demo
case in `IN_REVIEW`. Resetting PostgreSQL and Redis and replaying the CI
bootstrap/seed sequence produced the fresh `290 passed` result above; this was
not a Task 2 or PostgreSQL fixture failure.

## Fix round 1: delivery authority and Worker runtime (2026-08-30)

Review base `bbd879e` had two Critical and six Important findings.  The
implementation and regression tests are committed as `a03bce1`.

### RED evidence

The first Worker regression batch was run before the Worker and persistence
changes:

```bash
uv run --project apps/web-backend --extra dev \
  --with-editable packages/shared-schemas pytest \
  apps/web-backend/tests/modules/tasks/test_inference_consumer.py -q
```

It collected 15 tests and reported `8 passed, 7 failed`.  The failures proved
that the old Worker could claim a superseded dispatch generation, returned
Pending for malformed/oversized messages with no recoverable tenant/task
metadata, lost the typed quarantine reason, included Artifact loading in the
model deadline, accepted an incomplete inference result as success, and let a
single delivery exception terminate the batch.

The PostgreSQL regression initially failed during collection with an import
error for the not-yet-defined `DeliveryOutcome`; after the typed contract was
introduced, its controlled row-lock race was load-bearing: dispatch 1 waits
behind the redispatch transaction and observes committed dispatch 2 rather
than creating an Attempt.  A separate malformed typed-result regression first
failed by recording `RETRYABLE_INFRA` rather than `MODEL_CONFIGURATION`.

Finally, the Redis structural poison regression was added before its transport
fix and failed independently:

```text
ValueError: Redis Stream fields must contain key/value pairs
1 failed in 0.13s
```

That exception occurred before the old response normalizer could produce a
message for the Worker's per-message boundary, so the entry could neither be
durably quarantined nor ACKed.

### Implementation

1. `DeliveryRequest.expected_dispatch_seq` is mandatory.  The new
   `accept_delivery()` repository transaction obtains the camera anchor and
   Task row lock, then compares dispatch generation and claims or quarantines
   in the same PostgreSQL boundary.  A stale generation is an ACKable
   duplicate with no Attempt; an exact generation may claim/quarantine; a
   future generation is durably quarantined without changing Task state.
2. `UnscopedQuarantineCommand`, `QuarantineReason`, and
   `WorkerDeliveryScope` provide the constrained poison-message path.
   Missing/unknown references use nullable Task fields; stream/message/event/
   schema/error text is bounded and raw evidence is capped at 64 KiB.
   Structurally malformed Redis field sequences with a recoverable message id
   are normalized to bounded poison evidence so the Worker can quarantine and
   ACK them.
3. The old signature probing, method aliases, broad `TypeError` fallback, and
   object-shape introspection were removed.  Database/implementation
   `TypeError` now reaches the per-message isolation boundary and remains
   Pending; it is not misclassified as an unknown Task or ACKed.
4. Artifact loading and inference are explicit async typed ports.  Only the
   inference call is inside the 10-second timeout; renewal begins before
   Artifact loading and remains active through effect commit.  The threaded
   composition adapters call repository/service methods in worker threads;
   those methods create and close their own Sessions from session factories,
   so no SQLAlchemy Session crosses a thread boundary.
5. Success requires an `InferenceResult` with a complete
   `InferenceExecutionContract`, valid hashes and shape/threshold fields,
   typed detections, and finite stage durations.  Missing or malformed adapter
   output is `MODEL_CONFIGURATION`; no `unknown` text, zero digest, or missing
   frame hash is fabricated.
6. The exact typed reason (`MALFORMED_ENVELOPE`, `PAYLOAD_TOO_LARGE`,
   `UNSUPPORTED_SCHEMA`, `UNRESOLVED_TASK_REFERENCE`, or
   `FUTURE_DISPATCH_SEQUENCE`) flows through Worker, command, repository, Task
   error code where scoped, and quarantine row.
7. The Worker now depends on four small explicit ports and one typed Redis
   message.  It exposes no low-level success path; business success is written
   only through `InspectionEffectService` via the effect port.
8. Both real recovery tests now give Redis ownership and recovery to
   `Worker.run_once()`.  They cover Worker A cancellation followed by Worker B
   `XAUTOCLAIM`, and effect-commit-before-ACK redelivery as a terminal
   duplicate.  Each recovery section has a monotonic `<30s` assertion and
   verifies an empty PEL and exactly one Attempt/business result.

### GREEN and final verification

Focused deterministic suites:

```text
structural poison regression: 1 passed in 0.12s
Worker unit suite:             17 passed in 0.23s
Worker + persistence suite:   44 passed in 1.28s
```

The real isolated PostgreSQL/Redis gate used a disposable database and Redis
test DB and ran `test_postgres_fencing.py` plus
`test_redis_worker_recovery.py`: `8 passed in 45.45s`.  Six PostgreSQL tests
include the controlled dispatch row-lock race; two Redis tests own recovery
through `Worker.run_once()`.

The Task 1/P1A envelope, Relay, effects, recovery, schema, and fencing
selection on a separately migrated disposable database produced
`86 passed in 2.25s`.  An earlier harness attempt intentionally remains noted:
running the Outbox `create_all` fixture on a blank database before a fencing
test that calls Alembic caused six `DuplicateTable: actors` failures because
there was no Alembic version row.  Reproducing the CI order (Alembic first)
removed all six without a code change.

The final CI-shaped run used a fresh database, standalone pgvector migrations,
Alembic head, bootstrapped/granted `odp_app`, demo/knowledge seeds, and real
Redis:

```text
collected 305 items
305 passed in 65.28s; skips: 0; warnings: 0
```

Post-run quality gates:

```text
expanded Ruff: All checks passed!
uv lock --project apps/web-backend --check: Resolved 51 packages in 2ms
git diff --check: passed
```

### Eight-finding self-review

| Finding | Evidence/ruling |
| --- | --- |
| Atomic dispatch authority | One repository transaction and real PostgreSQL lock-race test; stale/future paths create zero Attempts. |
| Durable unscoped poison handling | Nullable reference tests, bounded persisted fields/raw, capability rejection, structural poison quarantine + ACK. |
| No swallowed `TypeError` | Dedicated two-message regression leaves the failing first delivery unacked, processes/ACKs the second, and creates no unscoped quarantine for the error. |
| Async ports and deadline/renewal scope | Slow Artifact load exceeds the inference deadline yet succeeds while renewal runs; only `infer()` is timed. |
| Complete output contract | Incomplete and malformed typed results record `MODEL_CONFIGURATION`; effect validation remains the only success boundary. |
| Exact quarantine reason | Unit and persistence assertions cover unsupported, malformed, oversized, unknown, and future reasons end-to-end. |
| No aliases/introspection/bypass | Source review finds no method-name probing or signature fallback; Worker uses only explicit typed ports and `InspectionEffectService` for success. |
| Real Worker-owned recovery | Two real PG/Redis tests call `Worker.run_once()`, exercise A/B ownership transfer and commit-before-ACK redelivery, and enforce `<30s` recovery. |

The two previously accepted minors remain deferred unchanged: consumer-name
UUID/BUSYGROUP response-code hardening, and removing the unused Worker
`MAX_ATTEMPTS` constant plus a Worker-level third-failure test.  They do not
weaken any of the eight fixed review boundaries.
