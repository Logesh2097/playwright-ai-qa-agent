"""Central prompt store.

Every prompt lives in this package as `<name>.v<version>.md`, with a `## system`
and a `## user` section. Code asks for a prompt by name and gets the highest
version, or a specific one. The name and version are logged with every call, so
a result can always be traced to the exact wording that produced it.

Placeholders use `$name` (string.Template): braces in JSON examples need no
escaping, and a missing value is an error rather than a silent blank.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from string import Template

_FILE = re.compile(r"^(?P<name>[a-z_]+)\.v(?P<version>\d+)\.md$")
_SECTION = re.compile(r"^## (system|user)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: int
    system: str
    user_template: Template

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"

    def render_user(self, **values: str) -> str:
        return self.user_template.substitute(**values)


def _parse(name: str, version: int, text: str) -> Prompt:
    parts = _SECTION.split(text)
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if set(sections) != {"system", "user"}:
        raise ValueError(
            f"prompt {name}.v{version} needs exactly a '## system' and a '## user' section"
        )
    return Prompt(name, version, sections["system"], Template(sections["user"]))


@lru_cache
def _all() -> dict[str, dict[int, Prompt]]:
    prompts: dict[str, dict[int, Prompt]] = {}
    for entry in resources.files(__package__).iterdir():
        match = _FILE.match(entry.name)
        if match:
            name, version = match["name"], int(match["version"])
            prompts.setdefault(name, {})[version] = _parse(name, version, entry.read_text("utf-8"))
    return prompts


def get_prompt(name: str, version: int | None = None) -> Prompt:
    versions = _all().get(name)
    if not versions:
        raise KeyError(f"no prompt named {name!r}")
    return versions[max(versions) if version is None else version]
