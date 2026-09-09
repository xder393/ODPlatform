# Task 5 — Recorded-video, RTSP, and camera ingestion

## Scope delivered

- Added `FrameEnvelope`/`DecodedFrame` and source/session ports.
- Added lazy OpenCV adapters for recorded video, RTSP, and explicit local
  camera device indexes. Recorded replay loops deterministically and assigns
  current process wall-clock timestamps on every loop; RTSP/local sources use
  capped reconnect backoff.
- Added JPEG encoding as a separate adapter so sampling and health admission
  happen before encoding or object upload.
- Added a 10 FPS→2 FPS application service with separate decoded, sampled,
  encoded, admitted, rejected, and reason counters. Synchronous Saga/encoder
  calls are offloaded from the event loop.
- Added guarded SQLAlchemy inspection-session claims, persisted ingestor
  process ownership, heartbeat/stop/failure transitions, and a migration for
  the ownership column/index.
- Added `FrameIngestor` process orchestration and source-URI sanitization;
  credentials are not retained in the URI.
- Added unit/persistence/schema coverage for wall-clock replay, sampling,
  no-upload rejection, RTSP reconnect, local-device validation, session
  ownership, and migration parity.

## Verification evidence

- Ingestion module suite: 22 passed (including Task 4 tests).
- Session persistence/schema/startup suite: 10 passed, 2 skipped.
- Full backend: 392 passed, 29 skipped, 1 warning.
- Ruff on source/tests/Task5 migrations: clean.
- `compileall`: clean.
- `git diff --check`: clean.

## Known environment gate

`opencv-python-headless`, `numpy`, and `minio` are declared in
`apps/web-backend/pyproject.toml`, but this environment cannot resolve PyPI,
so `uv.lock` cannot yet be regenerated safely. The lock must be refreshed
before CI's frozen-lock check; no lock contents were hand-written.
