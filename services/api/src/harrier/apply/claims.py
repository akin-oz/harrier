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

from harrier.resume.content import SkillVocabulary, TruthSources, strip_inline_markup

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


RETRY_INSTRUCTION = (
    "Your previous response was refused by the checks listed in refusals. Return a"
    " complete new response in the same format that fixes every one of them. Every"
    " claim sentence must appear word for word in the text."
)


def retry_payload(
    payload: dict[str, object], refusal: ClaimCheckError, previous_response: str
) -> dict[str, object]:
    """The first request's payload with what refused it added (spec 085, R2).

    The refusals and the raw response only quote what the model itself
    returned, so the retry sends the provider nothing it did not produce.
    """
    return {
        **payload,
        "retry": {
            "instruction": RETRY_INSTRUCTION,
            "refusals": list(refusal.violations),
            "previous_response": previous_response,
        },
    }


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
    one trailing period dropped (C1, C3). Inline code and paired emphasis
    markers are ignored (spec 068)."""
    value = strip_inline_markup(text.replace("\u2019", "'")).casefold()
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

# Backticks and underscores are markup around a number, not part of it, so
# `12` and _12_ are read as 12 (spec 113).
_SURROUNDING = "()[]{}\"'.,;:!?+~*`_\u201c\u201d\u2018\u2019"
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
    # The application profile, consulted only to name the source of
    # candidate evidence that failed C2 (spec 069). It never verifies.
    profile: str = ""

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
                violations.append(_unverified(fragment, context))
            if claim.about == "employer" and _norm(fragment) not in _norm(context.posting):
                violations.append(f"employer evidence not in posting: {fragment}")
        lines = [line for f in claim.evidence for line in context.evidence_lines(claim, f)]
        if any(_MARKER.search(line) for line in lines) and not _MARKER.search(claim.sentence):
            violations.append(f"synthetic evidence not labelled: {claim.sentence}")

    violations.extend(_number_violations(checked, claims, context, versions_in=output))
    violations.extend(_skill_violations(checked, context))
    violations.extend(_enforcement_violations(checked, claims))
    return list(dict.fromkeys(violations))


def _unverified(fragment: str, context: ClaimContext) -> str:
    """The C2 refusal, naming where the fragment was found when that was
    somewhere other than the truth sources (spec 069). The refusal itself is
    the same whichever message it carries."""
    wanted = _norm(fragment)
    if wanted and wanted in _norm(context.posting):
        return f"posting text cited as candidate evidence: {fragment}"
    if wanted and wanted in _norm(context.profile):
        return f"application profile cited as candidate evidence: {fragment}"
    return f"unverified evidence: {fragment}"


def _token_sentences(text: str) -> list[str]:
    """Where each of `number_tokens(text)` sits, in the same order, as the
    text a refusal adds after the token (spec 086, S2 and S3).

    The text is split at every line break, then at `_SENTENCE_SPLIT`. Every
    split point is whitespace, so each word of the text lands in exactly one
    piece, and the words of the pieces in order are the words of the text.
    """
    places: list[tuple[list[str], int]] = []
    for line in text.splitlines():
        for piece in _SENTENCE_SPLIT.split(line):
            words = piece.split()
            places.extend((words, position) for position in range(len(words)))
    found: list[str] = []
    for (words, position), word in zip(places, text.split(), strict=True):
        if not _NUMBER.match(word.strip(_SURROUNDING)):
            continue
        sentence = " ".join(words)
        value = next(t.value for t in number_tokens(word))
        if sum(t.value == value for t in number_tokens(sentence)) < 2:
            found.append(f"(in: {sentence})")
        elif position == 0:
            found.append(f"(first word of: {sentence})")
        else:
            found.append(f'(after "{words[position - 1].strip(_SURROUNDING)}" in: {sentence})')
    return found


# --- spec 087: versions the truth sources state ---------------------------------

_VERSION_SHAPE = re.compile(r"^[(\[\"\u201c]*\d{1,3}(?:\.\d+)*[)\]\"'\u201d\u2019.,;:!?]*$")
_DURATIONS = frozenset(
    {"year", "years", "yr", "yrs", "month", "months", "week", "weeks", "day", "days"}
    | {"hour", "hours", "time", "times"}
)
_PHRASE_END = tuple(",.;:!?)]\"'\u201d\u2019")
_PHRASE_JOINS = frozenset({"and", "or", "with", "in", "on", "to", "for"})
_PLACEHOLDER_WORD = "[[placeholder]]"


def _naming_term(before: str, terms: Sequence[str]) -> str | None:
    """The text of the longest vocabulary term that ends `before`, the words
    in front of a number (V1.2). The term starts the line or follows
    whitespace, an opening bracket or a double quote, so `non-Kafka` and
    `my.kafka` do not end with `Kafka`. Short terms match case-sensitively,
    as C8's do."""
    named: str | None = None
    for term in terms:
        wanted = " ".join(term.split())
        if not wanted:
            continue
        flags = 0 if len(wanted) <= 2 else re.IGNORECASE
        match = re.search(rf"(?:^|(?<=[\s(\[{{\"\u201c])){re.escape(wanted)}$", before, flags)
        if match and (named is None or len(match.group(0)) > len(named)):
            named = match.group(0)
    return named


def _version_at(
    words: Sequence[str], at: int, rate: bool, terms: Sequence[str], *, output: bool
) -> tuple[str, str] | None:
    """The naming term and `NumberToken.raw` of the number at `words[at]`,
    when it is a version occurrence (V1). `rate` is what `number_tokens`
    read for it over this line alone (V1.4). Only the output has to end the
    phrase after the number (V1.5)."""
    word = words[at]
    if rate or not _VERSION_SHAPE.match(word):
        return None
    term = _naming_term(" ".join(words[:at]), terms)
    if term is None:
        return None
    following = words[at + 1].strip(_SURROUNDING).casefold() if at + 1 < len(words) else None
    if following in _DURATIONS and not _SENTENCE_END.search(word):
        return None
    ends_phrase = word.endswith(_PHRASE_END) or following is None or following in _PHRASE_JOINS
    if output and not ends_phrase:
        return None
    return term, word.strip(_SURROUNDING)


def _line_versions(
    line: str, terms: Sequence[str], *, output: bool
) -> list[tuple[str, str] | None]:
    """One entry per number token of the line, in order: its version
    occurrence, or None. The line is read with inline markup removed."""
    words = strip_inline_markup(line).split()
    numbered = [at for at, word in enumerate(words) if _NUMBER.match(word.strip(_SURROUNDING))]
    tokens = number_tokens(" ".join(words))
    return [
        _version_at(words, at, token.rate, terms, output=output)
        for at, token in zip(numbered, tokens, strict=True)
    ]


def _truth_states(version: tuple[str, str], context: ClaimContext) -> bool:
    """Whether a supporting truth line holds the same version, named by the
    same term spelled the same way (V2). A line C7's marker matches never
    does. The posting, the profile and `verified_skills` are not read."""
    term, _ = version
    return any(
        version in _line_versions(line, context.vocabulary.terms, output=False)
        for line in context.sources.lines_containing(term)
        if not _MARKER.search(line)
    )


def _grounded_versions(checked: str, output: str, context: ClaimContext) -> set[int]:
    """Indexes into `number_tokens(checked)` of the version occurrences the
    truth sources state (spec 087).

    Each token is read on its line of the output with every placeholder
    replaced by a word rather than a space, so a term before a placeholder
    never names a number after it (V0). Both texts replace the same spans,
    so their lines correspond. Within a line the tokens are matched in
    order, and only when their raw texts agree. A line where they do not,
    because removing markup made or unmade a number, exempts nothing.
    """
    marked = _BRACKETED.sub("\0", _TODO.sub("\0", output)).replace("\0", _PLACEHOLDER_WORD)
    terms = context.vocabulary.terms
    grounded: set[int] = set()
    index = 0
    for checked_line, marked_line in zip(checked.splitlines(), marked.splitlines(), strict=True):
        raws = [token.raw for token in number_tokens(checked_line)]
        marked_raws = [
            word.strip(_SURROUNDING)
            for word in strip_inline_markup(marked_line).split()
            if _NUMBER.match(word.strip(_SURROUNDING))
        ]
        if marked_raws == raws:
            versions = _line_versions(marked_line, terms, output=True)
            grounded.update(
                index + ordinal
                for ordinal, version in enumerate(versions)
                if version is not None and _truth_states(version, context)
            )
        index += len(raws)
    return grounded


def _number_violations(
    output: str, claims: Sequence[Claim], context: ClaimContext, *, versions_in: str
) -> list[str]:
    """C5 and C6 over the text with placeholders removed. `versions_in` is
    the same text before that removal, which the version exemption reads
    (spec 087)."""
    exempt = {token.value for token in number_tokens(f"{context.company} {context.role}")}
    grounded = _grounded_versions(output, versions_in, context)
    violations: list[str] = []
    tokens = number_tokens(output)
    for index, (token, where) in enumerate(zip(tokens, _token_sentences(output), strict=True)):
        if token.value in exempt or index in grounded:
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
            violations.append(f"number without evidence: {token.raw} {where}")
        elif token.rate not in rates:
            violations.append(f"number changed scope: {token.raw} {where}")
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
