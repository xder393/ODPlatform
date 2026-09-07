# P1C Task 3 — operations UI increment

## Delivered against the approved UI scope

- Added typed clients for actor scope, inspection sessions, task list/detail,
  dead-letter replay, and evidence links.
- Integrated a dedicated operations section in the existing workbench.
  The profile endpoint supplies roles and line scope. Inspectors see read-only
  operational information and evidence access; supervisors/admins have session
  controls and dead-letter replay. The backend remains the permission authority.
- Session start preserves the request's idempotency key after network failure;
  changed input receives a new key. Submitted start/stop requests are shown as
  pending rather than reporting that the camera has already changed state.
- Task lists have status filtering, a 100-row server cap and bounded scrolling.
  Task detail loads attempt/error and dispatch history. Replay reports the new
  server-returned task ID and disables repeat submission after success.
- Polling runs every ten seconds after the prior read finishes, pauses when
  hidden, and cancels reads/timers on unmount. Manual refresh also recovers a
  failed initial profile request.
- Evidence URLs are fetched on demand, restricted to HTTP(S), retained only in
  component memory and cleared at expiry or when leaving the detail. The user
  opens them through a new-tab link with noopener/noreferrer.

## Verification

- Initial new UI test run failed because OperationsPanel did not exist.
- Added profile-recovery regression reproduced a real failure before the fix:
  refresh could not recover a failed profile read. The corrected dependency
  makes it pass.
- Full frontend Vitest suite: **8 files, 42 tests passed**.
- `npm run build`: passed (TypeScript and Vite production build).
- `npm run typecheck:e2e`: passed.
- `git diff --check`: passed.
- Locked dependencies installed with `npm ci`; package manifests/lock unchanged.

## Remaining acceptance boundaries

- No browser E2E or visual screenshot validation was executed in this increment.
  UI tests use real components/API clients with a controlled HTTP boundary.
- Current backend schemas do not expose sampled FPS, worker health, queue age,
  or model release in task diagnostics. These metrics are not fabricated in UI.
- Evidence is reached through task detail because current case summaries do
  not include artifact IDs. Case-linked preview and alert-source/latency display
  from the broader P1C plan remain follow-ups.
- The Compose API currently presigns against its internal MinIO endpoint.
  Externally reachable signed URLs still require deployment configuration and
  real-browser verification in the delivery gate.
- Task detail is loaded on selection; the task list and session list poll.
  Real recorded-video E2E and process composition remain separate gates.
