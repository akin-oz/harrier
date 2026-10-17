"""The cover letter linter (spec 158, rules L1 to L11).

Deterministic: no model call and no network. It reads the letter, the
evidence the letter may cite, the posting, and the record's professional
years, and returns one message per violation. The industry and focused
kinds run it through `cover_letter_violations`; the academic kind does not.

The rules name no fact about the candidate. Every value they compare
against comes from the context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harrier.apply.claims import number_tokens
from harrier.resume.content import year_counts
from harrier.resume.dashes import dash_marks, describe

# L2. "may" is not here: it is also a month.
HEDGES: tuple[str, ...] = (
    "hopefully",
    "perhaps",
    "possibly",
    "somewhat",
    "fairly",
    "relatively",
    "arguably",
    "might",
    "I believe",
    "I think",
    "I feel",
    "happy to",
    "would love to",
    "I hope",
)

# L4: the hook never begins with one of these.
BANNED_OPENERS: tuple[str, ...] = (
    "I am writing",
    "I'm writing",
    "I am applying",
    "I'm applying",
    "I am excited",
    "I'm excited",
    "The role",
    "This role",
    "Your team",
)

# L5: refused anywhere in the letter.
BANNED_ANYWHERE: tuple[str, ...] = (
    "I am writing to apply",
    "excited",
    "let me know",
    "looking forward",
)

# L7: a shared run of this many words is a restatement.
RESTATEMENT_WORDS = 8

# L10: legal entity types a letter may not call "my" without evidence.
ENTITY_TYPES: tuple[str, ...] = ("OÜ", "GmbH", "Ltd", "LLC", "BV", "SAS", "SRL", "sp. z o.o.")

# L11: the role line's length.
ROLE_LINE_MAX_WORDS = 15

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_APOSTROPHE = "\u2019"
_WORD = re.compile(rf"[^\W_]+(?:['{_APOSTROPHE}][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class LintContext:
    """What the linter reads besides the letter."""

    # Every document the letter may cite, as one text (spec 158).
    evidence: str
    posting: str
    company: str
    role: str
    # The record's completed professional years; None when the record
    # cannot be read, and then L9 checks only for "decade".
    professional_years: int | None = None


def _phrase(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)


def _blocks(text: str) -> list[str]:
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


def _sentences(block: str) -> list[str]:
    return [part for part in _SENTENCE_END.split(block.strip()) if part.strip()]


def _words(text: str) -> list[str]:
    return [word.lower().replace(_APOSTROPHE, "'") for word in _WORD.findall(text)]


def _windows(text: str) -> set[tuple[str, ...]]:
    words = _words(text)
    return {
        tuple(words[at : at + RESTATEMENT_WORDS])
        for at in range(len(words) - RESTATEMENT_WORDS + 1)
    }


def _shared_runs(text: str, posting: str, evidence: str) -> list[str]:
    """Each run of `RESTATEMENT_WORDS` words in `text` that the posting also
    has, case and punctuation aside. A run the evidence also has is the
    candidate's own fact in the posting's words, not a restatement."""
    windows = _windows(posting) - _windows(evidence)
    words = _words(text)
    runs: list[str] = []
    at = 0
    while at <= len(words) - RESTATEMENT_WORDS:
        window = tuple(words[at : at + RESTATEMENT_WORDS])
        if window in windows:
            runs.append(" ".join(window))
            at += RESTATEMENT_WORDS
        else:
            at += 1
    return runs


def _starts_with(text: str, prefix: str) -> bool:
    return bool(prefix.strip()) and text.lower().startswith(prefix.strip().lower())


def _registered(entity: str, evidence: str) -> bool:
    """Whether an evidence line names the entity type as registered. A whole
    word: "unregistered" and "deregistered" are not registration."""
    for line in evidence.splitlines():
        lowered = line.lower()
        if (
            _phrase(entity).search(line)
            and re.search(r"\bregistered\b", lowered)
            and not re.search(r"\bnot\b", lowered)
        ):
            return True
    return False


def lint_letter(full_version: str, context: LintContext, short_version: str = "") -> list[str]:
    """Every rule of L1 to L11 the letter breaks. Empty when it passes.

    L11's block count is the kind's and the brief's, so
    `cover_letter_violations` holds it; this checks the role line."""
    violations: list[str] = []
    blocks = _blocks(full_version)

    # L1: dashes, in both versions.
    for name, text in (("full_version", full_version), ("short_version", short_version)):
        marks = dash_marks(text)
        if marks:
            violations.append(f"dash as punctuation in {name}: {describe(marks)}")

    # L2: hedging.
    hedges = [hedge for hedge in HEDGES if _phrase(hedge).search(full_version)]
    if hedges:
        violations.append(f"hedging: {', '.join(hedges)}")

    # L3: length.
    words = len(full_version.split())
    if words > 220:
        violations.append(f"over the word limit: {words} words, at most 220")

    # L11 (role line), then L4 on the block after it.
    role_line = blocks[0] if blocks else ""
    if (
        not role_line
        or "\n" in role_line
        or len(role_line.split()) > ROLE_LINE_MAX_WORDS
        or role_line.rstrip()[-1:] in (".", "!", "?")
    ):
        violations.append(
            "missing role line: the first block names the role and company in one line"
        )
    hook = blocks[1] if len(blocks) > 1 else ""
    openers = [*BANNED_OPENERS, context.company, context.role]
    opener = next((prefix for prefix in openers if _starts_with(hook, prefix)), None)
    if opener is not None:
        violations.append(f"banned opener: the hook begins with {opener!r}")
    first_sentence = _sentences(hook)[0] if hook else ""
    if not number_tokens(first_sentence):
        violations.append("the hook's first sentence has no number: open with a quantified result")

    # L5: banned anywhere.
    banned = [phrase for phrase in BANNED_ANYWHERE if _phrase(phrase).search(full_version)]
    if banned:
        violations.append(f"banned phrase: {', '.join(banned)}")

    # L6: the close.
    close = blocks[-1] if blocks else ""
    if len(_sentences(close)) > 1:
        violations.append("the close is more than one sentence")
    if "?" in close:
        violations.append("the close asks a question")

    # L7: restatement, role line aside.
    body = "\n\n".join(blocks[1:])
    for run in _shared_runs(body, context.posting, context.evidence):
        violations.append(f"restates the posting: {run}")

    # L8: numbers, with the company's and role's own numbers exempt as in C5.
    known = {token.value for token in number_tokens(context.evidence)}
    exempt = {token.value for token in number_tokens(f"{context.company} {context.role}")}
    unbacked = [
        token.raw
        for token in number_tokens(full_version)
        if token.value not in known and token.value not in exempt
    ]
    if unbacked:
        violations.append(f"number not in the evidence: {', '.join(dict.fromkeys(unbacked))}")

    # L9: experience.
    years = context.professional_years
    if years is not None:
        larger = [count for count in year_counts(full_version) if count > years]
        if larger:
            violations.append(
                f"states {max(larger)} years of experience; the record holds"
                f" {context.professional_years}"
            )
    if re.search(r"\bdecades?\b", full_version, re.IGNORECASE):
        violations.append("counts experience in decades")

    # L10: an entity that is not registered is not "my" entity.
    for entity in ENTITY_TYPES:
        if _phrase(f"my {entity}").search(full_version) and not _registered(
            entity, context.evidence
        ):
            violations.append(
                f"calls an entity the evidence does not show registered 'my {entity}'"
            )

    return violations
