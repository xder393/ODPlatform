# Enterprise P0 Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the quality-inspection demo durable across restarts, secure its credentials, and deliver a genuinely continuous real-time alert stream in both Docker and Docker-free local development.

**Architecture:** Keep the existing domain models and ports/adapters direction, replace runtime `InMemory*` composition with SQLAlchemy repositories backed by PostgreSQL or SQLite, and use one unit-of-work transaction for case transitions plus audit appends. Authenticate REST with expiring HS256 JWTs and WebSockets with one-time 60-second tickets; deliver alerts through a durable cursor-based feed using Redis `XREAD` in Docker and SQLite plus a condition variable locally.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, SQLite, Redis 7, Argon2id, React 19, TypeScript, Vitest, Playwright, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-enterprise-p0-hardening-design.md`

## Global Constraints

- Docker mode uses PostgreSQL for actors, credentials, cases, inspection events, case transitions and audit chains; Redis stores reauthentication markers and carries cross-instance alert notifications.
- Docker-free local mode persists the same business state in SQLite and remains runnable without PostgreSQL or Redis.
- No runtime environment may silently fall back to process-local business state after a database or Redis failure.
- A case transition, transition-history insert and audit-chain append commit or roll back as one database transaction.
- Passwords use Argon2id hashes; no plaintext password is persisted or logged.
- JWT access tokens require HS256, UUID `sub`, integer `iat`, integer `exp`, a non-expired `exp`, and an `iat` no more than 60 seconds in the future; access-token TTL remains 12 hours.
- Browser WebSockets use random one-time tickets with a 60-second TTL, never the access JWT in the URL.
- WebSocket delivery remains at-most-once; durable alert storage and cursor reconciliation are the source of truth.
- Redis subscriptions use blocking `XREAD` from a cursor and bounded streams, not full-history `XRANGE - +` polling.
- Existing RAG confidence, citation, tenant filtering and LOW-risk behavior must not change.
- Every production behavior is implemented test-first and each task ends with a focused commit.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `apps/web-backend/src/odp_api/db.py` | SQLAlchemy engine/session factory and backend detection. |
| `apps/web-backend/src/odp_api/adapters/persistence/models.py` | SQLAlchemy tables shared by PostgreSQL and SQLite. |
| `apps/web-backend/src/odp_api/adapters/persistence/repositories.py` | Actor, credential, case, audit and alert repositories. |
| `apps/web-backend/src/odp_api/adapters/persistence/unit_of_work.py` | Atomic case/history/audit transaction boundary. |
| `apps/web-backend/src/odp_api/modules/cases/ports.py` | Infrastructure-independent case and unit-of-work protocols. |
| `apps/web-backend/src/odp_api/modules/cases/application.py` | Authorized transition application service. |
| `apps/web-backend/src/odp_api/modules/identity/ports.py` | Credential, reauthentication and WebSocket-ticket protocols. |
| `apps/web-backend/src/odp_api/modules/identity/tickets.py` | One-time ticket issuance and consumption service. |
| `apps/web-backend/src/odp_api/adapters/auth/argon2.py` | Argon2 password hashing/verifying adapter. |
| `apps/web-backend/src/odp_api/adapters/auth/redis_security.py` | Redis reauthentication markers and one-time tickets. |
| `apps/web-backend/src/odp_api/adapters/notifications/sqlite_feed.py` | Local durable cursor feed and live subscription. |
| `apps/web-backend/src/odp_api/adapters/notifications/redis_stream.py` | PostgreSQL fact persistence plus Redis live subscription. |
| `apps/web-backend/alembic/` | Cross-database P0 schema migrations. |

### Task 1: Add the cross-database persistence foundation and idempotent identities

**Files:**
- Modify: `apps/web-backend/pyproject.toml`
- Modify: `apps/web-backend/src/odp_api/settings.py`
- Create: `apps/web-backend/src/odp_api/db.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/__init__.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/models.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/repositories.py`
- Create: `apps/web-backend/src/odp_api/modules/identity/ports.py`
- Create: `apps/web-backend/alembic.ini`
- Create: `apps/web-backend/alembic/env.py`
- Create: `apps/web-backend/alembic/versions/0001_p0_business_state.py`
- Modify: `apps/web-backend/src/odp_api/seed.py`
- Test: `apps/web-backend/tests/persistence/test_identity_repository.py`
- Test: `apps/web-backend/tests/integration/test_seed_persistence.py`

**Interfaces:**
- Produces `create_engine_and_session(database_url: str) -> tuple[Engine, sessionmaker[Session]]`.
- Produces `ActorRepositoryPort.get(actor_id: UUID) -> Actor | None` and `get_by_email(email: str) -> Actor | None`.
- Produces `PasswordCredentialPort.password_hash(actor_id: UUID) -> str | None`.
- Produces `SqlAlchemyActorRepository(session_factory)` and `SqlAlchemyPasswordCredentialRepository(session_factory)`.
- Produces `seed_business_data(session_factory, seed: DemoSeed, hash_password: Callable[[str], str]) -> None`, idempotent by stable actor/case/event IDs.

- [ ] **Step 1: Write failing repository and restart tests.**

```python
def test_sqlite_actor_and_credential_survive_new_session(tmp_path):
    _, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(sessions.kw["bind"])
    seed_business_data(sessions, build_demo_seed(), hasher.hash)
    actor = SqlAlchemyActorRepository(sessions).get_by_email("inspector@example.test")
    assert actor is not None
    assert actor.role is Role.INSPECTOR
    assert credential_repository.password_hash(actor.actor_id).startswith("$argon2id$")

def test_repeated_seed_does_not_duplicate_business_rows(session_factory):
    seed_business_data(session_factory, build_demo_seed(), hasher.hash)
    seed_business_data(session_factory, build_demo_seed(), hasher.hash)
    assert count_rows(session_factory, ActorRow) == 3
    assert count_rows(session_factory, DefectCaseRow) == 10
    assert count_rows(session_factory, InspectionEventRow) == 10
```

- [ ] **Step 2: Run the tests and verify RED.**

Run: `../../.venv-runtime/bin/pytest tests/persistence/test_identity_repository.py tests/integration/test_seed_persistence.py -v`

Expected: collection fails because `odp_api.db`, persistence models and repository ports do not exist.

- [ ] **Step 3: Add dependencies and settings.**

Add runtime dependencies `SQLAlchemy>=2.0,<3`, `alembic>=1.13,<2`, `argon2-cffi>=23.1,<26`, and `uvicorn[standard]>=0.30,<1`. Add `database_url: str = "sqlite:////tmp/odp-quality-inspection.sqlite3"`; Docker overrides it with `postgresql+psycopg://odp:odp@postgres:5432/odp`.

- [ ] **Step 4: Implement SQLAlchemy tables and repositories.**

Define typed declarative rows for actors, line grants, password credentials, cases, inspection events, transitions, chain heads, audit logs, alerts and WebSocket tickets. Every tenant-owned row includes an indexed `organization_id`; case and event identifiers remain the deterministic UUIDs from `DemoSeed`.

- [ ] **Step 5: Implement idempotent business seeding.**

Use one transaction and primary-key/unique-email lookups. Hash a documented demo password only when its credential row is first inserted; never replace an existing hash during restart. Persist cases and events with insert-if-absent semantics.

- [ ] **Step 6: Configure and execute Alembic on SQLite.**

Run: `cd apps/web-backend && ../../.venv-runtime/bin/alembic upgrade head`

Expected: all P0 tables exist and a second invocation reports no pending migration.

- [ ] **Step 7: Run focused and full backend tests.**

Run: `../../.venv-runtime/bin/pytest tests/persistence/test_identity_repository.py tests/integration/test_seed_persistence.py -v && ../../.venv-runtime/bin/pytest tests -q`

Expected: focused tests pass and existing backend behavior remains green.

- [ ] **Step 8: Commit.**

```bash
git add apps/web-backend
git commit -m "feat: add durable business persistence foundation"
```

### Task 2: Make case history and audit chains durable and atomic

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/cases/ports.py`
- Create: `apps/web-backend/src/odp_api/modules/cases/application.py`
- Create: `apps/web-backend/src/odp_api/adapters/persistence/unit_of_work.py`
- Modify: `apps/web-backend/src/odp_api/adapters/persistence/repositories.py`
- Modify: `apps/web-backend/src/odp_api/modules/cases/router.py`
- Modify: `apps/web-backend/src/odp_api/modules/audit/service.py`
- Modify: `apps/web-backend/src/odp_api/modules/audit/verify.py`
- Modify: `apps/web-backend/src/odp_api/main.py`
- Test: `apps/web-backend/tests/persistence/test_case_audit_uow.py`
- Test: `apps/web-backend/tests/integration/test_runtime_persistence.py`

**Interfaces:**
- Produces `CaseRepositoryPort.list(organization_id, updated_after)`, `get(case_id, organization_id)` and `history(case_id, organization_id)`.
- Produces `BusinessUnitOfWorkPort` with `cases`, `audits`, `commit()` and rollback-on-exit behavior.
- Produces `CaseApplicationService.transition(case_id, to_status, actor, audit_context) -> StoredCase`.
- Extends `CaseSummary` with `history: list[CaseTransitionSummary]` where each entry contains `from_status`, `to_status`, `actor_id`, `occurred_at`, and `correlation_id`.

- [ ] **Step 1: Write failing atomicity and restart tests.**

```python
def test_transition_history_and_audit_survive_app_restart(sqlite_settings, seed):
    with TestClient(create_app(settings=sqlite_settings, seed=seed)) as first:
        token = login(first)
        case_id = first.get("/api/v1/cases", headers=auth(token)).json()[0]["case_id"]
        first.post(f"/api/v1/cases/{case_id}/transitions", json={"status": "IN_REVIEW"}, headers=auth(token)).raise_for_status()
    with TestClient(create_app(settings=sqlite_settings, seed=seed)) as restarted:
        payload = restarted.get("/api/v1/cases", headers=auth(login(restarted))).json()[0]
        assert payload["status"] == "IN_REVIEW"
        assert payload["history"][-1]["to_status"] == "IN_REVIEW"
        assert restarted.app.state.audit_service.verify_organization_chain(DEMO_ORG_ID).is_valid

def test_audit_failure_rolls_back_case_and_history(uow_factory):
    service = CaseApplicationService(uow_factory)
    with pytest.raises(AuditWriteError):
        service.transition(CASE_ID, "IN_REVIEW", ACTOR, context(), fail_audit=True)
    assert load_case(uow_factory).status == "PENDING_CONFIRMATION"
    assert load_history(uow_factory) == []
```

- [ ] **Step 2: Run tests and verify RED.**

Run: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py tests/integration/test_runtime_persistence.py -v`

Expected: failures show runtime composition still uses in-memory case/audit repositories and summaries have no history.

- [ ] **Step 3: Implement SQLAlchemy case and audit repositories plus unit of work.**

The application service loads the tenant-scoped case, calls the existing `CaseService.transition`, inserts a transition row, appends a canonical hash-chain entry under the organization head lock, saves the case status and commits once. PostgreSQL uses row locks; SQLite begins an immediate write transaction.

- [ ] **Step 4: Rewire routes and persistent verification.**

`create_app()` must create SQLAlchemy repositories for both local and Docker environments. Startup/daily verification reads persisted organization IDs and chain entries. In-memory adapters remain available only when tests explicitly inject them.

- [ ] **Step 5: Use middleware correlation ID in every audit command.**

Read `get_correlation_id()` when the request did not supply a valid UUID and persist the generated request correlation ID. Add an audit entry for successful simulated pause commands.

- [ ] **Step 6: Run focused concurrency, restart and full backend tests.**

Run: `../../.venv-runtime/bin/pytest tests/persistence/test_case_audit_uow.py tests/integration/test_runtime_persistence.py tests/modules/audit/test_hash_chain.py tests/integration/test_cases_api.py -v && ../../.venv-runtime/bin/pytest tests -q`

Expected: atomicity, chain continuity, restart persistence and existing authorization pass.

- [ ] **Step 7: Commit.**

```bash
git add apps/web-backend
git commit -m "feat: persist atomic case and audit workflows"
```

### Task 3: Enforce credential security and one-time WebSocket tickets

**Files:**
- Create: `apps/web-backend/src/odp_api/adapters/auth/argon2.py`
- Create: `apps/web-backend/src/odp_api/adapters/auth/redis_security.py`
- Create: `apps/web-backend/src/odp_api/modules/identity/tickets.py`
- Modify: `apps/web-backend/src/odp_api/modules/identity/ports.py`
- Modify: `apps/web-backend/src/odp_api/modules/identity/service.py`
- Modify: `apps/web-backend/src/odp_api/adapters/auth/jwt.py`
- Modify: `apps/web-backend/src/odp_api/adapters/redis_stream.py`
- Modify: `apps/web-backend/src/odp_api/main.py`
- Modify: `deploy/nginx.conf`
- Test: `apps/web-backend/tests/modules/identity/test_jwt_security.py`
- Test: `apps/web-backend/tests/integration/test_websocket_tickets.py`

**Interfaces:**
- Produces `Argon2PasswordVerifier(credentials: PasswordCredentialPort)` with `verify(actor_id, password) -> bool` and `hash(password) -> str`.
- Produces `WebSocketTicketService.issue(actor_id, now) -> str` and `consume(ticket, now) -> UUID`; tickets contain at least 256 random bits, expire after 60 seconds, and are removed atomically on consumption.
- Produces `POST /api/v1/auth/websocket-ticket -> {"ticket": str, "expires_in": 60}` under Bearer authentication.
- Changes WebSocket authentication to accept only `?ticket=...`, then reload the actor from the persistent actor repository.

- [ ] **Step 1: Write failing JWT security tests.**

```python
@pytest.mark.parametrize("claims", [
    {"sub": ACTOR_ID, "iat": NOW},
    {"sub": ACTOR_ID, "exp": NOW + 60},
    {"sub": ACTOR_ID, "iat": NOW + 61, "exp": NOW + 3600},
    {"sub": ACTOR_ID, "iat": NOW - 3600, "exp": NOW - 1},
])
def test_rejects_invalid_temporal_claims(claims):
    with pytest.raises(InvalidJwtSubject):
        extract_subject(sign_claims(claims), SECRET, now=datetime.fromtimestamp(NOW, UTC))
```

- [ ] **Step 2: Write failing password and ticket tests.**

```python
def test_password_database_contains_argon2_hash_and_login_still_works(client, credential_repo):
    assert credential_repo.password_hash(INSPECTOR_ID).startswith("$argon2id$")
    assert login(client, "odp-inspector-dev").status_code == 200

def test_websocket_ticket_is_single_use_and_expires(ticket_service):
    ticket = ticket_service.issue(ACTOR_ID, NOW)
    assert ticket_service.consume(ticket, NOW) == ACTOR_ID
    with pytest.raises(InvalidWebSocketTicket):
        ticket_service.consume(ticket, NOW)
    expired = ticket_service.issue(ACTOR_ID, NOW)
    with pytest.raises(InvalidWebSocketTicket):
        ticket_service.consume(expired, NOW + timedelta(seconds=61))
```

- [ ] **Step 3: Run tests and verify RED.**

Run: `../../.venv-runtime/bin/pytest tests/modules/identity/test_jwt_security.py tests/integration/test_websocket_tickets.py -v`

Expected: expired JWT is accepted, credentials are plaintext and the ticket endpoint/service are absent.

- [ ] **Step 4: Implement strict JWT validation and Argon2 verification.**

Reject missing/non-integer temporal claims, expired tokens and `iat` more than 60 seconds in the future. Use injected `now` only for deterministic tests. Replace runtime `InMemoryPasswordVerifier` with the persistent Argon2 adapter.

- [ ] **Step 5: Implement local and Redis security stores.**

SQLite consumes a ticket with a conditional `DELETE ... RETURNING` transaction. Redis stores `odp:ws-ticket:<digest>` with `SET EX 60 NX` and consumes through `GETDEL`; reauthentication markers use `SET EX 300` and actor-scoped keys.

- [ ] **Step 6: Remove JWT query authentication and sanitize access logs.**

The browser-facing WebSocket path accepts only a one-time ticket. Configure nginx WebSocket access logs without `$request_uri`; never include ticket/JWT values in application logs or exception messages.

- [ ] **Step 7: Run focused and full security tests.**

Run: `../../.venv-runtime/bin/pytest tests/modules/identity/test_jwt_security.py tests/integration/test_websocket_tickets.py tests/integration/test_login_api.py tests/integration/test_runtime_authentication.py -v && ../../.venv-runtime/bin/pytest tests -q`

Expected: all invalid tokens and reused/expired tickets are rejected while login and reauthentication work across restart.

- [ ] **Step 8: Commit.**

```bash
git add apps/web-backend deploy/nginx.conf
git commit -m "feat: secure runtime credentials and websocket tickets"
```

### Task 4: Deliver a durable, continuous cursor-based alert feed

**Files:**
- Modify: `apps/web-backend/src/odp_api/ports/notifications.py`
- Create: `apps/web-backend/src/odp_api/adapters/notifications/sqlite_feed.py`
- Modify: `apps/web-backend/src/odp_api/adapters/notifications/redis_stream.py`
- Modify: `apps/web-backend/src/odp_api/adapters/redis_stream.py`
- Modify: `apps/web-backend/src/odp_api/modules/notifications/router.py`
- Modify: `apps/web-backend/src/odp_api/main.py`
- Modify: `apps/web-backend/src/odp_api/observability/metrics.py`
- Test: `apps/web-backend/tests/integration/test_live_notification_api.py`
- Test: `apps/web-backend/tests/persistence/test_alert_feed_contract.py`

**Interfaces:**
- Adds opaque string `cursor` to every alert envelope returned by reconciliation or WebSocket.
- Produces `InspectionAlertFeedPort.publish(alert, line_id) -> str`, `list(organization_id, after_cursor, limit) -> list[StoredInspectionAlert]`, and async `subscribe(after_cursor) -> AsyncIterator[StoredInspectionAlert]`.
- REST accepts `after_cursor: str | None` and returns `{ "items": [...], "next_cursor": str | None }`.
- WebSocket accepts `ticket` and optional `cursor`, stays open, and pushes `{ "cursor": str, "alert": InspectionAlert }` for authorized future events.

- [ ] **Step 1: Write failing provider contract tests.**

```python
@pytest.mark.anyio
async def test_subscriber_receives_event_published_after_subscription(feed):
    subscription = feed.subscribe(None)
    pending = anext(subscription)
    await asyncio.sleep(0)
    cursor = feed.publish(ALERT, LINE_ID)
    received = await asyncio.wait_for(pending, timeout=1)
    assert received.cursor == cursor
    assert received.alert.event_id == ALERT.event_id

def test_reconciliation_returns_only_events_after_cursor(feed):
    first = feed.publish(ALERT_1, LINE_ID)
    second = feed.publish(ALERT_2, LINE_ID)
    assert [item.cursor for item in feed.list(ORG_ID, first, 100)] == [second]
```

- [ ] **Step 2: Write failing live WebSocket integration test.**

Open an authenticated WebSocket, consume initial reconciliation, publish a new alert through `app.state.inspection_alert_feed`, and assert the same open socket receives it without reconnecting.

- [ ] **Step 3: Run tests and verify RED.**

Run: `../../.venv-runtime/bin/pytest tests/persistence/test_alert_feed_contract.py tests/integration/test_live_notification_api.py -v`

Expected: existing feed has no cursor/subscription API and the socket returns after its initial snapshot.

- [ ] **Step 4: Implement SQLite durable feed.**

Persist alert facts with monotonic integer cursors and unique `event_id`; use a condition variable only as a wake-up optimization. Subscription always re-queries the database after wake/timeout so process restart cannot lose facts.

- [ ] **Step 5: Implement bounded Redis live transport.**

Persist the fact in `inspection_alerts`, publish the envelope with `XADD MAXLEN ~ 10000`, and block with `XREAD BLOCK 15000 COUNT 100 STREAMS odp:inspection-alerts <cursor>`. Treat timeout as a heartbeat opportunity and cancellation as normal disconnect.

- [ ] **Step 6: Keep WebSockets open and enforce authorization per event.**

After ticket consumption, send the authorized backlog and then iterate the subscription until `WebSocketDisconnect` or cancellation. Increment `websocket_active_connections`, `websocket_reconnect_total`, and `websocket_reconciled_events_total` without labeling by tenant/user.

- [ ] **Step 7: Run focused, notification and full backend tests.**

Run: `../../.venv-runtime/bin/pytest tests/persistence/test_alert_feed_contract.py tests/integration/test_live_notification_api.py tests/integration/test_notification_api.py -v && ../../.venv-runtime/bin/pytest tests -q`

Expected: a single connection receives future alerts, tenant/line isolation holds, and cursor reconciliation is incremental.

- [ ] **Step 8: Commit.**

```bash
git add apps/web-backend
git commit -m "feat: stream durable inspection alerts in realtime"
```

### Task 5: Migrate the frontend and prove the P0 runtime end to end

**Files:**
- Modify: `apps/web-frontend/src/api/types.ts`
- Modify: `apps/web-frontend/src/api/client.ts`
- Modify: `apps/web-frontend/src/features/workbench/useInspectionFeed.ts`
- Modify: `apps/web-frontend/src/features/workbench/RealtimeWorkbench.test.tsx`
- Modify: `apps/web-frontend/e2e/quality-workflow.spec.ts`
- Modify: `apps/web-frontend/playwright.config.ts`
- Modify: `deploy/compose.yaml`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Test: `apps/web-frontend/src/features/workbench/useInspectionFeed.test.tsx`

**Interfaces:**
- Produces `createWebSocketTicket() -> Promise<{ticket: string; expires_in: 60}>`.
- `useInspectionFeed()` stores `odp_alert_cursor` per authenticated actor session, reconciles through `after_cursor`, and connects with `?ticket=<one-time>&cursor=<last-cursor>`.
- Reconnect delay uses bounded exponential backoff of 1, 2, 4, 8, 16 and 30 seconds and resets after a stable connection.

- [ ] **Step 1: Write failing frontend cursor/ticket tests.**

```tsx
it("uses a one-time ticket and advances the reconciliation cursor", async () => {
  mockFetchTicket("ticket-1");
  mockReconciliation({items: [envelope("12-0")], next_cursor: "12-0"});
  renderHook(() => useInspectionFeed());
  await waitFor(() => expect(FakeWebSocket.url).toContain("ticket=ticket-1"));
  expect(FakeWebSocket.url).not.toContain(localStorage.getItem("odp_token")!);
  expect(localStorage.getItem("odp_alert_cursor")).toBe("12-0");
});

it("reconnects from the last cursor instead of the epoch", async () => {
  localStorage.setItem("odp_alert_cursor", "44-0");
  renderHook(() => useInspectionFeed());
  expect(reconciliationUrl()).toContain("after_cursor=44-0");
  expect(reconciliationUrl()).not.toContain("1970");
});
```

- [ ] **Step 2: Run frontend tests and verify RED.**

Run: `npm test -- --run src/features/workbench/useInspectionFeed.test.tsx src/features/workbench/RealtimeWorkbench.test.tsx`

Expected: the client still places the JWT in the WebSocket URL and always reconciles from the supplied epoch timestamp.

- [ ] **Step 3: Implement ticket/cursor client and backoff.**

Fetch a fresh ticket before each connection, persist the cursor only after a valid envelope is appended, deduplicate by `event_id`, and stop reconnecting after logout/unmount. A failed ticket request follows the same bounded backoff without opening a socket.

- [ ] **Step 4: Make Compose reproducible.**

Run Alembic before seed/start, set `ODP_DATABASE_URL=postgresql+psycopg://odp:odp@postgres:5432/odp`, install backend dependencies from its package metadata, and ensure the WebSocket runtime dependency is present. Keep PostgreSQL/Redis health dependencies and the nginx same-origin entry.

- [ ] **Step 5: Extend Playwright and CI.**

The E2E test logs in, observes an alert, causes a new alert to be published after the socket is open through a deterministic test/demo endpoint guarded to non-production environments, verifies it arrives without page reload, transitions a case, restarts the API container, and verifies status/history remain. CI runs Alembic, backend tests, frontend tests/build, Ruff for newly touched backend/frontend integration files, Compose E2E and teardown.

- [ ] **Step 6: Run the complete verification set.**

Run:

```bash
../../.venv-runtime/bin/pytest apps/platform/tests -q
../../.venv-runtime/bin/pytest apps/web-backend/tests -q
cd apps/web-frontend && npm test -- --run && npm run build && npm run typecheck:e2e
docker compose -f deploy/compose.yaml config --quiet
```

When Docker is available, additionally run `docker compose -f deploy/compose.yaml up -d --build`, wait for `/healthz`, run `E2E_BASE_URL=http://localhost:8080 npm run test:e2e`, restart `api`, rerun the persistence assertion, then tear down.

- [ ] **Step 7: Update documentation with exact local and Docker commands.**

Document the SQLite database path, Alembic commands, WebSocket ticket flow, cursor semantics, development-only live-event trigger, Docker persistence behavior and recovery steps. Remove statements that claim in-memory actors/cases/audits are the runtime source of truth.

- [ ] **Step 8: Commit.**

```bash
git add apps/web-frontend deploy .github/workflows/ci.yml README.md apps/web-backend
git commit -m "feat: complete durable realtime P0 workflow"
```

## Plan self-review

- [x] Spec coverage maps as follows: persistence/migrations/seed (Tasks 1–2), identity/JWT/password/tickets (Task 3), durable live feed (Task 4), frontend cursor/reconnect and delivery evidence (Task 5).
- [x] PostgreSQL and SQLite behavior share repository contracts; Docker-only Redis concerns remain behind adapters.
- [x] All new behavior has an explicit RED command before production implementation.
- [x] RAG, model inference semantics and non-P0 admin scope remain unchanged.
- [x] Each task produces a reviewable commit and can be rejected independently of later tasks.
