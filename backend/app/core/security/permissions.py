"""Roles and permissions.

Authorization decisions are made from permissions, never from role names, so
that a role's abilities are defined in exactly one place: this module.
"""

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    QA_ENGINEER = "qa_engineer"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class Permission(StrEnum):
    ORG_READ = "org:read"
    ORG_UPDATE = "org:update"
    ORG_DELETE = "org:delete"
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    AUDIT_READ = "audit:read"
    # Projects. Access to a given project also needs project membership, except for
    # roles holding PROJECTS_ACCESS_ALL.
    PROJECTS_READ = "projects:read"
    PROJECTS_CREATE = "projects:create"
    PROJECTS_ACCESS_ALL = "projects:access_all"
    PROJECTS_DELETE = "projects:delete"
    APPLICATIONS_MANAGE = "applications:manage"
    ENVIRONMENTS_MANAGE = "environments:manage"
    # Production environments and the switches that widen what the agent can reach.
    ENVIRONMENTS_SECURITY = "environments:security"
    CREDENTIALS_READ = "credentials:read"
    CREDENTIALS_MANAGE = "credentials:manage"
    INSTRUCTIONS_CREATE = "instructions:create"
    # Deciding that an analysed requirement is correct and may be turned into tests.
    INSTRUCTIONS_APPROVE = "instructions:approve"
    # Generating, writing, editing and organising test cases and suites.
    CASES_MANAGE = "cases:manage"
    # Deciding that a case or a suite is correct and may be executed.
    CASES_APPROVE = "cases:approve"
    # Starting a browser against an environment (connection and login checks).
    BROWSER_RUN = "browser:run"
    # Running approved test cases and suites against an environment.
    EXECUTIONS_RUN = "executions:run"
    # API keys for CI, connections to GitHub, and what linked repositories may trigger.
    INTEGRATIONS_MANAGE = "integrations:manage"


# Seniority, used only for "may this actor assign or modify that role".
_RANK: dict[Role, int] = {
    Role.VIEWER: 10,
    Role.DEVELOPER: 20,
    Role.QA_ENGINEER: 30,
    Role.ADMIN: 40,
    Role.OWNER: 50,
}

_VIEWER = frozenset({Permission.ORG_READ, Permission.MEMBERS_READ, Permission.PROJECTS_READ})
# People who register and configure the applications they test.
_CONTRIBUTOR = _VIEWER | {
    Permission.PROJECTS_CREATE,
    Permission.APPLICATIONS_MANAGE,
    Permission.ENVIRONMENTS_MANAGE,
    Permission.CREDENTIALS_READ,
    Permission.CREDENTIALS_MANAGE,
    Permission.INSTRUCTIONS_CREATE,
    Permission.CASES_MANAGE,
    Permission.BROWSER_RUN,
    Permission.EXECUTIONS_RUN,
}
_QA = _CONTRIBUTOR | {Permission.INSTRUCTIONS_APPROVE, Permission.CASES_APPROVE}
_ADMIN = _QA | {
    Permission.ORG_UPDATE,
    Permission.MEMBERS_MANAGE,
    Permission.AUDIT_READ,
    Permission.PROJECTS_ACCESS_ALL,
    Permission.PROJECTS_DELETE,
    Permission.ENVIRONMENTS_SECURITY,
    Permission.INTEGRATIONS_MANAGE,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    # Developers write instructions; QA engineers also approve what will be tested.
    Role.DEVELOPER: _CONTRIBUTOR,
    Role.QA_ENGINEER: _QA,
    Role.ADMIN: _ADMIN,
    Role.OWNER: _ADMIN | {Permission.ORG_DELETE},
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def rank(role: Role) -> int:
    return _RANK[role]


def can_manage_role(actor: Role, target: Role) -> bool:
    """An actor may assign, change or remove a role only if it is not above their own."""
    return rank(actor) >= rank(target)
