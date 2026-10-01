"""Claim citations and mechanical invention checks for letters and answers
(spec 065).

Spec 034 declined to match letter prose against the truth document: it
either rejects paraphrase or verifies nothing. This module checks something
else. The model declares each factual sentence as a claim with verbatim
evidence, and the citations are checked with the spec 034 predicate. The
remaining rules look for narrow, mechanical signs of invention that do not
depend on the model declaring anything: numbers, technology names,
enforcement words and placeholders.

A factual sentence the model does not declare, with none of those signs,
passes. That is the stated limit of this gate, not an oversight.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harrier.resume.content import SkillVocabulary, TruthSources

CLAIM_SUBJECTS = ("candidate", "employer")


class ClaimCheckError(ValueError):
    """Generated text failed one or more rules. Every violation is listed,
    not only the first, so one retry can address all of them."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = list(violations)
        super().__init__("generated text failed its checks: " + "; ".join(self.violations))


class NeedsInputError(Exception):
    """A draft still holds a placeholder only the operator can fill.

    Deliberately not a ValueError: the CLI reports it as its own outcome
    (exit 3) rather than as a generation failure.
    """

    def __init__(self, markdown_path: Path, placeholders: Sequence[str]) -> None:
        self.markdown_path = markdown_path
        self.placeholders = list(placeholders)
        super().__init__(f"{len(self.placeholders)} placeholder(s) need input: {markdown_path}")


@dataclass(frozen=True)
class Claim:
    sentence: str
    about: str
    evidence: tuple[str, ...]


def parse_claims(raw: object) -> list[Claim]:
    """The model's `claims` list. Missing or malformed is a parse failure,
    like a missing letter field."""
    if not isinstance(raw, list):
        raise ValueError("AI response did not contain a claims list")
    claims: list[Claim] = []
    for index, item in enumerate(cast("list[object]", raw), start=1):
        if not isinstance(item, dict):
            raise ValueError(f"claim {index} is not an object")
        entry = cast("dict[str, object]", item)
        sentence = str(entry.get("sentence", "")).strip()
        about = str(entry.get("about", "")).strip()
        evidence_raw = entry.get("evidence")
        if not sentence:
            raise ValueError(f"claim {index} has no sentence")
        if about not in CLAIM_SUBJECTS:
            raise ValueError(f"claim {index} is about {about!r}, not candidate or employer")
        if not isinstance(evidence_raw, list):
            raise ValueError(f"claim {index} has no evidence list")
        evidence = tuple(
            str(fragment).strip()
            for fragment in cast("list[object]", evidence_raw)
            if str(fragment).strip()
        )
        if not evidence:
            raise ValueError(f"claim {index} has no evidence")
        claims.append(Claim(sentence=sentence, about=about, evidence=evidence))
    return claims


def _norm(text: str) -> str:
    """Case-insensitive, whitespace collapsed, curly apostrophes straightened,
    one trailing period dropped (C1, C3)."""
    value = " ".join(text.replace("\u2019", "'").split()).casefold()
    return value.rstrip(".")


def _word_pattern(term: str) -> re.Pattern[str]:
    """A term on word boundaries. Two-letter terms such as `Go` or `TS` match
    case-sensitively, or every "go" in a letter would read as a skill."""
    flags = 0 if len(term.strip()) <= 2 else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(term.strip())}(?!\w)", flags)


def banned_hits(phrases: Iterable[str], text: str) -> list[str]:
    """Banned phrases on word boundaries (rule N1). Substring deletion turned
    "I leveraged caching" into "I d caching"; matching on boundaries means a
    longer word is left alone, and a real hit refuses instead of being cut."""
    return [phrase for phrase in phrases if phrase.strip() and _word_pattern(phrase).search(text)]


# --- C10: placeholders --------------------------------------------------------

_TODO = re.compile(r"\[\[TODO:[^\]]*\]\]")
_BRACKETED = re.compile(r"\[(?:insert|todo|placeholder|example|your)\b[^\]\[]*\]", re.IGNORECASE)


def find_placeholders(text: str) -> list[str]:
    """Unfilled placeholders, in order, each once (rule C10)."""
    found = _TODO.findall(text)
    found.extend(_BRACKETED.findall(_TODO.sub(" ", text)))
    return list(dict.fromkeys(found))


def _without_placeholders(text: str) -> str:
    return _BRACKETED.sub(" ", _TODO.sub(" ", text))


# --- C5 and C6: numbers -------------------------------------------------------

_SURROUNDING = "()[]{}\"'.,;:!?+~*\u201c\u201d\u2018\u2019"
_NUMBER = re.compile(r"^[€$£]?(\d+(?:[.,]\d+)*)(k|m|x|%|/(?:day|week|month|year))?$", re.IGNORECASE)
_RATE_WORDS = frozenset({"per", "each", "every", "daily", "weekly", "monthly", "annually"})
_PERIODS = frozenset({"day", "week", "month", "year"})
_SENTENCE_END = re.compile(r"[.!?][\"')\]\u201d\u2019]*$")


@dataclass(frozen=True)
class NumberToken:
    raw: str
    value: str
    rate: bool


def _follows_rate(following: list[str]) -> bool:
    window = following[:4]
    if any(word in _RATE_WORDS for word in window[:3]):
        return True
    return any(
        window[index] == "a" and index + 1 < len(window) and window[index + 1] in _PERIODS
        for index in range(min(3, len(window)))
    )


def number_tokens(text: str) -> list[NumberToken]:
    """Standalone numbers, with whether each carries a rate (rules C5, C6).

    Words mixing digits and letters (`S3`, `OAuth2`) and slash pairs (`24/7`)
    are not numbers. The rate window stops at the end of the sentence, so
    "over 2025. Every week" does not make 2025 a rate.
    """
    words = text.split()
    tokens: list[NumberToken] = []
    for index, word in enumerate(words):
        match = _NUMBER.match(word.strip(_SURROUNDING))
        if not match:
            continue
        digits, suffix = match.group(1), (match.group(2) or "").lower()
        following: list[str] = []
        if not _SENTENCE_END.search(word):
            for later in words[index + 1 : index + 5]:
                following.append(later.strip(_SURROUNDING).casefold())
                if _SENTENCE_END.search(later):
                    break
        rate = suffix == "%" or suffix.startswith("/") or _follows_rate(following)
        value = digits.replace(",", "") + (suffix if suffix in ("k", "m", "x") else "")
        tokens.append(NumberToken(raw=word.strip(_SURROUNDING), value=value, rate=rate))
    return tokens


# --- the check -----------------------------------------------------------------

_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|my|me)\b", re.IGNORECASE)
_MARKER = re.compile(r"\b(?:synthetic|demo)\b", re.IGNORECASE)
_ENFORCEMENT = re.compile(r"\benforce(?:d|s|ment)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ClaimContext:
    """What a check reads besides the generated text itself."""

    sources: TruthSources
    posting: str
    company: str
    role: str
    vocabulary: SkillVocabulary

    def evidence_lines(self, claim: Claim, fragment: str) -> list[str]:
        """The lines a fragment was quoted from: the truth documents for a
        candidate claim, the posting for an employer claim."""
        if claim.about == "candidate":
            return self.sources.lines_containing(fragment)
        wanted = _norm(fragment)
        return [line for line in self.posting.splitlines() if wanted and wanted in _norm(line)]


def check_claims(texts: Sequence[str], claims: Sequence[Claim], context: ClaimContext) -> list[str]:
    """Every violation of rules C1 to C9 in one generated unit (a letter, or
    one answer with its notes). An empty list means the unit passes."""
    output = "\n\n".join(texts)
    checked = _without_placeholders(output)
    output_norm = _norm(output)
    violations: list[str] = []

    for claim in claims:
        if _norm(claim.sentence) not in output_norm:
            violations.append(f"claim sentence not in output: {claim.sentence}")
        if claim.about == "employer" and _FIRST_PERSON.search(_norm(claim.sentence)):
            violations.append(f"first-person sentence cited to the employer: {claim.sentence}")
        for fragment in claim.evidence:
            if claim.about == "candidate" and not context.sources.contains(fragment):
                violations.append(f"unverified evidence: {fragment}")
            if claim.about == "employer" and _norm(fragment) not in _norm(context.posting):
                violations.append(f"employer evidence not in posting: {fragment}")
        lines = [line for f in claim.evidence for line in context.evidence_lines(claim, f)]
        if any(_MARKER.search(line) for line in lines) and not _MARKER.search(claim.sentence):
            violations.append(f"synthetic evidence not labelled: {claim.sentence}")

    violations.extend(_number_violations(checked, claims, context))
    violations.extend(_skill_violations(checked, context))
    violations.extend(_enforcement_violations(checked, claims))
    return list(dict.fromkeys(violations))


def _number_violations(output: str, claims: Sequence[Claim], context: ClaimContext) -> list[str]:
    exempt = {token.value for token in number_tokens(f"{context.company} {context.role}")}
    violations: list[str] = []
    for token in number_tokens(output):
        if token.value in exempt:
            continue
        rates: set[bool] = set()
        cited = False
        for claim in claims:
            if token.value not in {t.value for t in number_tokens(claim.sentence)}:
                continue
            for fragment in claim.evidence:
                if token.value not in {t.value for t in number_tokens(fragment)}:
                    continue
                cited = True
                # The rate is read from the line the fragment was cut from,
                # so quoting "1,200 invoices" out of "1,200 invoices a month"
                # does not lose the rate.
                contexts = context.evidence_lines(claim, fragment) or [fragment]
                for line in contexts:
                    rates.update(t.rate for t in number_tokens(line) if t.value == token.value)
        if not cited:
            violations.append(f"number without evidence: {token.raw}")
        elif token.rate not in rates:
            violations.append(f"number changed scope: {token.raw}")
    return violations


def _skill_violations(output: str, context: ClaimContext) -> list[str]:
    violations: list[str] = []
    for term in context.vocabulary.terms:
        pattern = _word_pattern(term)
        if not pattern.search(output) or pattern.search(context.role):
            continue
        if context.vocabulary.is_verified(term) or context.sources.contains(term):
            continue
        violations.append(f"unverified skill: {term}")
    return violations


def _enforcement_violations(output: str, claims: Sequence[Claim]) -> list[str]:
    violations: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(output):
        if not _ENFORCEMENT.search(sentence):
            continue
        wanted = _norm(sentence)
        backed = any(
            (_norm(claim.sentence) in wanted or wanted in _norm(claim.sentence))
            and any(_ENFORCEMENT.search(fragment) for fragment in claim.evidence)
            for claim in claims
        )
        if not backed:
            violations.append(f"enforcement claimed without evidence: {sentence.strip()}")
    return violations
