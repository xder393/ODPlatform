# Task 2 Report: Durable, Atomic Case and Audit Workflows

## Status

Complete.

## Commit

`c186686550fef8c39a56e0082bbfa3524e82cd6a` — `feat: persist atomic case and audit workflows`

## Changed files

- Added durable case/UoW ports, `CaseApplicationService`, SQLAlchemy case/history/audit repositories, and SQLite/PostgreSQL transaction handling.
- Added Alembic revision `0002_case_transition_metadata` for case update timestamps and transition correlation IDs; corrected Alembic URL handling so explicit runtime URLs are honored.
- Rewired `create_app()` to migrate, seed, and compose durable SQLAlchemy case/audit adapters. In-memory audit is now only an explicit test injection.
- Extended case responses with durable transition history, persisted middleware correlation IDs, and appended successful simulated pauses to the audit chain.
- Updated tests that formerly shared the default runtime SQLite path to use per-test durable SQLite databases; added focused rollback/restart/pause-audit tests.

## RED / GREEN evidence

- RED: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py tests/integration/test_runtime_persistence.py -v` failed during collection because `SqlAlchemyCaseRepository` and the business UoW/application modules did not exist.
- GREEN: the same focused command passed with `3 passed` after implementation.
- Focused regression command: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py tests/integration/test_runtime_persistence.py tests/modules/audit/test_hash_chain.py tests/integration/test_cases_api.py -v` — `16 passed`.

## Full-suite evidence

- `../../.venv-runtime/bin/pytest tests -q` — `93 passed, 1 warning`.
- `git diff --check` — clean before commit.

## Concerns

- PostgreSQL row-lock code is implemented with `SELECT ... FOR UPDATE` and conflict-safe audit-head creation, but live PostgreSQL/Compose verification remains for CI because the local Docker daemon was unavailable in the Task 1 environment.
- The sole warning is the existing Starlette `TestClient` / `httpx` deprecation warning.

## Fix round 1 (review changes requested)

### Commit

`deef7517eb8d5f32a220605c58d5a3c079071fde` — `fix: harden case audit transaction integrity`

### Changes

- `AuditContext` now normalizes every aware timestamp to UTC at construction. The normalized value is used for transition history, audit command/hash input, SQL writes, and returned objects.
- Added an SQLite +08:00 restart regression that verifies the returned history timestamp, persisted audit timestamp, and hash-chain verification all use `01:30Z`, not a reinterpreted `09:30Z`.
- Added `AuditService.append_guard()`. It holds the health lock from the blocked-state check through case history/audit append and the UoW commit. Case workflows acquire this guard before database locks; verification releases its database snapshot before blocking, so no inverse health/database lock order is introduced.
- Strengthened rollback proof: the injected failure double performs and flushes the real audit append before raising. The test confirms case status, transition history, `audit_logs`, and `audit_chain_heads` all remain unchanged after rollback.
- Added a deterministic SQLite two-writer `BEGIN IMMEDIATE` test and an env-gated PostgreSQL `FOR UPDATE` contract test. The PostgreSQL test runs only when CI supplies a dedicated `ODP_POSTGRES_TEST_URL`; it was skipped locally rather than simulated.

### RED / GREEN evidence

- RED: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py -v` — 2 expected failures: the +08 audit entry reloaded as `09:30Z`, and `block_appends()` completed while a paused transition was still in flight.
- GREEN: the same command — `4 passed, 1 skipped`.
- Focused: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py tests/integration/test_runtime_persistence.py tests/modules/audit/test_hash_chain.py tests/integration/test_cases_api.py -v` — `19 passed, 1 skipped`.
- Full backend: `../../.venv-runtime/bin/pytest tests -q` — `96 passed, 1 skipped, 1 warning`.

## Fix round 2 (review changes requested)

### Commit

`2c93c2d00d7b807f35f7388f7d772a06bc749f8a` — `fix: normalize durable audit timestamps`

### Changes

- Moved timestamp normalization to the shared `AuditCommand` domain boundary. Every standalone or UoW-backed durable audit append now writes the same UTC instant used by the canonical hash input.
- Added a standalone durable `AuditService.append(AuditCommand)` +08:00 SQLite restart regression. It asserts `01:30Z` on the returned and reloaded audit entry and validates the persisted chain.
- Upgraded the env-gated PostgreSQL contract from a case-load check to a complete `CaseApplicationService.transition`. On a real `ODP_POSTGRES_TEST_URL` database it captures SQL and separately asserts `FOR UPDATE` on `defect_cases` and `audit_chain_heads`, then verifies one committed history entry, audit entry, and matching chain head.

### RED / GREEN evidence

- RED: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py -v` — standalone durable audit roundtrip reloaded `09:30Z` instead of the expected `01:30Z`.
- GREEN: the same command — `5 passed, 1 skipped`.
- Focused: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py tests/integration/test_runtime_persistence.py tests/modules/audit/test_hash_chain.py tests/integration/test_cases_api.py -v` — `20 passed, 1 skipped`.
- Full backend: `../../.venv-runtime/bin/pytest tests -q` — `97 passed, 1 skipped, 1 warning`.

## Final review fix A: PostgreSQL audit immutability and database availability

### Status

Complete.

### Commit

`feat: enforce database audit immutability and health` (this commit).

### Changes

- Compose and CI now use an `odp_migrator` table-owner/migration role and a separate, unprivileged `odp_app` runtime role. Development-only credentials are explicitly documented as non-production defaults.
- Added idempotent runtime grants: business tables receive the required CRUD permissions, sequences receive `USAGE, SELECT`, `audit_logs` receives only `SELECT, INSERT`, and `audit_chain_heads` receives only `SELECT, INSERT, UPDATE`. The app cannot update, delete, or truncate audit entries.
- Migrations and RAG schema work run as the migrator; grants run after Alembic; seed and Uvicorn run as `odp_app`. Runtime app startup no longer performs DDL for Docker, staging, or PostgreSQL production deployments.
- `/healthz` now executes `SELECT 1` and returns a safe 503 when unavailable. A unified SQLAlchemy exception handler maps connection/driver availability failures on login and case routes to the same safe 503 without exposing DSNs; integrity failures remain non-503 database errors.
- Added static Compose/permission tests, health/login/case availability regressions, and an env-gated live PostgreSQL role contract that verifies distinct owner/current user, a real `AuditService.append`, and denied audit `UPDATE`/`DELETE`.

### RED / GREEN evidence

- RED: `../../.venv-runtime/bin/pytest tests/persistence/test_database_roles.py tests/integration/test_deploy_database_roles.py -v` — failed because `odp_api.database_roles` and the split-role Compose configuration did not exist.
- GREEN focused: `../../.venv-runtime/bin/pytest tests/integration/test_database_availability.py tests/persistence/test_database_roles.py tests/integration/test_deploy_database_roles.py -v` — `5 passed, 1 skipped` without a live role database.
- Live PostgreSQL: started a temporary `pgvector/pgvector:pg16` container, ran bootstrap → RAG migrations → Alembic → grants, then ran the env-gated contract with both role URLs — `2 passed`. The temporary container was removed afterward.
- Schema/config checks: `ODP_DATABASE_URL=sqlite:////tmp/odp-task2-final-alembic.sqlite3 ../../.venv-runtime/bin/alembic upgrade head`, `docker compose -f ../../deploy/compose.yaml config --quiet`, and `git diff --check` all passed.
- Full backend: `../../.venv-runtime/bin/pytest tests -q` — `145 passed, 3 skipped, 1 warning`.

### Concerns

- The only remaining warning is the existing Starlette `TestClient` / `httpx` deprecation warning.
- The PostgreSQL runtime-role contract remains env-gated in the ordinary suite and is provisioned in CI; it was additionally executed against a real temporary local pgvector container for this change.
