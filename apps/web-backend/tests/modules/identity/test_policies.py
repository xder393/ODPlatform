import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
SHARED_SCHEMAS_SRC = Path(__file__).parents[5] / "packages" / "shared-schemas" / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC), str(SHARED_SCHEMAS_SRC)]

from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.identity.policies import AuthorizationDenied, authorize
from odp_api.modules.identity.service import (
    InMemoryReauthenticationStore,
    ReauthenticationService,
    RecentReauthenticationRequired,
)


def test_inspector_can_update_cases_on_an_authorized_line_only() -> None:
    organization_id = uuid4()
    authorized_line_id = uuid4()
    another_line_id = uuid4()
    inspector = Actor(
        actor_id=uuid4(),
        organization_id=organization_id,
        role=Role.INSPECTOR,
        line_ids=frozenset({authorized_line_id}),
    )

    authorize(inspector, "defect_case:update:own_line", organization_id, authorized_line_id)

    with pytest.raises(AuthorizationDenied):
        authorize(inspector, "defect_case:update:own_line", organization_id, another_line_id)


def test_actor_cannot_read_a_different_organization() -> None:
    actor = Actor(
        actor_id=uuid4(),
        organization_id=uuid4(),
        role=Role.ADMINISTRATOR,
        line_ids=frozenset(),
    )

    with pytest.raises(AuthorizationDenied):
        authorize(actor, "defect_case:read:own_line", uuid4(), None)


def test_recent_reauthentication_expires_after_five_minutes() -> None:
    actor_id = uuid4()
    now = datetime(2026, 8, 20, tzinfo=UTC)
    service = ReauthenticationService(InMemoryReauthenticationStore())

    with pytest.raises(RecentReauthenticationRequired):
        service.require_recent_reauth(actor_id, now)

    service.record_success(actor_id, now)
    service.require_recent_reauth(actor_id, now + timedelta(minutes=5))

    with pytest.raises(RecentReauthenticationRequired):
        service.require_recent_reauth(actor_id, now + timedelta(minutes=5, seconds=1))
