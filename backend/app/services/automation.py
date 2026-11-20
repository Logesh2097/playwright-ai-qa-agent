"""What a script is checked against, gathered from the project and the environment."""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.login import LoginRecipe
from app.core.security.url_safety import Origin
from app.models import Credential, Environment, TestDataSet
from app.repositories.projects import CredentialRepository, EnvironmentRepository
from app.repositories.test_data import DataSetRepository, TestFileRepository
from app.testing.automation import Script
from app.testing.automation_checks import ScriptFacts, check_script
from app.testing.case_validation import CaseIssue
from app.testing.cases import CaseContent


@dataclass(frozen=True, slots=True)
class EnvironmentFacts:
    """Everything about one environment that scripts are checked and run against."""

    environment: Environment
    origins: frozenset[Origin]
    credentials: Mapping[str, Credential]
    data_sets: Sequence[TestDataSet]
    file_names: frozenset[str]

    @property
    def variables(self) -> dict[str, str]:
        """Plain variables only. Ones that point at a credential are not values."""
        return {
            str(item["name"]): str(item["value"])
            for item in self.environment.variables
            if isinstance(item, dict) and item.get("value") is not None
        }

    def data_for(self, script: Script, content: CaseContent) -> dict[str, str]:
        """Data sets the script names, in order, then the case's own data on top.

        Within a set name, the values for this environment replace the general ones.
        """
        data: dict[str, str] = {}
        for name in script.data_sets:
            for data_set in self.data_sets:  # general first, environment-specific after
                if data_set.name == name:
                    data.update({str(k): str(v) for k, v in data_set.values.items()})
        data.update({item.name: item.value for item in content.test_data if item.value is not None})
        return data

    def script_facts(self, script: Script, content: CaseContent) -> ScriptFacts:
        login_roles = set()
        for role, credential in self.credentials.items():
            try:
                LoginRecipe.model_validate(list(credential.login_recipe))
            except ValidationError:
                continue
            if credential.source == "stored":
                login_roles.add(role)
        environment = self.environment
        return ScriptFacts(
            allowed_origins=self.origins,
            allow_http=environment.kind != "production",
            read_only=environment.is_read_only,
            allowed_actions=frozenset(environment.allowed_actions),
            env_names=frozenset(self.variables),
            data_names=frozenset(self.data_for(script, content)),
            data_set_names=frozenset(d.name for d in self.data_sets),
            credentials={
                role: frozenset(c.field_names)
                for role, c in self.credentials.items()
                if c.source == "stored"
            },
            login_roles=frozenset(login_roles),
            file_names=self.file_names,
        )


async def load_environment_facts(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    environment: Environment,
) -> EnvironmentFacts:
    environments = EnvironmentRepository(session, organization_id)
    origins = (await environments.origins([environment.id]))[environment.id]
    credentials = await CredentialRepository(session, organization_id).list_in_environment(
        environment.id
    )
    data_sets = [
        d
        for d in await DataSetRepository(session, organization_id).list_in_project(project_id)
        if d.environment_id in (None, environment.id)
    ]
    files = await TestFileRepository(session, organization_id).list_in_project(project_id)
    return EnvironmentFacts(
        environment=environment,
        origins=frozenset(Origin(o.scheme, o.host, o.port) for o in origins),
        credentials={c.role_label: c for c in credentials},
        data_sets=data_sets,
        file_names=frozenset(f.name for f in files),
    )


def parse_script(automation: dict[str, Any] | None) -> tuple[Script | None, list[CaseIssue]]:
    """A stored script, or why it cannot be used."""
    if automation is None:
        return None, []
    try:
        return Script.model_validate(automation), []
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return None, [
            CaseIssue(
                "automation_invalid",
                "blocking",
                f"The script is not valid at {where}: {first['msg']}",
            )
        ]


async def automation_issues(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    environment_id: uuid.UUID | None,
    content: CaseContent,
    automation: dict[str, Any] | None,
) -> list[CaseIssue]:
    """Issues of a case's script, for review. Empty when the case has no script."""
    script, issues = parse_script(automation)
    if script is None:
        return issues
    environment = (
        await EnvironmentRepository(session, organization_id).get(environment_id)
        if environment_id
        else None
    )
    if environment is None:
        return [
            CaseIssue(
                "automation_unchecked",
                "warning",
                "The case has no environment, so its script is only checked when it is run.",
            )
        ]
    facts = await load_environment_facts(
        session, organization_id=organization_id, project_id=project_id, environment=environment
    )
    return check_script(script, facts.script_facts(script, content))
