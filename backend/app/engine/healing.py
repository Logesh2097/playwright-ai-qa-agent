"""Self-healing: recovering from a locator that no longer matches anything.

When a step's locator matches no element, the engine looks at the page as it
is at that moment, and may find the element the step was written for under a
changed name. Everything here is rules; no model is involved.

What can be healed is only *where* a step acts. The action, the value typed,
the assertion and its expected value are never changed. A test whose
expectation no longer holds fails, as it should.

A replacement is used automatically only if all of these hold:

1. the original locator said what the element is called (role, label, text or
   placeholder; a CSS selector or test ID carries no name to compare with);
2. the candidate has the same role, or one the action can be performed on;
3. its name is close to the original's (stricter for steps that change data);
4. no other candidate is nearly as close;
5. the new name does not point at a different or destructive action;
6. the replacement locator matches exactly one element.

Otherwise the candidate is recorded as a proposal for a person to look at, and
the step fails as it would have.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.engine.locators import find, matched
from app.engine.values import Values
from app.testing.automation import Locator

ACTING = frozenset({"click", "check", "uncheck", "select_radio", "fill", "clear", "select_option"})
# Steps this module never touches: a file upload target and anything without a locator.
NOT_HEALED = frozenset({"upload_file"})
ROLES_FOR_ACTION: dict[str, frozenset[str]] = {
    "click": frozenset(
        {"button", "link", "menuitem", "tab", "checkbox", "radio", "switch", "option"}
    ),
    "fill": frozenset({"textbox", "searchbox", "spinbutton", "combobox"}),
    "clear": frozenset({"textbox", "searchbox", "spinbutton", "combobox"}),
    "select_option": frozenset({"combobox", "listbox"}),
    "check": frozenset({"checkbox", "switch"}),
    "uncheck": frozenset({"checkbox", "switch"}),
    "select_radio": frozenset({"radio"}),
}
_ANY_ROLE = frozenset(
    {
        "alert", "button", "checkbox", "combobox", "heading", "link", "listbox", "menuitem",
        "option", "radio", "searchbox", "slider", "spinbutton", "status", "switch", "tab",
        "textbox", "img", "cell", "row", "listitem",
    }
)  # fmt: skip

RECOVER_ACTING = 0.80
RECOVER_READING = 0.75
PROPOSE = 0.50
MARGIN = 0.15
MAX_CANDIDATES = 3

_ELEMENT = re.compile(r'^\s*- ([a-z]+) "((?:[^"\\]|\\.)*)"', re.M)
_WORD = re.compile(r"[a-z0-9]+")
# Words that mean something is destroyed, spent, sent or ended.
_DESTRUCTIVE = frozenset(
    {
        "delete", "remove", "destroy", "erase", "purge", "wipe", "drop", "cancel", "pay", "purchase",
        "buy", "checkout", "order", "transfer", "send", "withdraw", "refund", "deactivate", "disable",
        "revoke", "reset", "unsubscribe", "logout", "signout", "terminate", "close", "discard", "reject",
        "decline", "block", "ban", "archive", "publish", "submit", "confirm",
    }
)  # fmt: skip
_OPPOSITES = (
    ("in", "out"), ("on", "off"), ("enable", "disable"), ("add", "remove"), ("accept", "decline"),
    ("approve", "reject"), ("allow", "deny"), ("next", "previous"), ("open", "close"),
    ("start", "stop"), ("show", "hide"), ("lock", "unlock"), ("subscribe", "unsubscribe"),
    ("follow", "unfollow"), ("activate", "deactivate"), ("yes", "no"), ("save", "discard"),
    ("up", "down"), ("increase", "decrease"), ("expand", "collapse"), ("mute", "unmute"),
    ("login", "logout"), ("first", "last"), ("create", "delete"), ("keep", "delete"),
)  # fmt: skip
_NEGATIONS = frozenset({"not", "no", "never", "dont", "without", "un", "non"})


@dataclass(frozen=True, slots=True)
class Candidate:
    role: str
    name: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "score": round(self.score, 2)}


@dataclass(slots=True)
class Healing:
    """What was found for one failed locator, and what may be done with it."""

    decision: str  # "recover", "propose" or "none"
    original: dict[str, Any]
    replacement: Locator | None = None
    confidence: float = 0.0
    rationale: str = ""
    # Why the replacement may not be used without a person, if it may not.
    held_back: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    # Set by the caller: the step was performed with the replacement and it worked.
    verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "verified": self.verified,
            "original": self.original,
            "replacement": self.replacement.model_dump(exclude_none=True, exclude_defaults=True)
            if self.replacement
            else None,
            "confidence": round(self.confidence, 2),
            "rationale": self.rationale,
            "held_back": self.held_back,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("'", ""))


def similarity(wanted: str, found: str) -> float:
    """How alike two element names are, from 0 to 1.

    Ignores case, punctuation and spacing. A name that contains all the words of
    the other ("Save" / "Save changes") counts as close, but less than equal.
    """
    a, b = words(wanted), words(found)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 1 and all(word in longer for word in shorter):
        # All words kept, some added: more added words, less sure.
        ratio = max(ratio, 0.95 - 0.07 * (len(longer) - len(shorter)))
    return round(min(ratio, 0.99), 4)


def _squash(text: str) -> str:
    return "".join(words(text))


def _stem(word: str) -> str:
    """Singular and plural are the same word here."""
    for ending in ("ies", "es", "s"):
        if len(word) > len(ending) + 2 and word.endswith(ending):
            return word[: -len(ending)] + ("y" if ending == "ies" else "")
    return word


def _same_letters(gone: set[str], new: set[str]) -> bool:
    """The same letters split differently: "email" against "e" + "mail"."""
    return "".join(sorted(gone)) == "".join(sorted(new)) or "".join(
        sorted("".join(gone))
    ) == "".join(sorted("".join(new)))


def changes_meaning(wanted: str, found: str) -> str | None:
    """Reasons a similar-looking name must not be trusted for an action, if there are any."""
    before, after = set(words(wanted)), set(words(found))
    # A different number is a different thing: another amount, another row, another year.
    if {w for w in before if w.isdigit()} != {w for w in after if w.isdigit()}:
        return "the names contain different numbers"
    # Words may be added or dropped ("Save" / "Save changes"). A word that was *replaced*
    # by another names something else ("Message Anna" / "Message Hanna"). Spelling of the
    # same words ("Email" / "E-mail", "report" / "reports") is not a replacement.
    if _squash(wanted) != _squash(found):
        gone = {_stem(w) for w in before} - {_stem(w) for w in after}
        new = {_stem(w) for w in after} - {_stem(w) for w in before}
        if gone and new and not _same_letters(gone, new):
            return f"'{sorted(gone)[0]}' was replaced by '{sorted(new)[0]}'"
    added = after - before
    new_destructive = sorted(added & _DESTRUCTIVE)
    if new_destructive:
        return f"the new name adds '{new_destructive[0]}', which the original did not have"
    for one, other in _OPPOSITES:
        if (one in before and other in after and other not in before) or (
            other in before and one in after and one not in before
        ):
            return f"the names differ by '{one}' and '{other}', which mean opposite things"
    if added & _NEGATIONS or (before - after) & _NEGATIONS:
        return "one of the names is a negation of the other"
    for word in added:
        for prefix in ("un", "dis", "de", "non"):
            if word.startswith(prefix) and word[len(prefix) :] in before:
                return f"'{word}' is the opposite of '{word[len(prefix) :]}'"
    for word in before - after:
        for prefix in ("un", "dis", "de", "non"):
            if word.startswith(prefix) and word[len(prefix) :] in after:
                return f"'{word[len(prefix) :]}' is the opposite of '{word}'"
    return None


def inventory(snapshot: str) -> list[tuple[str, str]]:
    """(role, name) of every named element in an accessibility snapshot, once each."""
    seen: dict[tuple[str, str], None] = {}
    for role, name in _ELEMENT.findall(snapshot):
        if name.strip():
            seen.setdefault((role, name.replace('\\"', '"')), None)
    return list(seen)


def choose(
    *,
    action: str,
    original: Locator,
    wanted_name: str | None,
    elements: list[tuple[str, str]],
) -> Healing:
    """Rank the page's elements against what the step was looking for. Pure."""
    record = original.model_dump(exclude_none=True, exclude_defaults=True)
    if wanted_name is None or not words(wanted_name):
        return Healing(
            "none", record, rationale="The original locator carries no name to compare with."
        )
    roles = (
        frozenset({original.role})
        if original.by == "role" and original.role
        else ROLES_FOR_ACTION.get(action, _ANY_ROLE)
    )
    ranked = sorted(
        (
            Candidate(role, name, similarity(wanted_name, name))
            for role, name in elements
            if role in roles
        ),
        key=lambda c: (-c.score, c.name),
    )
    shortlist = [c for c in ranked if c.score >= PROPOSE][:MAX_CANDIDATES]
    if not shortlist:
        return Healing(
            "none", record, rationale="No element with a similar name and a fitting role was found."
        )

    best = shortlist[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    replacement = Locator(
        by="role", role=best.role, name=best.name, exact=True, within=original.within
    )
    healing = Healing(
        "propose",
        record,
        replacement=replacement,
        confidence=best.score,
        candidates=shortlist,
        rationale=f'A {best.role} named "{best.name}" is the closest match for "{wanted_name}".',
    )
    acting = action in ACTING
    threshold = RECOVER_ACTING if acting else RECOVER_READING
    if best.score < threshold:
        healing.held_back = (
            f"The names are not close enough ({best.score:.2f}, needed {threshold:.2f})."
        )
    elif best.score - runner_up < MARGIN:
        healing.held_back = (
            "Another element is almost as close a match, so the right one is not certain."
        )
    elif (reason := changes_meaning(wanted_name, best.name)) is not None:
        healing.held_back = f"It could trigger a different or destructive action: {reason}."
    else:
        healing.decision = "recover"
    return healing


def wanted_name_of(locator: Locator, values: Values) -> str | None:
    if locator.by == "role":
        return values.render(locator.name) if locator.name else None
    if locator.by in ("label", "text", "placeholder"):
        return values.render(locator.value or "")
    return None  # css, test_id: an identifier, not a name a person sees


async def find_replacement(page: Page, *, action: str, locator: Locator, values: Values) -> Healing:
    """Inspect the current page for the element a failed locator was written for."""
    record = locator.model_dump(exclude_none=True, exclude_defaults=True)
    if action in NOT_HEALED:
        return Healing("none", record, rationale="This kind of step is not healed.")
    try:
        scope = find(page, locator.within, values) if locator.within else page.locator("body")
        if await matched(scope) != 1:
            return Healing(
                "none",
                record,
                rationale="The part of the page the step looks in could not be found.",
            )
        snapshot = await scope.aria_snapshot(timeout=5_000)
    except PlaywrightError:
        return Healing("none", record, rationale="The page could not be inspected.")
    healing = choose(
        action=action,
        original=locator,
        wanted_name=wanted_name_of(locator, values),
        elements=inventory(snapshot),
    )
    if healing.replacement is not None:
        # The replacement has to identify exactly one element, like any locator.
        count = await matched(find(page, healing.replacement, values))
        if count != 1:
            healing.decision = "propose" if count else "none"
            healing.held_back = (
                f"The replacement matches {count} elements; it must match exactly one."
            )
    return healing
