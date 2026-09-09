# Evidence storage and browser follow-up

## Fixes

- API evidence signing now supports a browser-visible MinIO endpoint with
  independent TLS and explicit region configuration. Internal reads/writes keep
  the internal endpoint; signed URLs are never rewritten after signing.
- Compose defaults to public `localhost:9000` with HTTP for local development.
  Deployment documentation covers remote hostname, HTTPS, region, and proxy Host
  preservation.
- Real MinIO regression exposed an existing upload failure: SDK `_execute`
  expects bytes but received BytesIO, causing TypeError before upload. Passing
  the original bytes fixes the conditional PUT while preserving If-None-Match.
- Added Chrome/Chromium browser coverage of both operational roles, session
  start/stop, task diagnostics, replay, evidence popup and narrow viewport.
  Browser test artifacts are ignored by Git.

## Evidence

- Public-endpoint unit regression first failed with unexpected constructor
  argument, then passed with a real SDK offline signature and original internal
  object reads.
- Actual isolated MinIO service on `127.0.0.1:29001`: signed HTTP GET returned
  the original bytes; changing the host to its loopback alias returned 403.
- Existing two real MinIO upload tests first failed on `len(BytesIO)` inside the
  SDK; after the fix, upload/read/presign/immutable overwrite and public URL
  selection passed together: **9 passed**.
- Full backend with real MinIO configured: **415 passed, 29 skipped**.
- Frontend Vitest: **42 passed**; production build and E2E TypeScript passed.
- Installed Chrome, Playwright `operations.spec.ts`: **2 passed**.
- Ruff, compileall, Compose configuration and diff whitespace checks passed.

## Reproduction

The integration test uses existing CI `ODP_MINIO_TEST_ENDPOINT`,
`ODP_MINIO_TEST_ACCESS_KEY`, `ODP_MINIO_TEST_SECRET_KEY` variables and a loopback
service port. It creates a unique temporary bucket and removes its object and
bucket in finally. Run `pytest tests/integration/test_public_evidence_url.py
tests/integration/test_minio_artifacts.py` from the backend with these variables.

The browser suite used `PLAYWRIGHT_CHANNEL=chrome
E2E_BASE_URL=http://127.0.0.1:5179 npm run test:e2e -- operations.spec.ts` with a
local Vite server. These are controlled-HTTP UI tests, not proof of a running
video/worker pipeline. Real MinIO HTTP and browser UI were tested separately;
the complete Compose video-to-case-to-evidence flow is still pending.

PostgreSQL/Redis environment gates remain skipped in the full backend command.
The pre-existing dependency lock/runtime-composition gaps are not resolved by
this increment.
