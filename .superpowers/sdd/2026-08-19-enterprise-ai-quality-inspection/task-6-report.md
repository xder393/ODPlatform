# Task 6 report: verifiable audit hash chain

Implemented append-only, organization-scoped audit records in the web backend.

## Delivered

- `AuditService.append(command)` appends immutable entries under a per-organization locked chain head.
- Canonical SHA-256 input is versioned, deterministic JSON over the organization, sequence, previous hash, and every `AuditCommand` value. UTC timestamps are normalized to microsecond precision.
- `verify_organization_chain` recalculates every row, continuity, organization scope, and the persisted chain head.
- `AuditVerificationMonitor` supports startup sampling and daily full verification. A failure emits a P0 reporter event and blocks new appends until `explicit_administrator_recovery`.
- Case transitions create an audit command in the same in-memory mutation boundary. If audit append is held or fails, the case update is not persisted and the API returns HTTP 503.
- `AuditRepository` defines the database extension point. Its contract requires `SELECT ... FOR UPDATE` of the chain-head row, entry insertion, and head advancement in one transaction; the application role must have only INSERT/SELECT privileges for immutable audit entries.

## Verification

- RED: `pytest apps/web-backend/tests/modules/audit/test_hash_chain.py -v` initially failed because the audit module did not exist.
- GREEN: `.venv-runtime/bin/pytest apps/web-backend/tests/modules/audit/test_hash_chain.py -v` passed: 5 tests.
- Regression suite: `.venv-runtime/bin/pytest apps/web-backend/tests -v` passed: 26 tests.
- Syntax check: `.venv-runtime/bin/python -m compileall -q apps/web-backend/src/odp_api` passed.
- Whitespace check: `git diff --check` passed.

The repository has Compose PostgreSQL (`deploy/compose.yaml`) but has no Alembic configuration, migrations directory, or Alembic dependency. Therefore no migration upgrade was runnable; the requested in-memory/testable adapter fallback is used while retaining the explicit transaction boundary for a future PostgreSQL adapter.
