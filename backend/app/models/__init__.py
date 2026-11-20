"""ORM models. Every model is imported here so Alembic autogenerate can see it."""

from app.models.base import Base, TenantMixin
from app.models.execution import (
    Approval,
    LocatorHealProposal,
    TestEvidence,
    TestExecution,
    TestExecutionStep,
    TestFinding,
    TestReport,
    TestResult,
)
from app.models.identity import (
    ApiKey,
    Organization,
    OrganizationMember,
    PasswordResetToken,
    RefreshToken,
    User,
)
from app.models.operations import (
    AgentExecutionLog,
    AuditLog,
    GithubDelivery,
    GithubRepository,
    Integration,
    McpConnection,
    Notification,
)
from app.models.project import (
    Application,
    Credential,
    Environment,
    EnvironmentAllowedOrigin,
    Project,
    ProjectMember,
)
from app.models.testing import (
    TestCase,
    TestCaseVersion,
    TestDataSet,
    TestFile,
    TestInstruction,
    TestPlan,
    TestStep,
    TestSuite,
    TestSuiteCase,
)
from app.models.workflow import WorkflowCheckpoint, WorkflowCheckpointWrite, WorkflowRun

__all__ = [
    "AgentExecutionLog",
    "ApiKey",
    "Application",
    "Approval",
    "AuditLog",
    "Base",
    "Credential",
    "Environment",
    "EnvironmentAllowedOrigin",
    "GithubDelivery",
    "GithubRepository",
    "Integration",
    "LocatorHealProposal",
    "McpConnection",
    "Notification",
    "Organization",
    "OrganizationMember",
    "PasswordResetToken",
    "Project",
    "ProjectMember",
    "RefreshToken",
    "TenantMixin",
    "TestCase",
    "TestCaseVersion",
    "TestDataSet",
    "TestEvidence",
    "TestExecution",
    "TestExecutionStep",
    "TestFile",
    "TestFinding",
    "TestInstruction",
    "TestPlan",
    "TestReport",
    "TestResult",
    "TestStep",
    "TestSuite",
    "TestSuiteCase",
    "User",
    "WorkflowCheckpoint",
    "WorkflowCheckpointWrite",
    "WorkflowRun",
]


def tenant_table_names() -> list[str]:
    """Tables that carry `organization_id` and are protected by row-level security."""
    return sorted(
        mapper.local_table.name  # type: ignore[attr-defined]
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantMixin)
    )
