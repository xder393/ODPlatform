# Task 7 — Compose independent runtime processes

## Scope delivered

- Added fail-closed `ProcessSettings`/`WorkerSettings` families with explicit
  PostgreSQL, Redis, MinIO, model-release, CPU-provider, and recovery-budget
  validation. Model material is required only for Worker/Ingestor roles;
  relay, scheduler, retention, and artifact reconciliation do not pretend to
  load a model.
- Added shared process lifecycle helpers (`process_id`, readiness failure,
  cooperative stop events, and bounded async loops) plus a real artifact
  reconciler process wrapper around the PostgreSQL/MinIO adapters.
- Added Compose services for frame ingestion, Outbox relay, two inference
  workers, recovery scheduler, artifact reconciliation, and Redis Stream
  retention. Each service uses an independent database login and no SQLite
  task-state volume/environment. The API now keeps its P0 SQLite task adapter
  only for local/test runtimes and does not create a hidden Docker `/tmp`
  task database.
- Added PostgreSQL bootstrap/grant boundaries for `odp_api`, `odp_worker`,
  `odp_relay`, and `odp_scheduler`, while retaining `odp_app` grants for
  existing volumes during migration.
- Documented startup prerequisites, role ownership, model digest override,
  and the still-unfrozen Compose install path.

## Verification evidence

- Task 7 settings/Compose/role suite: **10 passed, 1 PostgreSQL-gated skip**.
- Full backend regression after the runtime cutover: **400 passed, 30 skipped**.
- Ruff on backend source/tests/scripts: clean.
- `compileall`: clean.
- `git diff --check`: clean.
- `docker compose -f deploy/compose.yaml config --quiet`: passed.

## Environment gates and follow-up

- The P1 service commands now point at importable process modules and the
  artifact reconciler has a concrete database/MinIO composition. The Worker,
  Relay, Ingestor, Scheduler, and Retention classes remain dependency-injected
  process surfaces; their production adapter factories are validated in the
  next P1C runtime/observability gate rather than fabricated here.
- `uv.lock` still needs a real refresh after PyPI access returns. MinIO,
  OpenCV/NumPy, ONNX Runtime, and ONNX wheels are intentionally not hand-added
  to the lock; the real MinIO/ONNX smoke gates remain explicit skips locally.
- The Compose MinIO healthcheck uses the official `/minio/health/live`
  endpoint; production image digest pinning and frozen image builds remain a
  deployment-hardening follow-up.
