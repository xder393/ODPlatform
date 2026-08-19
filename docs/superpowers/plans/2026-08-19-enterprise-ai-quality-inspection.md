# Enterprise AI Quality Inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Docker-runnable manufacturing quality-inspection platform with real-time simulated vision alerts, human case handling, enterprise access control and evidence-backed RAG guidance.

**Architecture:** Add a React workbench and a FastAPI modular monolith. The backend owns business modules and depends only on ports; adapters connect it to the existing `odp_platform`, PostgreSQL/pgvector, Redis Streams, object storage and model providers. Build vertical slices in dependency order so each milestone remains demonstrable.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic 2, PostgreSQL 16 + pgvector, Redis 7, React 18 + TypeScript + Vite, Docker Compose, pytest, Playwright, Prometheus, Grafana, Alertmanager, OpenTelemetry.

**Spec:** `docs/superpowers/specs/2026-08-19-enterprise-ai-quality-inspection-design.md`

## Global Constraints

- Preserve `apps/platform` as the visual-engine core; Web business modules must never import storage or model providers directly.
- Every persisted business resource contains `organization_id`; repositories enforce tenant scope before returning data.
- High-risk pause is simulated only and requires a password or TOTP reauthentication completed within five minutes.
- WebSocket delivery is at-most-once; reconnecting clients fetch current unseen events and open cases by timestamp.
- AI replies always include source snippets and `HIGH`, `MEDIUM`, or `LOW` confidence; `LOW` cannot independently recommend high-risk action.
- Async work is idempotent, stateful, bounded by retries/timeouts, and uses `DEAD_LETTER` for exhausted failures.
- `InspectionEvent` stores model release, preprocessing parameters, threshold and input-frame SHA-256.
- Audit entries are append-only, hash chained per organization and checked at startup and daily.
- Docker Compose must seed three cameras, ten defect samples, two knowledge documents and demo accounts.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `apps/web-backend/src/odp_api/main.py` | FastAPI factory, routers, lifespan and health endpoints. |
| `apps/web-backend/src/odp_api/modules/*` | Focused domain modules: identity, organization, inspection, cases, audit, knowledge, models and notifications. |
| `apps/web-backend/src/odp_api/ports/*.py` | Infrastructure-independent protocols for vision, retrieval, generation, storage, notifications and tasks. |
| `apps/web-backend/src/odp_api/adapters/*` | SQLAlchemy, Redis, pgvector, `odp_platform`, mock LLM and local object-storage adapters. |
| `apps/web-backend/tests/*` | Unit, integration and deterministic end-to-end backend coverage. |
| `packages/shared-schemas/src/odp_schemas/*` | Versioned Pydantic API/event contracts and generated TypeScript output. |
| `apps/web-frontend/src/*` | React workbench, real-time client, case timeline, admin views and API client. |
| `deploy/compose.yaml` | Local platform, dependencies, seed and observability services. |
| `deploy/observability/*` | Prometheus scrape/alert rules and Grafana dashboard provisioning. |

## Milestone 1 — reproducible vertical slice

### Task 1: Create the backend, shared-schema and local-runtime foundations

**Files:**
- Create: `apps/web-backend/pyproject.toml`
- Create: `apps/web-backend/src/odp_api/main.py`
- Create: `apps/web-backend/src/odp_api/settings.py`
- Create: `apps/web-backend/tests/test_health.py`
- Create: `packages/shared-schemas/pyproject.toml`
- Create: `packages/shared-schemas/src/odp_schemas/events.py`
- Create: `deploy/compose.yaml`
- Modify: `apps/web-backend/README.md`

**Interfaces:**
- Produces `create_app() -> FastAPI` and `GET /healthz -> {"status":"ok"}`.
- Produces `InspectionAlert` with `event_id: UUID`, `organization_id: UUID`, `camera_id: UUID`, `occurred_at: datetime`, `defect_class: str`, `confidence: float`.

- [ ] **Step 1: Write failing health and schema tests.**

```python
def test_healthz_returns_ok(client):
    assert client.get("/healthz").json() == {"status": "ok"}

def test_alert_requires_confidence_between_zero_and_one():
    with pytest.raises(ValidationError):
        InspectionAlert(confidence=1.1, **valid_alert)
```

- [ ] **Step 2: Run `pytest apps/web-backend/tests/test_health.py -v`; verify import and route failures.**
- [ ] **Step 3: Implement `Settings`, `create_app`, the health router and bounded Pydantic `InspectionAlert`.**
- [ ] **Step 4: Add Compose services named `api`, `postgres`, `redis`, `minio`, `prometheus`, `grafana` and `alertmanager`, with health checks for PostgreSQL and Redis.**
- [ ] **Step 5: Run `docker compose -f deploy/compose.yaml config` and `pytest apps/web-backend/tests/test_health.py -v`; verify both pass.**
- [ ] **Step 6: Commit `feat: scaffold web quality inspection runtime`.**

### Task 2: Model the inspection event and case state machine

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/inspection/models.py`
- Create: `apps/web-backend/src/odp_api/modules/cases/service.py`
- Create: `apps/web-backend/src/odp_api/modules/cases/errors.py`
- Create: `apps/web-backend/tests/modules/cases/test_service.py`

**Interfaces:**
- Consumes `InspectionAlert` from Task 1.
- Produces `CaseStatus = Literal["PENDING_CONFIRMATION", "IN_REVIEW", "RESOLVED", "FALSE_POSITIVE"]`.
- Produces `CaseService.transition(case: DefectCase, to_status: CaseStatus, actor_id: UUID) -> DefectCase`.

- [ ] **Step 1: Write failing transition tests for `PENDING_CONFIRMATION → IN_REVIEW`, `IN_REVIEW → RESOLVED`, and rejection of `RESOLVED → IN_REVIEW`.**
- [ ] **Step 2: Run `pytest apps/web-backend/tests/modules/cases/test_service.py -v`; verify state-service import failure.**
- [ ] **Step 3: Implement immutable `InspectionEvent`, `DefectCase`, `InvalidCaseTransition`, and a transition map that raises on terminal-state reopening.**
- [ ] **Step 4: Run the case test file and `ruff check apps/web-backend/src`; verify pass.**
- [ ] **Step 5: Commit `feat: add inspection case state machine`.**

### Task 3: Deliver deterministic simulated vision alerts and REST case handling

**Files:**
- Create: `apps/web-backend/src/odp_api/ports/vision.py`
- Create: `apps/web-backend/src/odp_api/adapters/vision/mock.py`
- Create: `apps/web-backend/src/odp_api/modules/inspection/service.py`
- Create: `apps/web-backend/src/odp_api/modules/cases/router.py`
- Create: `apps/web-backend/tests/integration/test_cases_api.py`

**Interfaces:**
- Produces `VisionInferencePort.inspect(frame: FrameInput) -> VisionResult`.
- Produces `POST /api/v1/cases/{case_id}/transitions` accepting `{ "status": "IN_REVIEW" }`.
- Produces `GET /api/v1/cases?updated_after=<ISO8601>` returning ordered case summaries.

- [ ] **Step 1: Write failing API test that feeds fixture `scratch-frame-001`, receives one `PENDING_CONFIRMATION` case, then transitions it to `IN_REVIEW`.**
- [ ] **Step 2: Run `pytest apps/web-backend/tests/integration/test_cases_api.py -v`; verify it fails before routing exists.**
- [ ] **Step 3: Implement a protocol-based mock vision adapter returning a fixed scratch detection with frame SHA-256, model release `mock-yolo-1.0`, threshold `0.80`, and confidence `0.964`.**
- [ ] **Step 4: Implement in-memory repository fixture and case router with Pydantic request/response models.**
- [ ] **Step 5: Run the integration test; verify the final transition and input metadata assertions pass.**
- [ ] **Step 6: Commit `feat: add simulated inspection case workflow`.**

### Task 4: Stream alerts to a React workbench and reconcile on reconnect

**Files:**
- Create: `apps/web-frontend/package.json`
- Create: `apps/web-frontend/src/main.tsx`
- Create: `apps/web-frontend/src/features/workbench/RealtimeWorkbench.tsx`
- Create: `apps/web-frontend/src/features/workbench/useInspectionFeed.ts`
- Create: `apps/web-backend/src/odp_api/modules/notifications/router.py`
- Create: `apps/web-backend/tests/integration/test_notification_api.py`
- Create: `apps/web-frontend/src/features/workbench/RealtimeWorkbench.test.tsx`

**Interfaces:**
- Produces `GET /api/v1/inspection-events?updated_after=<ISO8601>` and `WS /ws/inspection-events`.
- Produces `useInspectionFeed(since: string): { alerts: InspectionAlert[]; reconnecting: boolean }`.

- [ ] **Step 1: Write backend test asserting a reconnect fetch returns the currently unseen event and WebSocket payload matches `InspectionAlert`.**
- [ ] **Step 2: Write frontend test asserting the workbench renders “疑似表面划痕”, “96.4%”, and reconnects by calling the REST reconciliation endpoint.**
- [ ] **Step 3: Run `pytest apps/web-backend/tests/integration/test_notification_api.py -v` and `npm test -- --run RealtimeWorkbench.test.tsx`; verify expected failures.**
- [ ] **Step 4: Implement an at-most-once WebSocket endpoint and the React hook: on `onopen`, fetch unseen events, then append future socket messages; do not implement client ACK.**
- [ ] **Step 5: Implement `RealtimeWorkbench` with video placeholder, alert details, case actions and an accessible live-region alert.**
- [ ] **Step 6: Run both test commands and `npm run build --prefix apps/web-frontend`; verify pass.**
- [ ] **Step 7: Commit `feat: add realtime inspection workbench`.**

## Milestone 2 — enterprise boundaries and reliability

### Task 5: Add organization scope, RBAC policies and reauthentication

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/identity/models.py`
- Create: `apps/web-backend/src/odp_api/modules/identity/policies.py`
- Create: `apps/web-backend/src/odp_api/modules/identity/service.py`
- Create: `apps/web-backend/src/odp_api/adapters/auth/jwt.py`
- Create: `apps/web-backend/tests/modules/identity/test_policies.py`
- Create: `apps/web-backend/tests/integration/test_tenant_isolation.py`

**Interfaces:**
- Produces `authorize(actor: Actor, permission: str, organization_id: UUID, line_id: UUID | None) -> None`.
- Produces `POST /api/v1/auth/reauthenticate` and `require_recent_reauth(actor_id: UUID, now: datetime) -> None`.

- [ ] **Step 1: Write failing tests showing `defect_case:update:own_line` permits an inspector on their line, denies another line, and scope filters prevent cross-organization reads.**
- [ ] **Step 2: Write failing test that rejects a pause action after five minutes and accepts it after password-based reauthentication.**
- [ ] **Step 3: Run identity and tenant tests; verify authorization is absent.**
- [ ] **Step 4: Implement role grants, resource-scope policies, tenant-aware repository base query, JWT subject extraction, and Redis `last_reauth_at` with a 300-second TTL.**
- [ ] **Step 5: Guard every case mutation and alert query using `authorize`; expose pause as a simulation command only.**
- [ ] **Step 6: Run the two test files and full backend suite; verify pass.**
- [ ] **Step 7: Commit `feat: enforce tenant scoped inspection permissions`.**

### Task 6: Add append-only, hash-chained audit logging

**Files:**
- Create: `apps/web-backend/src/odp_api/modules/audit/models.py`
- Create: `apps/web-backend/src/odp_api/modules/audit/service.py`
- Create: `apps/web-backend/src/odp_api/modules/audit/verify.py`
- Create: `apps/web-backend/tests/modules/audit/test_hash_chain.py`

**Interfaces:**
- Produces `AuditService.append(command: AuditCommand) -> AuditLog`.
- Produces `verify_organization_chain(organization_id: UUID) -> VerificationResult`.
- `AuditCommand` fields are `resource_type`, `resource_id`, `action`, `change_summary`, `actor_id`, `occurred_at`, `correlation_id`, `request_ip`.

- [ ] **Step 1: Write failing tests for deterministic SHA-256 input, two concurrent appends forming one continuous chain, and a tampered row producing failed verification.**
- [ ] **Step 2: Run `pytest apps/web-backend/tests/modules/audit/test_hash_chain.py -v`; verify failure.**
- [ ] **Step 3: Implement organization chain-head table and transaction that locks its row, inserts one append-only entry, then advances the head hash. Grant no application `UPDATE` or `DELETE` privilege for audit rows.**
- [ ] **Step 4: Implement startup sample verification and daily full verification task; make failure emit P0 metric/log and block new audit appends until explicit admin recovery.**
- [ ] **Step 5: Run audit tests and an Alembic migration upgrade against Compose PostgreSQL; verify pass.**
- [ ] **Step 6: Commit `feat: add verifiable audit hash chain`.**

### Task 7: Make asynchronous work idempotent and bounded

**Files:**
- Create: `apps/web-backend/src/odp_api/ports/tasks.py`
- Create: `apps/web-backend/src/odp_api/adapters/tasks/redis_stream.py`
- Create: `apps/web-backend/src/odp_api/modules/tasks/models.py`
- Create: `apps/web-backend/src/odp_api/modules/tasks/service.py`
- Create: `apps/web-backend/tests/modules/tasks/test_service.py`

**Interfaces:**
- Produces `TaskStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "RETRYING", "DEAD_LETTER"]`.
- Produces `enqueue(task_type: str, idempotency_key: str, payload: dict[str, object]) -> TaskRecord`.

- [ ] **Step 1: Write failing tests for duplicate `frame_id:model_version` enqueue, timeout retry with exponential delay, final `DEAD_LETTER`, and frame backlog marking stale work `SKIPPED`.**
- [ ] **Step 2: Run `pytest apps/web-backend/tests/modules/tasks/test_service.py -v`; verify failure.**
- [ ] **Step 3: Implement persisted task records, a Redis Stream adapter, retry limit of three attempts, 30-second vision timeout, and stale-frame cutoff of two seconds.**
- [ ] **Step 4: Emit `task_dead_letter_total` and queue-depth metrics; write alert event on final failure.**
- [ ] **Step 5: Run the task tests plus integration tests; verify pass.**
- [ ] **Step 6: Commit `feat: add resilient async task processing`.**

## Milestone 3 — evidence-backed AI assistant

### Task 8: Build versioned, tenant-isolated knowledge ingestion

**Files:**
- Create: `apps/web-backend/src/odp_api/ports/retrieval.py`
- Create: `apps/web-backend/src/odp_api/modules/knowledge/models.py`
- Create: `apps/web-backend/src/odp_api/modules/knowledge/ingest.py`
- Create: `apps/web-backend/src/odp_api/adapters/retrieval/pgvector.py`
- Create: `apps/web-backend/tests/modules/knowledge/test_ingest.py`

**Interfaces:**
- Produces `KnowledgeDocument(status: Literal["INDEXED", "SUPERSEDED", "FAILED"])`.
- Produces `RAGRetrievalPort.search(query: str, organization_id: UUID, filters: RetrievalFilters) -> list[RetrievedChunk]`.

- [ ] **Step 1: Write failing tests that ingest a two-page rule document, retain page and paragraph provenance, supersede a prior version, and reject a cross-tenant search result.**
- [ ] **Step 2: Run `pytest apps/web-backend/tests/modules/knowledge/test_ingest.py -v`; verify failure.**
- [ ] **Step 3: Implement validated PDF/DOCX ingestion, parent-child chunking with overlap, `organization_id` indexed on each pgvector row, and mandatory repository filter.**
- [ ] **Step 4: Add a lexical BM25 score and vector score to the same retrieval adapter, normalizing and combining them before returning ordered chunks.**
- [ ] **Step 5: Run tests and a PostgreSQL migration; verify provenance and isolation assertions pass.**
- [ ] **Step 6: Commit `feat: add versioned tenant scoped knowledge ingestion`.**

### Task 9: Add deterministic RAG advice with citations and confidence tiers

**Files:**
- Create: `apps/web-backend/src/odp_api/ports/generation.py`
- Create: `apps/web-backend/src/odp_api/adapters/generation/mock.py`
- Create: `apps/web-backend/src/odp_api/modules/ai_orchestration/service.py`
- Create: `apps/web-backend/src/odp_api/modules/ai_orchestration/router.py`
- Create: `apps/web-backend/tests/modules/ai_orchestration/test_service.py`
- Create: `apps/web-backend/tests/golden/rag_advice.json`

**Interfaces:**
- Produces `AdviceResponse(answer: str, citations: list[Citation], confidence: Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE"])`.
- Produces `POST /api/v1/cases/{case_id}/advice`.

- [ ] **Step 1: Write failing Golden Dataset tests for a direct specification match (`HIGH`), same-line historical case (`MEDIUM`), cross-product reference (`LOW`), and unavailable retrieval (`UNAVAILABLE`).**
- [ ] **Step 2: Run `pytest apps/web-backend/tests/modules/ai_orchestration/test_service.py -v`; verify failure.**
- [ ] **Step 3: Implement `MockLLMAdapter` as a pure template renderer over retrieved chunks, not a random generator. Return source document, version, page/paragraph and quoted snippet in each citation.**
- [ ] **Step 4: Enforce that `LOW` advice contains no pause recommendation and `UNAVAILABLE` returns the exact human-review message.**
- [ ] **Step 5: Run unit and Golden Dataset tests; verify deterministic output.**
- [ ] **Step 6: Commit `feat: add cited quality inspection advice`.**

### Task 10: Render citations and case history in the workbench

**Files:**
- Create: `apps/web-frontend/src/features/advice/AdvicePanel.tsx`
- Create: `apps/web-frontend/src/features/cases/CaseTimeline.tsx`
- Create: `apps/web-frontend/src/features/advice/AdvicePanel.test.tsx`
- Modify: `apps/web-frontend/src/features/workbench/RealtimeWorkbench.tsx`

**Interfaces:**
- Consumes `AdviceResponse` from Task 9.
- Produces `AdvicePanel({ advice }: { advice: AdviceResponse }): JSX.Element`.

- [ ] **Step 1: Write tests asserting HIGH, MEDIUM and LOW labels have accessible text and green, yellow and gray semantic classes; assert every citation shows document version and page/paragraph.**
- [ ] **Step 2: Run `npm test -- --run AdvicePanel.test.tsx`; verify failure.**
- [ ] **Step 3: Implement the advice panel and chronological case timeline containing detection, advice and human state changes. Disable the pause action for LOW and UNAVAILABLE advice.**
- [ ] **Step 4: Run frontend tests and production build; verify pass.**
- [ ] **Step 5: Commit `feat: display cited inspection advice`.**

## Milestone 4 — operations, admin and delivery evidence

### Task 11: Instrument the platform and provide alertable dashboards

**Files:**
- Create: `apps/web-backend/src/odp_api/observability/metrics.py`
- Create: `apps/web-backend/src/odp_api/observability/tracing.py`
- Create: `deploy/observability/prometheus.yml`
- Create: `deploy/observability/alerts.yml`
- Create: `deploy/observability/grafana/provisioning/dashboards/dashboard.yml`
- Create: `deploy/observability/grafana/dashboards/quality-inspection.json`
- Create: `apps/web-backend/tests/test_metrics.py`

**Interfaces:**
- Produces `GET /metrics` in Prometheus exposition format.
- Produces counters `inspection_alert_total`, `task_dead_letter_total`, `audit_chain_verification_failure_total` and histograms `vision_inference_seconds`, `case_resolution_seconds`.

- [ ] **Step 1: Write failing test that a simulated event increments `inspection_alert_total` and that `/metrics` exposes `task_dead_letter_total`.**
- [ ] **Step 2: Run `pytest apps/web-backend/tests/test_metrics.py -v`; verify failure.**
- [ ] **Step 3: Instrument HTTP, WebSocket, Redis task and database spans with correlation ID; register the stated metrics.**
- [ ] **Step 4: Configure Prometheus scrape, Grafana dashboard panels for frame rate, queue depth, case closure, RAG hit and WebSocket health, plus Alertmanager rules for queue depth > 1,000, dead letters > 0 and audit verification failure > 0.**
- [ ] **Step 5: Run the test, `docker compose -f deploy/compose.yaml config`, and a Compose smoke startup; verify all observability services become healthy.**
- [ ] **Step 6: Commit `feat: add inspection observability stack`.**

### Task 12: Seed, test, document and enforce the deliverable

**Files:**
- Create: `apps/web-backend/src/odp_api/seed.py`
- Create: `apps/web-backend/tests/e2e/test_quality_workflow.py`
- Create: `apps/web-frontend/e2e/quality-workflow.spec.ts`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `apps/web-backend/README.md`
- Modify: `apps/web-frontend/README.md`

**Interfaces:**
- Produces `python -m odp_api.seed` with accounts `inspector@example.test`, `leader@example.test`, `admin@example.test` and documented development-only passwords.
- Produces a deterministic E2E path from simulated frame to closed case with cited advice and audit verification.

- [ ] **Step 1: Write failing backend E2E test that seeds data, ingests the two documents, emits a defect, logs in as inspector, resolves the case, and verifies the audit chain.**
- [ ] **Step 2: Write failing Playwright test that sees the alert, opens cited advice, completes the confirmation action and sees the case timeline update.**
- [ ] **Step 3: Run `pytest apps/web-backend/tests/e2e/test_quality_workflow.py -v` and `npx playwright test apps/web-frontend/e2e/quality-workflow.spec.ts`; verify failures before seed/runtime completion.**
- [ ] **Step 4: Implement idempotent seed fixtures for three cameras, ten samples, two documents, user/role assignments and one model release.**
- [ ] **Step 5: Update CI to run backend tests, frontend unit tests, TypeScript build, Compose migration/seed smoke test and deterministic E2E tests. Document local startup, architecture boundaries, demo script and performance baseline.**
- [ ] **Step 6: Run the complete CI-equivalent command set locally; capture the passing output in the delivery notes.**
- [ ] **Step 7: Commit `feat: deliver reproducible enterprise quality inspection demo`.**

## Plan self-review

- [x] Spec coverage maps to Tasks 1–12: ports/adapters (1, 3, 8, 9); inspection workflow (2–4); RBAC/audit/tasks (5–7); RAG (8–10); observability and delivery (11–12).
- [x] The plan deliberately excludes real equipment actuation, automatic model failover, A/B model experiments, Pact and Chaos Mesh, matching the approved first-version scope.
- [x] Every introduced interface has a defining task before a consuming task, and each task includes a failing test, implementation, verification and commit step.
