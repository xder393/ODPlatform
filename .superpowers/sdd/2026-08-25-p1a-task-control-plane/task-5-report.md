# Task 5 report

Initial RED: focused collection failed because `inspection_effects` was absent.
SQLite behavioral suite is GREEN (6 passed). Fixtures construct same-org
composite keys at creation time. PostgreSQL concurrency coverage is gated by
`ODP_POSTGRES_TEST_URL`, with independent sessions, barrier, futures, and
cleanup. PostgreSQL/full-suite verification requires that integration URL.

## Verification evidence

Implementation includes the published-effect contract/service, the single
transactional SQLAlchemy adapter, SQLite behavioral tests, and the gated
PostgreSQL concurrency test. Existing UoW/repositories needed no changes:
publication owns one SQLAlchemy session transaction and does not nest commits.

Initial RED command: `pytest apps/web-backend/tests/modules/inspection/test_effects.py -q`.
The system executable was unavailable (`zsh: command not found: pytest`); the
backend environment then collected zero tests and failed with
`ModuleNotFoundError: No module named 'odp_api.adapters.persistence.inspection_effects'`.

Focused/UoW command:
`uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/inspection/test_effects.py apps/web-backend/tests/persistence/test_postgres_case_deduplication.py apps/web-backend/tests/persistence/test_case_audit_uow.py -q`
Result: `11 passed, 2 skipped in 1.06s`.

Task 3 regression command:
`uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/tasks/test_fenced_execution.py apps/web-backend/tests/persistence/test_postgres_fencing.py -q`.
It was included in the full run: Task 3 local tests passed (PostgreSQL-gated
tests skipped).

Full backend command:
`uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests -q`
Result: `217 passed, 10 skipped in 13.25s`, no warnings.

Ruff command on every changed Python file reported `All checks passed!`;
`git diff --check` reported no output.

The PostgreSQL test uses two independent claims/sessions, a Barrier and
futures with propagated exceptions, and asserts two results/events, one case,
one active episode, and two alert outbox rows. It is skipped locally because
`ODP_POSTGRES_TEST_URL` is unset.

Self-review confirms camera-first locking; tenant/owner/fence/attempt guards;
DB completion time; duplicate comparison of hash, detections, and execution;
no nested commits; rollback/no-effect behavior; predecessor-linked audit hash;
and one alert outbox row per event. The failure hook is adapter constructor
injection used only by tests and is not part of the production service API.

Concern: PostgreSQL locking and unique-key concurrency remain CI-gated.

## Fix Round 2 evidence

The two findings addressed were unconstrained detection confidence, which
could persist a feed payload rejected by `InspectionAlert`, and incorrect
timezone relabeling of aware database timestamps. The invalid-confidence tests
cover both `-0.1` and `1.5`, asserting rollback and a still-live task. A
pre-implementation RED run for these new tests was not captured; this report
does not claim one. The aware non-UTC normalization regression likewise was
implemented and verified, but no separate pre-implementation RED output was
recorded.

Exact GREEN command:

```text
uv run --project apps/web-backend --extra dev --with-editable packages/shared-schemas --with httpx2 pytest apps/web-backend/tests/modules/inspection/test_effects.py -q
```

Result: `21 passed in 0.86s`.

Ruff was run on the changed adapter and effects tests and finished with no
errors (`All checks passed!` after import/style fixes). `git diff --check`
reported no output. The implementation constructs the actual shared
`InspectionAlert` and stores `model_dump(mode="json")`; `_utc` attaches UTC to
naive DB values and uses `astimezone(UTC)` for aware values.

Concern: the focused normalization test uses the adapter helper indirectly;
PostgreSQL-specific DB clock behavior remains integration-gated.

## Fix Round 1 evidence

The hardening sequence preserved history: `e2f3c25` report, canonical audit
fix `ec4f2b7`, boundary fix `729c0b7`, rollback test `9d55905`, Phase 3A revert
`182fe32` and recovery `b9b1480`, Phase 3B revert `b4290a2` and recovery
`2a57e21`. The final net implementation also includes `1eb3d0c`'s audit/feed
regression history and its explicit revert/recovery sequence.

Exact verification commands and results:

- Focused effects, PG case dedup, case-audit UoW, alert feed, Task 3 fencing,
  and PG fencing: `52 passed, 7 skipped in 2.06s`.
- Full backend shared-schemas/httpx2 command (`uv run ... pytest
  apps/web-backend/tests -q`): `230 passed, 10 skipped in 13.07s`.
- Ruff on all Task 5 production/tests: `All checks passed!`.
- `git diff --check`: clean.

Self-review confirms canonical same-session audit head/hash append, schema-valid
alert payload, DB-time lease fence, complete attempt identity, shell-before-case
episode claim, full duplicate comparison, camera-first lock and missing-anchor
StaleLease, one commit boundary without nested commits, and rollback/no-effect
paths. Publish-level PG concurrency remains gated and the requested lower-level
episode primitive concurrency test is deferred; `ODP_POSTGRES_TEST_URL` is not
configured locally.
