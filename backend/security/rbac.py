"""Role-based access control for the REST API.

Three roles, strictly nested. The interesting line is between ``viewer`` and
``reviewer``: only a reviewer may turn an agent's proposal into a public
GitHub comment.
"""

from __future__ import annotations

from backend.models.enums import Role

PERMISSIONS: dict[str, set[Role]] = {
    "reviews:read": {Role.VIEWER, Role.REVIEWER, Role.ADMIN},
    "economics:read": {Role.VIEWER, Role.REVIEWER, Role.ADMIN},
    "queue:read": {Role.VIEWER, Role.REVIEWER, Role.ADMIN},
    "hitl:read": {Role.VIEWER, Role.REVIEWER, Role.ADMIN},
    "hitl:decide": {Role.REVIEWER, Role.ADMIN},
    "feedback:write": {Role.REVIEWER, Role.ADMIN},
    "reviews:rerun": {Role.ADMIN},
    "index:write": {Role.ADMIN},
}


def role_allows(role: str, permission: str) -> bool:
    try:
        return Role(role) in PERMISSIONS.get(permission, set())
    except ValueError:
        return False
