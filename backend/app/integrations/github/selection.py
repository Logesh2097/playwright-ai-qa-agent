"""Which suites a change concerns. Rules written by people; no model, no guessing.

A repository link lists the suites of a **full** run and, optionally, path
rules: "changes under `src/billing/**` concern these suites". A run is narrowed
only when that is safe:

- the list of changed files is known to be complete;
- every changed file is covered by at least one rule;
- the rules point at suites that are part of the full run.

In every other case the full set runs. A file nobody wrote a rule for is a
reason to test everything, not a reason to test nothing.
"""

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

MAX_UNMATCHED_SHOWN = 20


@dataclass(frozen=True, slots=True)
class PathRule:
    pattern: str
    suite_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Selection:
    # "full" or "affected"
    scope: str
    suite_ids: list[str]
    reason: str
    changed_files: int = 0
    unmatched: list[str] = field(default_factory=list)
    # suite id -> the rules' patterns that selected it
    matched: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "suite_ids": self.suite_ids,
            "reason": self.reason,
            "changed_files": self.changed_files,
            "unmatched_files": self.unmatched[:MAX_UNMATCHED_SHOWN],
            "unmatched_count": len(self.unmatched),
            "matched": self.matched,
            "decided_by": "path rules",
        }


def _regex(pattern: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` and `?` do not; a trailing `/` means everything below."""
    if pattern.endswith("/"):
        pattern += "**"
    out, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out, index = out + "(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            out, index = out + ".*", index + 2
        elif pattern[index] == "*":
            out, index = out + "[^/]*", index + 1
        elif pattern[index] == "?":
            out, index = out + "[^/]", index + 1
        else:
            out, index = out + re.escape(pattern[index]), index + 1
    return re.compile(out + r"\Z")


def path_matches(path: str, pattern: str) -> bool:
    if "/" not in pattern.rstrip("/") and "**" not in pattern and not pattern.endswith("/"):
        # A bare name pattern such as `*.md` applies to the file name at any depth.
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    return bool(_regex(pattern.lstrip("/")).match(path))


def parse_rules(raw: Sequence[Any]) -> list[PathRule]:
    rules: list[PathRule] = []
    for item in raw:
        if isinstance(item, dict) and item.get("pattern") and item.get("suite_ids"):
            rules.append(PathRule(str(item["pattern"]), tuple(str(s) for s in item["suite_ids"])))
    return rules


def select(
    *,
    mode: str,
    full_suite_ids: Sequence[str],
    rules: Sequence[PathRule],
    changed_files: Sequence[str] | None,
    complete: bool,
) -> Selection:
    """`changed_files` is None when the change could not be read at all."""
    full = list(dict.fromkeys(full_suite_ids))
    count = len(changed_files) if changed_files is not None else 0

    def everything(reason: str, **more: Any) -> Selection:
        return Selection("full", full, reason, changed_files=count, **more)

    if mode != "affected":
        return everything("This repository always runs the full set.")
    if not rules:
        return everything("No path rules are configured, so nothing can be narrowed.")
    if changed_files is None:
        return everything("The changed files could not be read, so the full set runs.")
    if not complete:
        return everything("The list of changed files may be incomplete, so the full set runs.")
    if not changed_files:
        return everything("No changed files were reported, so the full set runs.")

    matched: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for path in changed_files:
        hit = False
        for rule in rules:
            if path_matches(path, rule.pattern):
                hit = True
                for suite in rule.suite_ids:
                    patterns = matched.setdefault(suite, [])
                    if rule.pattern not in patterns:
                        patterns.append(rule.pattern)
        if not hit:
            unmatched.append(path)
    if unmatched:
        return everything(
            f"{len(unmatched)} changed file(s) are not covered by any path rule, so the full set runs.",
            unmatched=unmatched,
            matched=matched,
        )
    chosen = [suite for suite in full if suite in matched]
    if not chosen:
        return everything(
            "The matching rules point at no suite of this repository's set, so the full set runs.",
            matched=matched,
        )
    if len(chosen) == len(full):
        return everything("The change concerns every suite.", matched=matched)
    return Selection(
        "affected",
        chosen,
        f"Every one of the {count} changed file(s) is covered by a path rule; "
        f"{len(chosen)} of {len(full)} suites are concerned.",
        changed_files=count,
        matched={suite: matched[suite] for suite in chosen},
    )
