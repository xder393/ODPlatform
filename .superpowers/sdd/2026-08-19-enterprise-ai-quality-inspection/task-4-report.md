# Task 4 report: realtime inspection workbench

## Delivered

- Added `GET /api/v1/inspection-events?updated_after=<ISO8601>` and
  `WS /ws/inspection-events`, registered by `odp_api.main.create_app`.
- The WebSocket has no client acknowledgement path and sends an event once per
  connection. Reconciliation remains REST-based.
- Added `useInspectionFeed(since)` with the required result shape. On every
  socket open it fetches reconciliation results before flushing queued socket
  messages; event IDs deduplicate any overlap.
- Added the React workbench with a video placeholder, alert details, case
  action controls, reconnection status, and an assertive accessible live region.

## Test evidence

- `PYTHONPATH="apps/web-backend/src:apps/platform/src:packages/shared-schemas/src" .venv-runtime/bin/pytest apps/platform/tests apps/web-backend/tests -v`
  — 211 passed, 1 existing FastAPI/TestClient deprecation warning.
- `npm test --prefix apps/web-frontend -- --run RealtimeWorkbench.test.tsx`
  — 1 passed.
- `npm run build --prefix apps/web-frontend` — passed.

## npm installation investigation

The initial install command appeared stalled because the command wrapper
returned after 30 seconds while npm continued in the background. Evidence:

- Node `v24.18.1`; npm `11.16.0`.
- Registry `https://registry.npmjs.org/`; cache `/Users/xder393/.npm`.
- The first thirty npm log lines show the offline install resolving manifests
  from stale cache in 1–4 ms (`~/.npm/_logs/2026-08-20T06_15_51_573Z-debug-0.log`,
  lines 1–30).
- The same log ends with `verbose exit 0` and `info ok`; it created
  `apps/web-frontend/package-lock.json` and populated `node_modules`.

The minimal successful approach was `npm install --prefix apps/web-frontend
--offline --no-audit --no-fund`; no workaround or dependency substitution was
needed.

## Concerns

- The local task runtime created untracked `apps/web-backend/.venv` and
  `apps/web-backend/uv.lock`; neither is part of this task’s commit.
