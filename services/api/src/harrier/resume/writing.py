"""How the CV's own text reads (spec 164, rules W1 to W8).

Deterministic: no model call and no network. It reads the bundle's pool
lines and its framing strings and returns one advisory message per finding.
A finding names the bullet id or the framing field and the matched phrase,
never the line's text. Nothing here refuses anything: `profile check` prints
the findings after the truth gate's lines and keeps the gate's exit code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from harrier.apply.claims import number_tokens
from harrier.connection import Connection
from harrier.resume.content import (
    ResumeBundle,
    ResumeBundleError,
    load_bundle,
    strip_inline_markup,
)
from harrier.resume.plan import sentence_count
from harrier.tracks import Scope

# W1: a pool line never begins with one of these.
WEAK_OPENERS: tuple[str, ...] = (
    "responsible for",
    "worked on",
    "working on",
    "helped",
    "assisted",
    "involved in",
    "participated in",
    "tasked with",
    "duties included",
    "in charge of",
    "was",
    "were",
)

# W2: matched in any case, except "US" in capitals (the country). "I" counts
# only as a capital standing alone: not inside "API", "I/O" or "i.e.".
FIRST_PERSON: tuple[str, ...] = ("me", "my", "mine", "we", "us", "our")

# W6: negative framing, and passive voice about one's own growth.
NEGATIVE_FRAMING: tuple[str, ...] = (
    "had to",
    "was required to",
    "were required to",
    "was given the opportunity",
)
PASSIVE_GROWTH_VERBS: tuple[str, ...] = ("developed", "strengthened", "improved", "gained", "built")

# W7: self-description a framing string states without a fact.
SELF_DESCRIPTION: tuple[str, ...] = (
    "hard-working",
    "hardworking",
    "team player",
    "passionate",
    "highly motivated",
    "self-starter",
    "detail-oriented",
    "results-driven",
    "strong work ethic",
    "excellent communication skills",
)

# W8: a pool line holds at most this many words and two sentences.
MAX_WORDS = 35
MAX_SENTENCES = 2

FRAMING_FIELDS: tuple[str, ...] = ("profile_summary", "profile_lead", "experience_statement")
ADVISORY_HEADER = "writing (advisory):"


def _phrases(phrases: Iterable[str]) -> str:
    """An alternation of whole phrases, longest first."""
    return "|".join(re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True))


_WEAK_OPENER = re.compile(rf"^(?:{_phrases(WEAK_OPENERS)})\b", re.IGNORECASE)
_CAPITAL_I = re.compile(r"(?<![\w/.])I(?![\w/]|\.\w)")
_FIRST_PERSON = re.compile(rf"\b(?:{_phrases(FIRST_PERSON)})\b", re.IGNORECASE)
_NEGATIVE = re.compile(
    rf"\b(?:{_phrases(NEGATIVE_FRAMING)}"
    rf"|(?:was|were)\s+(?:{_phrases(PASSIVE_GROWTH_VERBS)})\s+(?:by|through))\b",
    re.IGNORECASE,
)
_SELF_DESCRIPTION = re.compile(rf"\b(?:{_phrases(SELF_DESCRIPTION)})\b", re.IGNORECASE)
_WORD_EDGE = "\"'([{*`\u201c\u2018"
_WORD_TAIL = ".,;:!?\"')]}*`\u201d\u2019"


def writing_report(conn: Connection, scope: Scope) -> list[str]:
    """The advisory block `profile check` prints: the header and every
    finding, or nothing when there is none or the bundle does not load."""
    try:
        bundle = load_bundle(conn, scope)
    except ResumeBundleError:
        return []
    findings = writing_findings(bundle)
    return [ADVISORY_HEADER, *findings] if findings else []


def writing_findings(bundle: ResumeBundle) -> list[str]:
    """Every W1 to W8 finding, in rule order, W4's role lines last."""
    pool = {bullet_id: strip_inline_markup(text) for bullet_id, text in bundle.bullet_pool.items()}
    framing = {
        name: strip_inline_markup(value)
        for name in FRAMING_FIELDS
        if (value := str(getattr(bundle, name)))
    }
    roles = {
        role.id: [bullet_id for bullet_id in pool if bullet_id.startswith(f"{role.id}_")]
        for role in bundle.roles
    }
    achievements = [bullet_id for bullet_id in pool if bullet_id.startswith("ach_")]
    findings: list[str] = []
    for bullet_id, text in pool.items():
        if opener := _WEAK_OPENER.match(text):
            phrase = opener.group(0).lower()
            findings.append(f'bullet {bullet_id}: opens with "{phrase}"; lead with what was done')
    for name, text in [*_labelled(pool), *framing.items()]:
        if word := first_person(text):
            findings.append(f'{name}: first person ("{word}"); use the implied first person')
    findings.extend(
        f"bullet {bullet_id}: an achievement with no figure"
        for bullet_id in achievements
        if not number_tokens(pool[bullet_id])
    )
    for role_id, ids in roles.items():
        findings.extend(_repeated_openers(f"role {role_id}", ids, pool))
    findings.extend(_repeated_openers("achievements", achievements, pool))
    for name, text in [*_labelled(pool), *framing.items()]:
        if negative := _NEGATIVE.search(text):
            findings.append(f'{name}: negative or passive framing ("{negative.group(0)}")')
    for name, text in framing.items():
        if described := _SELF_DESCRIPTION.search(text):
            findings.append(
                f'{name}: self-description ("{described.group(0)}"); show it with a fact instead'
            )
    for bullet_id, text in pool.items():
        words = len(text.split())
        if words > MAX_WORDS:
            findings.append(f"bullet {bullet_id}: {words} words; keep a bullet under {MAX_WORDS}")
        elif (sentences := sentence_count(text)) > MAX_SENTENCES:
            findings.append(
                f"bullet {bullet_id}: {sentences} sentences; keep a bullet to {MAX_SENTENCES}"
            )
    for role_id, ids in roles.items():
        figured = sum(1 for bullet_id in ids if number_tokens(pool[bullet_id]))
        findings.append(f"role {role_id}: {figured} of {len(ids)} bullets state a figure")
    return findings


def first_person(text: str) -> str | None:
    """The first first-person word in `text` (W2), as written, or None."""
    found = [match for match in _FIRST_PERSON.finditer(text) if match.group(0) != "US"]
    found.extend(_CAPITAL_I.finditer(text))
    if not found:
        return None
    return min(found, key=lambda match: match.start()).group(0)


def _labelled(pool: dict[str, str]) -> list[tuple[str, str]]:
    return [(f"bullet {bullet_id}", text) for bullet_id, text in pool.items()]


def _repeated_openers(label: str, ids: list[str], pool: dict[str, str]) -> list[str]:
    """W5: each first word two or more of the lines share, as first written."""
    openers: dict[str, list[str]] = {}
    for bullet_id in ids:
        words = pool[bullet_id].split()
        if not words:
            continue
        word = words[0].lstrip(_WORD_EDGE).rstrip(_WORD_TAIL)
        if word:
            openers.setdefault(word.casefold(), []).append(word)
    return [
        f'{label}: {len(forms)} bullets open with "{forms[0]}"'
        for forms in openers.values()
        if len(forms) > 1
    ]
