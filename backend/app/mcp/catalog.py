"""The tools a server said it has, and validation of arguments against them.

Nothing here is specific to one server: names, schemas and hints all come from
discovery.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import exceptions as schema_errors
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for

from app.mcp.errors import InvalidToolArgumentsError, UnknownToolError

_MAX_PROBLEMS = 8


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One discovered tool. The hints are the server's own claims, not facts."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    title: str | None = None
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None

    @property
    def argument_names(self) -> frozenset[str]:
        properties = self.input_schema.get("properties")
        return frozenset(properties) if isinstance(properties, Mapping) else frozenset()

    def summary(self) -> dict[str, Any]:
        """What is stored about a discovered tool: no schema bodies, only names."""
        return {
            "name": self.name,
            "title": self.title,
            "arguments": sorted(self.argument_names),
            "read_only_hint": self.read_only_hint,
        }


@dataclass(slots=True)
class ToolCatalog:
    """Discovered tools with a compiled validator each.

    A tool whose schema is not valid JSON Schema is left out and listed in
    `rejected`: arguments for it could not be checked, so it cannot be called.
    """

    _tools: dict[str, ToolSpec] = field(default_factory=dict)
    _validators: dict[str, Validator] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, tools: Iterable[ToolSpec]) -> "ToolCatalog":
        catalog = cls()
        for tool in tools:
            if tool.name in catalog._tools:
                catalog.rejected[tool.name] = "listed more than once"
                del catalog._tools[tool.name]
                del catalog._validators[tool.name]
                continue
            if tool.name in catalog.rejected:
                continue
            schema = dict(tool.input_schema)
            if schema.get("type") != "object":
                catalog.rejected[tool.name] = "input schema is not an object schema"
                continue
            try:
                validator_class = validator_for(schema)
                validator_class.check_schema(schema)
            except schema_errors.SchemaError:
                catalog.rejected[tool.name] = "input schema is not valid JSON Schema"
                continue
            catalog._tools[tool.name] = tool
            catalog._validators[tool.name] = validator_class(schema)
        return catalog

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"The server has no usable tool named {name!r}.") from None

    def validate(self, name: str, arguments: Mapping[str, Any]) -> None:
        """Raise `InvalidToolArgumentsError` unless the arguments fit the tool's schema."""
        self.get(name)
        problems: list[str] = []
        try:
            errors = sorted(
                self._validators[name].iter_errors(dict(arguments)), key=lambda e: list(e.path)
            )
        except Exception:
            raise InvalidToolArgumentsError(
                name, ["the tool's schema could not be applied"]
            ) from None
        for error in errors[:_MAX_PROBLEMS]:
            problems.append(_describe(error))
        if len(errors) > _MAX_PROBLEMS:
            problems.append(f"and {len(errors) - _MAX_PROBLEMS} more")
        if problems:
            raise InvalidToolArgumentsError(name, problems)


def _describe(error: schema_errors.ValidationError) -> str:
    """Field and rule only. `error.message` would repeat the value, which may be secret."""
    where = ".".join(str(part) for part in error.absolute_path) or "arguments"
    rule = str(error.validator)
    if rule == "required":
        missing = [
            name
            for name in error.validator_value
            if isinstance(error.instance, Mapping) and name not in error.instance
        ]
        return f"{where}: missing {', '.join(missing)}"
    if rule == "additionalProperties" and isinstance(error.instance, Mapping):
        allowed = set(error.schema.get("properties", {}))
        extra = sorted(str(key) for key in error.instance if key not in allowed)
        return f"{where}: unexpected {', '.join(extra)}"
    if rule in ("type", "enum", "minimum", "maximum", "minLength", "maxLength", "minItems"):
        return f"{where}: must satisfy {rule} {error.validator_value!r}"
    return f"{where}: does not satisfy {rule}"
