"""Deterministic offline end-to-end quality inspection workflow.

Seeds the demo dataset, then drives the complete inspector loop against the
real application: login, case listing, evidence-backed advice, case
transitions and audit-chain verification. No network, database or model
provider is involved; every adapter is the in-memory deterministic one.
"""

from uuid import UUID

from fastapi.testclient import TestClient

from odp_api.adapters.auth.jwt import InMemoryActorRepository, issue_token
from odp_api.main import create_app
from odp_api.modules.identity.models import Actor, Role
from odp_api.seed import DEMO_ACCOUNTS, DEMO_ORG_ID, build_demo_seed
from odp_api.settings import Settings

INSPECTOR_EMAIL = DEMO_ACCOUNTS[0][0]
INSPECTOR_PASSWORD = DEMO_ACCOUNTS[0][1]
E2E_SECRET = "e2e-demo-secret"


def _demo_app(tmp_path, **overrides: object) -> "object":
    return create_app(
        settings=Settings(
            auth_jwt_secret=E2E_SECRET,
            database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
            task_database_path=str(tmp_path / "tasks.db"),
        ),
        seed=build_demo_seed(),
        **overrides,
    )


def _login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": INSPECTOR_EMAIL, "password": INSPECTOR_PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_seeded_inspector_resolves_a_case_with_cited_advice_and_audit_trail(
    tmp_path,
) -> None:
    app = _demo_app(tmp_path)
    client = TestClient(app)
    headers = _login(client)

    # Step 1: the seeded inspector sees ten cases on their line.
    cases_response = client.get("/api/v1/cases", headers=headers)
    assert cases_response.status_code == 200
    cases = cases_response.json()
    assert len(cases) == 10
    case = cases[0]
    assert case["status"] == "PENDING_CONFIRMATION"
    assert case["inspection_events"][0]["defect_class"] == "scratch"
    assert case["inspection_events"][0]["model_release"] == "mock-yolo-1.0"

    # Step 2: advice is HIGH confidence with source-addressable citations.
    advice_response = client.post(
        f"/api/v1/cases/{case['case_id']}/advice", headers=headers
    )
    assert advice_response.status_code == 200
    advice = advice_response.json()
    assert advice["confidence"] == "HIGH"
    assert advice["citations"]
    for citation in advice["citations"]:
        assert citation["source_name"]
        assert citation["document_version"] >= 1
        assert citation["page_number"] >= 1
        assert citation["paragraph_number"] >= 1
        assert citation["snippet"]
    assert "specification" in advice["answer"].lower()

    # Step 3: the inspector confirms, reviews and resolves the case.
    for target_status in ("IN_REVIEW", "RESOLVED"):
        transition_response = client.post(
            f"/api/v1/cases/{case['case_id']}/transitions",
            json={"status": target_status},
            headers=headers,
        )
        assert transition_response.status_code == 200
        assert transition_response.json()["status"] == target_status

    # A resolved case is terminal: reopening must be rejected.
    reopen_response = client.post(
        f"/api/v1/cases/{case['case_id']}/transitions",
        json={"status": "IN_REVIEW"},
        headers=headers,
    )
    assert reopen_response.status_code == 409

    # Step 4: every mutation is covered by a continuous audit chain.
    verification = app.state.audit_service.verify_organization_chain(DEMO_ORG_ID)
    assert verification.is_valid is True
    assert verification.checked_entries >= 2  # IN_REVIEW + RESOLVED transitions

    # Step 5: metrics reflect the simulated inspection and resolution work.
    metrics_response = client.get("/metrics")
    assert metrics_response.status_code == 200
    metrics_text = metrics_response.text
    assert "inspection_alert_total" in metrics_text
    assert "case_resolution_seconds" in metrics_text


def test_advice_is_tenant_scoped_across_organizations(tmp_path) -> None:
    """An actor from another organization cannot see this organization's cases."""
    seed = build_demo_seed()
    outsider = Actor(
        actor_id=UUID("99990000-0000-4000-8000-000000000001"),
        organization_id=UUID("99990000-0000-4000-8000-000000000002"),
        role=Role.INSPECTOR,
        line_ids=frozenset({UUID("99990000-0000-4000-8000-000000000003")}),
        email="outsider@example.test",
    )
    app = create_app(
        settings=Settings(
            auth_jwt_secret=E2E_SECRET,
            database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
            task_database_path=str(tmp_path / "tasks.db"),
        ),
        seed=seed,
        actor_repository=InMemoryActorRepository(
            {
                **{actor.actor_id: actor for actor in seed.actors},
                outsider.actor_id: outsider,
            }
        ),
    )
    client = TestClient(app)
    headers = _login(client)
    case_id = client.get("/api/v1/cases", headers=headers).json()[0]["case_id"]

    outsider_headers = {
        "Authorization": f"Bearer {issue_token(outsider.actor_id, E2E_SECRET)}"
    }
    # The case list is filtered to the outsider's (empty) organization scope.
    outsider_cases = client.get("/api/v1/cases", headers=outsider_headers)
    assert outsider_cases.status_code == 200
    assert outsider_cases.json() == []
    # Direct access by id is indistinguishable from a missing case (404).
    advice_response = client.post(
        f"/api/v1/cases/{case_id}/advice", headers=outsider_headers
    )
    assert advice_response.status_code == 404
