"""Values a script refers to: resolved when a step needs them, never earlier."""

import secrets
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.browser.login import secret_values
from app.engine.errors import StepError
from app.testing.automation import PLACEHOLDER
from app.testing.automation_checks import UNIQUE_NAME


class Values:
    """Environment variables, test data, credentials, generated and saved values of one attempt."""

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        data: Mapping[str, str],
        credentials: Mapping[str, Mapping[str, str]],
    ) -> None:
        self._env = env
        self._data = data
        self._credentials = credentials
        self._unique: dict[str, str] = {}
        self._token = secrets.token_hex(4)
        self.saved: dict[str, Any] = {}
        self.responses: dict[str, dict[str, Any]] = {}
        self.secrets: frozenset[str] = frozenset(
            value for fields in credentials.values() for value in secret_values(dict(fields))
        )

    def _unique_value(self, name: str) -> str:
        if not UNIQUE_NAME.fullmatch(name):
            raise StepError("test_data", f"unique.{name} is not a kind of generated value.")
        if name not in self._unique:
            kind, _, suffix = name.partition("_")
            tag = f"{self._token}{secrets.token_hex(2)}" if suffix else self._token
            self._unique[name] = {
                "id": f"qa-{tag}",
                "text": f"QA test {tag}",
                "email": f"qa-{tag}@example.com",
                "number": str(int(tag, 16) % 10**9),
            }[kind]
        return self._unique[name]

    def lookup(self, namespace: str, name: str) -> Any:
        if namespace == "env" and name in self._env:
            return self._env[name]
        if namespace == "data" and name in self._data:
            return self._data[name]
        if namespace == "var" and name in self.saved:
            return self.saved[name]
        if namespace == "unique":
            return self._unique_value(name)
        if namespace == "cred":
            role, _, field = name.partition(".")
            if field in self._credentials.get(role, {}):
                return self._credentials[role][field]
        raise StepError("test_data", f"{namespace}.{name} has no value.")

    def render(self, text: str) -> str:
        return PLACEHOLDER.sub(lambda m: str(self.lookup(m.group(1), m.group(2))), text)

    def render_any(self, value: Any) -> Any:
        """Resolve placeholders in every string of a JSON-like value."""
        if isinstance(value, str):
            return self.render(value)
        if isinstance(value, dict):
            return {self.render(str(k)): self.render_any(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render_any(item) for item in value]
        return value

    def uses_secret(self, value: Any) -> bool:
        """Does this step put a secret credential field into the page?"""
        from app.testing.automation import placeholders_in

        for namespace, name in placeholders_in(value):
            if namespace == "cred":
                role, _, field = name.partition(".")
                if self._credentials.get(role, {}).get(field) in self.secrets:
                    return True
        return False

    def for_expression(self) -> dict[str, Any]:
        return {
            **{f"var.{k}": v for k, v in self.saved.items()},
            **{f"data.{k}": v for k, v in self._data.items()},
            **{f"env.{k}": v for k, v in self._env.items()},
        }


def step_dump(step: BaseModel) -> dict[str, Any]:
    return step.model_dump(exclude_none=True, exclude_defaults=True)
