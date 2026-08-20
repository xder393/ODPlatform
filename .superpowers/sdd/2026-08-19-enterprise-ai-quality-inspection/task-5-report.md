# Task 5 Report — Organization scope, RBAC, and reauthentication

## Status

Implemented the in-memory enterprise-security slice.

- `authorize` enforces matching organization, role grants, and inspector production-line scope.
- Case and inspection-alert repositories receive the actor organization before returning records; mutation paths also authorize in the case service.
- The application injects a deterministic demo actor through FastAPI dependency overrides, so existing vertical tests remain unauthenticated at the transport level without bypassing authorization.
- `POST /api/v1/auth/reauthenticate` verifies the local demo password and records `last_reauth_at:<actor_id>` for 300 seconds.
- Pause is a reauthentication-protected simulation endpoint and never controls a device.

## Verification

- Passed: `uv run --project apps/web-backend pytest apps/web-backend/tests -q` — 15 passed.
- Passed: `apps/web-backend/.venv/bin/python -m compileall -q apps/web-backend/src`.
- Passed: `git diff --check`.
- The root Python suite could not collect because the workspace test environment lacks pre-existing platform dependencies (`numpy`, `scikit-learn`, `colorlog`, and `pyyaml`). The backend-specific suite above is green.

## Self-review

Reviewed tenant filtering, exact line authorization, actor dependency injection, reauthentication TTL boundary behavior, and simulation-only pause response. No unresolved code issues found.

## Notes

Pre-existing untracked `.venv-runtime/` and `apps/web-backend/uv.lock` were not included.
