# Task 4 — MinIO Artifact Saga and cleanup

## Scope delivered

- Added the typed immutable `ObjectStoragePort` and MinIO adapter.
- Added admission → upload → HEAD verification → PostgreSQL promotion Saga.
- Added compensating failure handling: storage verification failures mark the
  artifact FAILED, release the camera reservation, and best-effort delete the
  just-uploaded object; a second-transaction DB failure intentionally leaves a
  PENDING orphan for reconciliation.
- Added bounded orphan reconciliation and retention cleanup. Reconciliation
  locks the camera anchor first, verifies the current reservation and object
  SHA/length, and never revives an expired admission. Cleanup is restricted to
  expired AVAILABLE/EVIDENCE artifacts and re-checks evidence references and
  live tasks before marking DELETED.
- Added actor-aware evidence authorization before presigning; the Saga no
  longer accepts a caller-controlled boolean authorization flag.
- Evidence promotion now records a 90-day retention deadline.
- Added PostgreSQL/SQLite partial indexes for bounded pending and retention
  scans.
- Added unit, persistence, and environment-gated real MinIO integration tests.
- CI provisions a digest-pinned MinIO service and exports diagnostics on
  failure.

## TDD and verification evidence

- Reconciler RED: collection failed because `odp_api.modules.ingestion.reconciler`
  did not exist.
- Reconciler GREEN: 3 passed.
- Saga/adapter/reconciler module suite: 16 passed.
- Persistence reconciliation + schema suite: 6 passed.
- Inspection effects suite: 29 passed.
- Full backend suite: 384 passed, 29 skipped, 1 warning.
- Ruff on `src` and `tests`: clean.
- `compileall`: clean.
- `git diff --check`: clean.

## Environment-gated checks

- PostgreSQL concurrency tests remain URL-gated and were skipped locally when
  the explicit test URL was not configured.
- Real MinIO tests are present and CI-configured but skipped locally because
  `minio-py` is not installed in the current virtualenv.

## Merge blocker

`apps/web-backend/pyproject.toml` now declares `minio>=7.2,<8`, but the
corresponding `uv.lock` update could not be generated because this environment
cannot resolve `pypi.org`. CI's frozen-lock check must be run after network is
available; the lock file was intentionally not hand-edited.

## Follow-up wiring

Task 7 still needs to compose the Saga, reconciler cadence, MinIO credentials,
and the concrete evidence authorization policy into the deployed runtime.
