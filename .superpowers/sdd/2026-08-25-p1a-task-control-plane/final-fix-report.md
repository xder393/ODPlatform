# P1A final-fix checkpoint report

This is a bounded checkpoint after the interrupted final-fix wave. It records the
coherent implementation currently in the worktree; it does not claim the final
wave is complete.

## Findings covered in this checkpoint

- **Critical 1:** retry release and compatibility replay now serialize from the
  tenant/camera anchor, enforce the two-READY cap, and recompute `ready_count`.
  READY-to-BLOCKED quarantine also uses the camera-first protocol and count sync.
- **Critical 2:** the PostgreSQL episode test now races the transaction-bound
  `claim_defect_episode()` primitive, preserving the one-RUNNING invariant.
- **Important 1:** publication validates and propagates the authoritative
  inspection-session `line_id` through Case, Event, Alert, feed, and outbox data.
- **Important 2:** publication now tenant-scopes the Task, Attempt, Artifact, and
  InspectionSession chain, checks task/artifact ownership and availability, and
  compares the command frame hash with the artifact hash.
- **Important 3:** defect publication emits the numeric-versioned
  `inspection.alert.created.v1` envelope with the complete canonical payload and
  cursor assigned after feed insertion. The existing feed reader retains its
  legacy `defect_class` compatibility field.
- **Important 4:** low-level `publish_success` and `complete_no_defect` methods
  were removed from `TaskExecutionPort` and the task-control repository; the
  composed inspection effect service is the success boundary.
- **Selected minors 1, 2, 4, and 5:** replay uses the canonical same-session
  audit append, the property test asserts capacity/ownership/effect invariants
  after generated actions including clock advance/recovery/completion, attempt
  error codes are persisted, and PostgreSQL setup applies Alembic head.

## RED evidence recovered from the interrupted wave

The changed focused tests were rerun with the shared-schema package on the
import path before the behavioral fixes. The genuine RED result was:

```text
15 failed, 51 passed, 8 skipped
```

The failures covered missing authoritative line propagation, artifact/task/hash
and tenant-chain guards, READY-count/capacity invariants, public success methods,
missing attempt error codes, retry release capacity, and compatibility replay
capacity. The initial collection-only run without `PYTHONPATH` was discarded as
an environment error and is not counted as behavioral RED evidence.

## Focused GREEN evidence

```text
$ PYTHONPATH="$PWD/apps/web-backend/src:$PWD/packages/shared-schemas/src" \
  uv run --directory apps/web-backend pytest \
  tests/modules/inspection/test_effects.py \
  tests/modules/tasks/test_delivery_properties.py \
  tests/modules/tasks/test_fenced_execution.py \
  tests/persistence/test_postgres_camera_serialization.py \
  tests/persistence/test_postgres_case_deduplication.py \
  tests/persistence/test_postgres_fencing.py \
  tests/persistence/test_task_recovery_transactions.py -q

66 passed, 8 skipped in 9.24s
```

The PostgreSQL tests skipped because `ODP_POSTGRES_TEST_URL` is absent in this
environment. The Alembic schema/startup check previously run in this wave was
`4 passed, 1 skipped`; the PostgreSQL-specific long-revision check was the skip.

## Remaining findings / final verification

- **Important 5 remains:** restore the FastAPI/Starlette resolution in
  `apps/web-backend/uv.lock` to the pre-downgrade base while retaining only the
  Hypothesis dependency delta.
- **Selected minor 3 remains:** replace the backend-wide Ruff `B008` ignore with
  narrow per-file ignores or targeted annotations.
- The full backend, expanded Ruff, final Alembic/lock checks, and final
  `git diff --check` still remain for the unbounded continuation.
- The two explicitly deferred schema items remain deferred: dual frame-hash
  consistency constraint and broader generic schema assertions.

At this checkpoint the only environmental limitation is that PostgreSQL
contention tests cannot execute without `ODP_POSTGRES_TEST_URL`.

## Final completion evidence

The remainder was completed with no production scope expansion. The lock was
updated reproducibly with:

```text
uv lock --upgrade-package "fastapi==0.141.1" --upgrade-package "starlette==1.6.0"
Resolved 51 packages in 1.95s
Updated fastapi v0.140.13 -> v0.141.1
Updated starlette v1.5.1 -> v1.6.0
```

The final lock contains FastAPI `0.141.1`, Starlette `1.6.0`, and Hypothesis
`6.165.10`; its diff contains only the FastAPI and Starlette package records.
`uv lock --check` passed (`Resolved 51 packages in 14ms`).

Final verification outputs:

```text
expanded Ruff: All checks passed!
changed test files: 13 passed, 5 skipped in 0.60s
property test: 3 passed in 6.26s
P1A tasks + inspection + persistence: 113 passed, 11 skipped in 9.92s
full backend: 245 passed, 11 skipped in 20.47s
Alembic schema/startup/offline tests: 4 passed, 1 skipped in 0.76s
git diff --check: passed with no output
```

The direct PostgreSQL offline render also succeeded and included the 0007 to
0008 conversion `ALTER TABLE outbox_events ... TYPE INTEGER USING
schema_version::integer`. Every PostgreSQL skip above is due to the absent
`ODP_POSTGRES_TEST_URL`; no test emitted warnings. Self-review found no new
deadlock path, READY-cap/count drift, tenant-chain bypass, public success API,
alert-envelope/migration mismatch, or unrelated dependency churn. The two
explicitly deferred schema items remain the only deferred findings.
