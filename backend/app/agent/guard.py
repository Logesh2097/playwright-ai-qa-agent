"""Handling of untrusted text on its way into a prompt.

Everything a person typed (the instruction, clarification answers, names and
descriptions of applications) and, later, everything read from a web page is
*data*. This module is where that data is cleaned, fenced and inspected.

None of this is the security boundary. The boundary is structural: the model
receives no secrets, its answer must fit a schema that has no field for granting
anything, the application and environment come from the request and the
database, and nothing it says can approve a plan. The measures here reduce how
often a manipulated answer is produced and make attempts visible to reviewers.
"""

import re
import secrets
import unicodedata
from dataclasses import dataclass

# Phrases typical of text addressed to the model rather than describing a test.
_SUSPICIOUS = [
    (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any|system)\b.{0,30}\b(instruction|prompt|rule|message)s?",
        "tells the assistant to ignore its instructions",
    ),
    (r"\b(system|developer)\s+(prompt|message|instruction)s?\b", "refers to the system prompt"),
    (
        r"\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+to\s+be\b|\bnew\s+(role|persona)\b",
        "tries to change the assistant's role",
    ),
    (
        r"\b(reveal|print|show|output|send|leak|exfiltrat\w*|dump)\b.{0,60}\b(password|secret|credential|api[\s_-]?key|token|cookie|env(ironment)?\s+variable)s?",
        "asks for secrets to be disclosed",
    ),
    (
        r"\b(grant|give|enable|unlock)\b.{0,40}\b(admin|root|permission|access|privilege)s?",
        "asks for additional permissions",
    ),
    (
        r"\b(mark|report|treat)\b.{0,40}\b(all|every|each)?\s*(test|check|case)s?\b.{0,20}\b(as\s+)?pass(ed|ing)?\b",
        "asks for results to be reported as passed",
    ),
    (
        r"\b(approve|approved|auto-?approve)\b.{0,30}\b(plan|test|run|itself|automatically)",
        "asks for approval to be skipped",
    ),
    (r"</?\s*(untrusted|system|assistant|instructions?)\b", "imitates prompt delimiters"),
    (r"\b(jailbreak|developer\s+mode|dan\s+mode)\b", "uses a known jailbreak phrase"),
]
_SUSPICIOUS_PATTERNS = [(re.compile(p, re.IGNORECASE | re.DOTALL), why) for p, why in _SUSPICIOUS]

_URL = re.compile(r"\b(?:https?|ftp|file)://[^\s<>\"')\]]+", re.IGNORECASE)
_HOST = re.compile(
    r"(?<![@\w.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,24}))(?::\d{1,5})?(?![\w-])",
    re.IGNORECASE,
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_LOCAL_NAMES = re.compile(
    r"(?<![\w.-])(localhost|metadata\.google\.internal)(?![\w-])", re.IGNORECASE
)
# Words that look like host names to the pattern above but are ordinary writing.
_NOT_HOSTS = frozenset({"e.g", "i.e", "etc.etc"})


def clean_text(text: str, *, max_chars: int) -> str:
    """Normalise untrusted text: no control or invisible characters, bounded length."""
    text = unicodedata.normalize("NFKC", text)
    kept = []
    for character in text:
        category = unicodedata.category(character)
        if character in "\n\t" or category[0] != "C":
            kept.append(character)
        # Cc/Cf/Co/Cs: control, format (zero-width, bidi overrides), private use, surrogates
    cleaned = "".join(kept).strip()
    return cleaned[:max_chars]


@dataclass(frozen=True, slots=True)
class Fence:
    """Delimiters around untrusted content, unguessable by whoever wrote the content."""

    token: str

    @classmethod
    def new(cls) -> "Fence":
        return cls(secrets.token_hex(8))

    def wrap(self, label: str, content: str) -> str:
        # The content cannot close the fence: it does not know the token, and
        # anything shaped like one of our tags is defused before it goes in.
        safe = re.sub(r"<(/?)\s*untrusted", r"<\1 untrusted", content, flags=re.IGNORECASE)
        safe = safe.replace(self.token, "[removed]")
        return (
            f'<untrusted {label} id="{self.token}">\n{safe}\n</untrusted {label} id="{self.token}">'
        )


def suspicious_phrases(text: str) -> list[str]:
    """Reasons why a text looks like an attempt to instruct the model. Empty if none."""
    return list(dict.fromkeys(why for pattern, why in _SUSPICIOUS_PATTERNS if pattern.search(text)))


@dataclass(frozen=True, slots=True)
class Target:
    text: str
    # True for a URL with a scheme, an IP address or localhost: unmistakably an address.
    # False for a bare name such as "shop.example.com", which could be ordinary writing.
    explicit: bool


def mentioned_targets(text: str) -> list[Target]:
    """URLs, IP addresses and host names written in a text."""
    found: dict[str, Target] = {}

    def add(value: str, explicit: bool) -> None:
        found.setdefault(value.lower(), Target(value, explicit))

    for match in _URL.finditer(text):
        add(match.group(0).rstrip(".,;"), True)
    remainder = _URL.sub(" ", text)
    for match in _IPV4.finditer(remainder):
        add(match.group(0), True)
    for match in _LOCAL_NAMES.finditer(remainder):
        add(match.group(1), True)
    for match in _HOST.finditer(remainder):
        if match.group(1).lower() not in _NOT_HOSTS:
            add(match.group(0), False)
    return list(found.values())
