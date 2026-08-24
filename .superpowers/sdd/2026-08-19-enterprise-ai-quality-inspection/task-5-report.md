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

## P0 security follow-up — runtime authentication

Removed the global runtime dependency override that supplied a fixed administrator. Runtime protected routes now require an HS256-verified Bearer JWT, resolve its UUID subject through the configured actor repository, and return `401` for missing, malformed, invalidly signed, unconfigured, or unknown-subject credentials. The runtime contains no demo administrator and no public password verifier; reauthentication receives its actor solely through the authenticated JWT dependency. Deterministic actors are limited to FastAPI dependency overrides in test application composition.

Added integration coverage for missing and invalid credentials, distinct authenticated tenant callers, and password reauthentication for the verified JWT subject.

Exact covering command and output:

```text
$ uv run --project apps/web-backend pytest apps/web-backend/tests -q
collected 18 items
18 passed, 1 warning in 0.17s
```

## P1/P2 review follow-up — WebSocket and malformed JWT handling

The inspection-event WebSocket no longer tries to run `HTTPBearer(Request)` during its handshake. It extracts a Bearer credential from the WebSocket authorization header, verifies it through the same runtime JWT authenticator and actor resolver, and closes missing or invalid connections with policy-violation code `1008`. Test-only FastAPI dependency overrides remain supported for the deterministic notification fixture.

JWT verification now requires both decoded header and payload JSON values to be objects. Array-shaped signed sections and other malformed structures are normalized to `InvalidJwtSubject`, so HTTP authentication returns `401` rather than propagating a server error.

Exact covering command and output:

```text
$ uv run --project apps/web-backend pytest apps/web-backend/tests/integration/test_runtime_authentication.py apps/web-backend/tests/integration/test_notification_api.py -q
collected 7 items
7 passed, 1 warning in 0.16s
```
