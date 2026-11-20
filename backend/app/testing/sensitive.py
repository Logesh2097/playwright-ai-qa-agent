"""Sensitive actions: steps that need a person's explicit approval before they run.

Deleting records, paying, sending messages, changing important settings and
destructive clean-up cannot be undone by running the test again. Such steps are
found by rules, from what the case and its script say, never by a model:

- a step of the case declared as deleting data or reaching outside the
  application (`side_effect: delete | external`);
- a script step that changes something and whose target, description or address
  names such an action ("Delete account", "Pay now", "Send invitation",
  `/api/settings`);
- an API request with the `DELETE` method.

What happens with them is decided by the execution service:

- against a **production** environment they are never run;
- elsewhere they run only if the person starting the execution approved them
  for that execution, and has the right to approve test cases;
- nothing automated (a CI key, a webhook, a workflow without that approval)
  can approve them: such cases are blocked and the rest of the run goes on.

The word lists are English and deliberately err towards asking. A step is
missed only if nothing about it says what it does; authors mark such steps with
`side_effect` on the case.
"""

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.testing.automation import Script, changes_state
from app.testing.cases import CaseContent

KINDS: dict[str, frozenset[str]] = {
    "delete": frozenset(
        {
            "delete",
            "deletes",
            "remove",
            "removes",
            "destroy",
            "erase",
            "purge",
            "wipe",
            "drop",
            "archive",
            "trash",
        }
    ),
    "payment": frozenset(
        {
            "pay",
            "payment",
            "payments",
            "purchase",
            "buy",
            "checkout",
            "transfer",
            "withdraw",
            "refund",
            "charge",
            "donate",
            "billing",
            "invoice",
        }
    ),
    "message": frozenset(
        {
            "send",
            "sends",
            "email",
            "sms",
            "notify",
            "invite",
            "invitation",
            "message",
            "broadcast",
            "publish",
        }
    ),
    "configuration": frozenset(
        {
            "settings",
            "configuration",
            "config",
            "permissions",
            "roles",
            "webhook",
            "webhooks",
            "deactivate",
            "disable",
            "revoke",
            "terminate",
            "unsubscribe",
            "admin",
        }
    ),
}
PHRASES: dict[str, tuple[str, ...]] = {
    "payment": ("place order", "submit order", "confirm order", "complete purchase"),
    "configuration": ("api key", "reset password", "change password", "cancel subscription"),
    "delete": ("clear all", "empty trash"),
}
LABELS = {
    "delete": "deletes records",
    "payment": "submits a payment or transaction",
    "message": "sends a message",
    "configuration": "changes important configuration",
    "external": "reaches outside the application",
    "destructive_cleanup": "is destructive clean-up",
}
_WORD = re.compile(r"[a-z]+")
# Steps that commit something: pressing, choosing, calling. Not typing.
ACTS = frozenset(
    {
        "click",
        "press",
        "check",
        "uncheck",
        "select_radio",
        "select_option",
        "api_request",
        "upload_file",
    }
)


@dataclass(frozen=True, slots=True)
class SensitiveStep:
    kind: str
    # Where it is: "case step 3", "script step 2", "cleanup step 1".
    where: str
    what: str

    @property
    def reason(self) -> str:
        return f"{self.where} {LABELS.get(self.kind, 'is sensitive')}: {self.what}"

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "where": self.where, "what": self.what}


def kind_of(text: str) -> str | None:
    """Which kind of sensitive action a piece of text names, if any."""
    lowered = " ".join(text.lower().replace("_", " ").replace("-", " ").split())
    for kind, phrases in PHRASES.items():
        if any(phrase in lowered for phrase in phrases):
            return kind
    words = set(_WORD.findall(lowered))
    for kind, vocabulary in KINDS.items():
        if words & vocabulary:
            return kind
    return None


def _texts(step: BaseModel) -> list[str]:
    found: list[str] = []
    locator: Any = getattr(step, "locator", None)
    while locator is not None:
        found += [
            str(v) for v in (getattr(locator, "name", None), getattr(locator, "value", None)) if v
        ]
        locator = getattr(locator, "within", None)
    for name in ("description", "url"):
        value = getattr(step, name, None)
        if isinstance(value, str):
            found.append(value)
    if getattr(step, "action", "") == "select_option" and isinstance(
        getattr(step, "value", None), str
    ):
        found.append(str(step.value))  # type: ignore[attr-defined]
    return found


def sensitive_steps(
    script: Script | None, content: CaseContent | None = None
) -> list[SensitiveStep]:
    """Every step of a case that needs explicit approval, with the reason."""
    found: list[SensitiveStep] = []
    if content is not None:
        for number, step in enumerate(content.steps, start=1):
            if step.side_effect in ("delete", "external"):
                found.append(
                    SensitiveStep(step.side_effect, f"case step {number}", step.description[:120])
                )
    if script is None:
        return found
    for phase, steps in (("script", script.steps), ("cleanup", script.cleanup)):
        for number, script_step in enumerate(steps, start=1):
            if not changes_state(script_step):
                continue
            action = str(getattr(script_step, "action", ""))
            # Typing into a field named "Email" sends nothing; the click that follows may.
            if action not in ACTS:
                continue
            where = f"{phase} step {number}"
            if action == "api_request" and getattr(script_step, "method", "GET") == "DELETE":
                kind: str | None = "delete"
                what = f"DELETE {getattr(script_step, 'url', '')}"[:120]
            else:
                texts = _texts(script_step)
                kind = next((k for k in map(kind_of, texts) if k), None)
                what = (texts[0] if texts else action)[:120]
            if kind is None:
                continue
            if phase == "cleanup" and kind == "delete":
                kind = "destructive_cleanup"
            found.append(SensitiveStep(kind, where, what))
    return found
