# P1B Task 2 — Inference consumer and Worker recovery loop

## Checkpoint status

This checkpoint covers the unit-level implementation and verification of the
Redis inference consumer and Worker orchestration. The real Redis/PostgreSQL
recovery tests were intentionally not executed in this bounded checkpoint;
they remain required before Task 2 can be considered fully verified.

## TDD evidence

The first test run was deliberately made before the production implementation
and failed during collection with:

```text
ModuleNotFoundError: No module named 'odp_api.processes.inference_worker'
```

The implementation was then added behind the existing TaskExecutionPort and
InspectionEffectService boundaries. The unit suite now exercises the real
Worker orchestration rather than a duplicate task state machine.

## Implemented behavior

- Redis consumer-group setup tolerates `BUSYGROUP` only, preserves a stable
  process UUID consumer name, parses new deliveries and `XAUTOCLAIM` response
  shapes, and returns deliberate ACK counts.
- Worker delivery parsing is bounded by the canonical 64 KiB envelope limit;
  malformed, oversized, unsupported-schema, and unsupported-event deliveries
  are quarantined before ACK when authoritative metadata is available.
- Tenant/task data is reloaded from the authoritative task port. Terminal or
  stale dispatch references ACK without creating an Attempt; a live foreign
  lease remains pending.
- Claimed work starts a 5-second renewal loop, enforces a 10-second inference
  deadline, classifies timeout/model/input/infra failures, and records failure
  before ACK.
- Successful work commits through `InspectionEffectService.publish()` before
  ACK. Cancellation and lost/stale leases do not ACK. Renewal tasks are
  stopped and awaited on every exit path.
- Worker-owned recovery uses `XAUTOCLAIM` with a 20-second idle threshold and
  feeds reclaimed deliveries through the same processing path.

## Verification

Command:

```text
uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas pytest apps/web-backend/tests/modules/tasks/test_inference_consumer.py -q
```

Result: `8 passed in 0.23s`.

Command:

```text
uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas ruff check apps/web-backend/src/odp_api/modules/tasks/consumer.py apps/web-backend/src/odp_api/processes/inference_worker.py apps/web-backend/tests/modules/tasks/test_inference_consumer.py
```

Result: `All checks passed!`. The staged diff also passes `git diff --cached
--check`.

## Remaining verification and concerns

- `tests/integration/test_redis_worker_recovery.py` was not run in this
  checkpoint, as requested. It must run against the CI-provisioned
  `ODP_POSTGRES_TEST_URL` and `ODP_REDIS_TEST_URL` services and verify Worker A
  cancellation, Worker B `XAUTOCLAIM` recovery, commit-before-ACK redelivery,
  and the one-RUNNING-per-camera invariant.
- P1A fencing/effects regressions, the expanded Ruff scope, the full backend
  suite, and the lockfile check remain for the parent integration checkpoint.
- Production MinIO and ONNX adapters are intentionally not hard-coded here;
  artifact loading and inference remain injected ports for Tasks 4/6/7.
