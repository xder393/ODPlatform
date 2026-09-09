from uuid import UUID

from odp_api.modules.identity.models import Actor, Role


class AuthorizationDenied(PermissionError):
    """Raised when an actor lacks an organization, role, or resource grant."""


ROLE_GRANTS: dict[Role, frozenset[str]] = {
    Role.INSPECTOR: frozenset(
        {
            "defect_case:read:own_line",
            "defect_case:update:own_line",
            "defect_case:pause:own_line",
            "inspection_event:read:own_line",
            "inspection_session:read:own_line",
            "inference_task:read:own_line",
            "artifact:evidence:read:own_line",
        }
    ),
    Role.SUPERVISOR: frozenset(
        {
            "defect_case:read:own_line",
            "defect_case:update:own_line",
            "defect_case:pause:own_line",
            "inspection_event:read:own_line",
            "inspection_session:read:own_line",
            "inspection_session:start",
            "inspection_session:stop",
            "inference_task:read:own_line",
            "inference_task:replay",
            "artifact:evidence:read:own_line",
        }
    ),
    Role.ADMINISTRATOR: frozenset({"*"}),
}


def authorize(
    actor: Actor,
    permission: str,
    organization_id: UUID,
    line_id: UUID | None,
) -> None:
    """Enforce grants and resource scope before an organization resource is used."""
    if actor.organization_id != organization_id:
        raise AuthorizationDenied("The resource belongs to a different organization.")

    role = Role(actor.role)
    grants = ROLE_GRANTS[role]
    if permission not in grants and "*" not in grants:
        raise AuthorizationDenied(f"Role {role.value} lacks {permission}.")

    if role is not Role.ADMINISTRATOR and (line_id is None or line_id not in actor.line_ids):
        raise AuthorizationDenied("The resource is outside the actor's production-line scope.")
