# P1C Task 1 — Durable alert facts and Redis Gateway wake-ups

## Scope delivered

- Split the notification port into a read-only `InspectionAlertReadPort` and
  the explicit local/dev publisher port. The deployed API composes the
  read-only Gateway; the development trigger receives a separate publisher.
- Added `RedisGatewayInspectionAlertFeed`: one per-instance consumer group,
  canonical `inspection.alert.created.v1` envelope validation, PostgreSQL
  fact re-query before delivery, ACK after the yielded fact resumes, and
  graceful group destruction.
- Kept REST cursor reconciliation as the authoritative recovery path. Redis
  wake-ups carry no alert fact authority; malformed wake-ups are bounded and
  ACKed so they cannot poison the Gateway PEL.
- Aligned alert stream names with the P1B Relay/Retention contract:
  `odp:inspection:alerts`. The legacy direct publisher remains only for the
  explicit non-production dev trigger and now includes tenant/cursor hints for
  Gateway compatibility.
- Added shutdown cleanup for the Gateway consumer group in the API lifespan.

## Verification evidence

- Gateway contract tests: **3 passed**.
- Notification/Gateway/cursor/WebSocket regression selection: **32 passed**.
- Full backend after the P1C Task 1 cutover: **404 passed, 30 skipped**.
- Ruff and compileall: clean.
- `git diff --check`: clean.

## Boundary notes

- P1 inspection effects already persist `inspection_alerts` facts and an
  `inspection.alert.created.v1` Outbox row in the same PostgreSQL transaction;
  the new Gateway now consumes the Relay wake-up rather than publishing from
  the effect path.
- Real Redis consumer-group behavior remains environment-gated locally; the
  fake contract covers ACK/group lifecycle and fact re-query semantics. The
  Compose/CI Redis service remains the authoritative integration gate.
