# P1C Task 2 — realtime operations APIs

## Scope delivered

- Added idempotent, tenant- and line-scoped inspection-session APIs for
  supervisor/admin start requests, read/list, and guarded stop requests.
- Added `/api/v1/auth/me` so the frontend can render the authenticated
  organization, role, and production-line scope without exposing credentials.
- Added tenant/line-scoped inference-task diagnostics with attempt and outbox
  delivery history, plus supervisor/admin dead-letter replay.
- Added durable dead-letter replay as one PostgreSQL transaction: the original
  task remains terminal history, a fresh READY task and dispatch outbox are
  created, the camera ready-window guard is retained, and the action is added
  to the canonical audit hash chain.
- Added evidence URL authorization for AVAILABLE/EVIDENCE artifacts with
  tenant/line checks and a 60-second MinIO presigned URL.
- Added API MinIO settings and PostgreSQL runtime grants for the P1 control
  plane tables.

## TDD and verification evidence

- Operations API RED: the new session, task, evidence, and profile routes
  initially returned **404** before implementation.
- Operations/evidence/process/gateway selection: **13 passed**.
- Durable dead-letter replay regression: **1 passed**; covers original task
  preservation, fresh task/outbox creation, audit-chain validity, and the
  two-task ready-window cap.
- Full backend: **411 passed, 30 skipped**.
- Ruff: clean.
- Python compileall: clean.
- `git diff --check`: clean.
- Docker Compose configuration: valid.

## Environment-gated checks

- PostgreSQL concurrency suites remain skipped when
  `ODP_POSTGRES_TEST_URL` is not configured. The SQLite unit/integration
  coverage exercises the transaction shape, but PostgreSQL row-lock behavior
  still requires the Compose/CI database gate.
- Real MinIO presigning remains environment-gated locally; the evidence route
  contract uses a fake storage adapter and the Compose MinIO service is the
  deployment gate.
