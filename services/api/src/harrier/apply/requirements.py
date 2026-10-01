"""What a posting asks of the candidate, and which questions only the
candidate can answer (spec 066).

Flags surface hard requirements for the operator to decide on. They never
filter or score: screening owns that, and EU permit phrases stay positive
signals there (harrier.screening.rules). Requirement and opinion questions
are kept away from the model, because no truth document holds the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REQUIREMENT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "time_zone": (
        re.compile(r"\btime ?zones?\b", re.IGNORECASE),
        re.compile(r"\b(?:CET|CEST|GMT|UTC|BST|EST|PST)\b"),
        re.compile(r"\boverlap\b.{0,40}\bhours?\b", re.IGNORECASE),
        re.compile(r"\bworking hours\b", re.IGNORECASE),
    ),
    "travel": (
        re.compile(r"\btravel(?:l?ing)?\b", re.IGNORECASE),
        re.compile(r"\boff-?sites?\b", re.IGNORECASE),
        re.compile(r"\bon-?site visits?\b", re.IGNORECASE),
    ),
    "visa_sponsorship": (
        re.compile(r"\bvisas?\b", re.IGNORECASE),
        re.compile(r"\bsponsor(?:s|ship)?\b", re.IGNORECASE),
    ),
    "work_authorization": (
        re.compile(r"\bwork authori[sz]ation\b", re.IGNORECASE),
        re.compile(r"\bauthori[sz]ed to work\b", re.IGNORECASE),
        re.compile(r"\b(?:right|eligible|eligibility) to work\b", re.IGNORECASE),
        re.compile(r"\bwork permits?\b", re.IGNORECASE),
        re.compile(r"\bcitizenship\b", re.IGNORECASE),
        re.compile(r"\bsecurity clearance\b", re.IGNORECASE),
    ),
}

_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class Flag:
    kind: str
    sentence: str


def _kinds(text: str) -> list[str]:
    return [
        kind
        for kind, patterns in REQUIREMENT_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    ]


def requirement_flags(*texts: str) -> list[Flag]:
    """Each sentence of the posting and guidance that states a requirement
    of one of the four kinds, once per kind (B5)."""
    flags: list[Flag] = []
    for text in texts:
        for sentence in _SENTENCES.split(text or ""):
            stripped = sentence.strip()
            if stripped:
                flags.extend(Flag(kind, stripped) for kind in _kinds(stripped))
    return list(dict.fromkeys(flags))


def requirement_kind(question: str) -> str | None:
    """The requirement a question asks about, if any (B6)."""
    kinds = _kinds(question)
    return kinds[0] if kinds else None


_OPINION_PHRASES = re.compile(
    r"\b(?:favou?rite|your opinion|what do you think about|how do you feel about)\b",
    re.IGNORECASE,
)
_FEELING = re.compile(r"\b(?:love|hate|dislike)\b", re.IGNORECASE)
_SUBJECT = re.compile(
    r"\b(?:tools?|languages?|frameworks?|librar(?:y|ies)|technolog(?:y|ies))\b", re.IGNORECASE
)


def is_opinion_question(question: str) -> bool:
    """A question only the candidate's own view can answer (B7). "Love" alone
    is not enough: "why would you love working here" asks for interest."""
    if _OPINION_PHRASES.search(question):
        return True
    return bool(_FEELING.search(question) and _SUBJECT.search(question))


_CURRENCY = r"(?:[€$£]|\b(?:EUR|USD|GBP|CHF)\s?)"
_AMOUNT = r"\d[\d,.]*\s?[kK]?"
_DASH = r"\s*(?:-|\u2013|\u2014|to)\s*"
_CODE_AFTER = r"\s?(?:EUR|USD|GBP|CHF)\b"
_RANGE = re.compile(
    rf"{_CURRENCY}{_AMOUNT}{_DASH}{_CURRENCY}?{_AMOUNT}(?:{_CODE_AFTER})?"
    rf"|\b{_AMOUNT}{_DASH}{_AMOUNT}{_CODE_AFTER}"
)


def posted_ranges(posting: str) -> list[str]:
    """Every currency range in the posting, verbatim (B8)."""
    return list(dict.fromkeys(match.group(0).strip() for match in _RANGE.finditer(posting or "")))
