"""The application brief: what the operator knows about one application
that no truth document holds (spec 066).

One JSON document per tracker job, in `profile_documents` with kind
`application_brief` and the job id as its name, written only through
`put_document`. It holds personal data (client names, the operator's views,
a salary number), so it lives in the database and never in git (ADR-008).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import cast

from harrier.profile.store import get_document, put_document
from harrier.resume.content import (
    ResumeBundleError,
    TruthSources,
    load_bundle,
    load_truth_sources,
)
from harrier.tracks import default_scope

APPLICATION_BRIEF_KIND = "application_brief"

_TOP_KEYS = frozenset(
    {
        "never_name",
        "guidance_url",
        "employer_guidance",
        "letter",
        "answers",
        "evidence",
        "views",
        "compensation_number",
        "confirmed_skills",
    }
)
_LETTER_KEYS = frozenset({"max_words", "max_sentences", "paragraphs"})
_ANSWER_KEYS = frozenset({"max_words", "max_sentences"})


class BriefError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_words: int | None = None
    max_sentences: int | None = None
    paragraphs: int | None = None


@dataclass(frozen=True)
class Brief:
    never_name: tuple[str, ...] = ()
    guidance_url: str = ""
    employer_guidance: str = ""
    letter: Limits = Limits()
    answers: Limits = Limits()
    evidence: tuple[str, ...] = ()
    views: dict[str, str] = field(default_factory=dict[str, str])
    compensation_number: str = ""
    # Skills the candidate confirmed for this application in answer to the
    # fit evaluation's questions (spec 071 O9).
    confirmed_skills: tuple[str, ...] = ()

    def view_for(self, question: str) -> str | None:
        wanted = _question_key(question)
        for asked, view in self.views.items():
            if _question_key(asked) == wanted:
                return view
        return None


EMPTY_BRIEF = Brief()


def _question_key(question: str) -> str:
    return " ".join(question.casefold().split()).rstrip("?").strip()


def _strings(raw: object, key: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(item, str) for item in cast("list[object]", raw)
    ):
        raise BriefError(f"{key} must be a list of strings")
    return tuple(item.strip() for item in cast("list[str]", raw) if item.strip())


def _text(raw: object, key: str) -> str:
    if not isinstance(raw, str):
        raise BriefError(f"{key} must be a string")
    return raw.strip()


def _limits(raw: object, key: str, allowed: frozenset[str]) -> Limits:
    if not isinstance(raw, dict):
        raise BriefError(f"{key} must be an object")
    entry = cast("dict[str, object]", raw)
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise BriefError(f"{key} has unknown keys: {', '.join(unknown)}")
    values: dict[str, int] = {}
    for name, value in entry.items():
        # bool is an int in Python; `true` is not a word limit.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise BriefError(f"{key}.{name} must be a positive integer")
        values[name] = value
    return Limits(**values)


def parse_brief(raw: object) -> Brief:
    """Every key optional, unknown keys and wrong types refused."""
    if not isinstance(raw, dict):
        raise BriefError("a brief must be a JSON object")
    data = cast("dict[str, object]", raw)
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise BriefError(f"unknown brief keys: {', '.join(unknown)}")
    views: dict[str, str] = {}
    if "views" in data:
        raw_views = data["views"]
        if not isinstance(raw_views, dict) or not all(
            isinstance(value, str) for value in cast("dict[str, object]", raw_views).values()
        ):
            raise BriefError("views must map each question to a string")
        views = {str(k): str(v).strip() for k, v in cast("dict[str, str]", raw_views).items()}
    return Brief(
        never_name=_strings(data["never_name"], "never_name") if "never_name" in data else (),
        guidance_url=_text(data["guidance_url"], "guidance_url") if "guidance_url" in data else "",
        employer_guidance=(
            _text(data["employer_guidance"], "employer_guidance")
            if "employer_guidance" in data
            else ""
        ),
        letter=_limits(data["letter"], "letter", _LETTER_KEYS) if "letter" in data else Limits(),
        answers=(
            _limits(data["answers"], "answers", _ANSWER_KEYS) if "answers" in data else Limits()
        ),
        evidence=_strings(data["evidence"], "evidence") if "evidence" in data else (),
        views=views,
        compensation_number=(
            _text(data["compensation_number"], "compensation_number")
            if "compensation_number" in data
            else ""
        ),
        confirmed_skills=(
            _strings(data["confirmed_skills"], "confirmed_skills")
            if "confirmed_skills" in data
            else ()
        ),
    )


def store_brief(conn: sqlite3.Connection, job_id: int, text: str) -> Brief:
    """Validate, then store. Nothing is written when validation fails."""
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BriefError(f"brief is not valid JSON: {exc}") from exc
    brief = parse_brief(raw)
    _check_confirmed_skills(conn, brief.confirmed_skills)
    put_document(
        conn,
        APPLICATION_BRIEF_KIND,
        str(job_id),
        "json",
        json.dumps(raw, ensure_ascii=False, indent=2),
    )
    return brief


def _named_in(sources: TruthSources, skill: str) -> bool:
    """A whole-word mention in a truth line, so "Go" is not found in "good"."""
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(skill)}(?![A-Za-z0-9])", flags=re.IGNORECASE)
    return any(pattern.search(line) for line in sources.lines_containing(skill))


def _check_confirmed_skills(conn: sqlite3.Connection, skills: tuple[str, ...]) -> None:
    """A confirmed skill must already exist somewhere the candidate wrote it:
    the bundle's `all_skills` or a line of the truth documents (spec 071 O9).
    Confirming is permission to show it, not a new claim."""
    if not skills:
        return
    try:
        # `brief set` runs on the default track only (spec 093).
        bundle = load_bundle(conn, default_scope(conn))
        sources = load_truth_sources(conn)
    except ResumeBundleError as exc:
        raise BriefError(f"confirmed_skills cannot be checked: {exc}") from exc
    unknown = [
        skill
        for skill in skills
        if skill not in bundle.all_skills and not _named_in(sources, skill)
    ]
    if unknown:
        raise BriefError(
            "confirmed_skills not found in all_skills or the truth documents: " + ", ".join(unknown)
        )


def brief_text(conn: sqlite3.Connection, job_id: int) -> str | None:
    return get_document(conn, APPLICATION_BRIEF_KIND, str(job_id))


def load_brief(conn: sqlite3.Connection, job_id: int) -> Brief:
    """No brief is the empty brief. A stored brief that no longer parses is
    refused, so a damaged never-name list cannot switch itself off."""
    content = brief_text(conn, job_id)
    if content is None:
        return EMPTY_BRIEF
    try:
        raw: object = json.loads(content)
    except json.JSONDecodeError as exc:
        raise BriefError(f"stored brief for job {job_id} is not valid JSON: {exc}") from exc
    return parse_brief(raw)


def with_operator_evidence(sources: TruthSources, *lines: str) -> TruthSources:
    """Brief evidence and views count as candidate evidence for this job only
    (B4, B7). They go under their own heading so a disclaimer section at the
    end of the achievements document cannot swallow them."""
    kept = [line for line in lines if line.strip()]
    if not kept:
        return sources
    block = "\n\n## Operator evidence for this application\n\n" + "\n".join(kept) + "\n"
    return TruthSources(
        truth_text=sources.truth_text, achievements_text=sources.achievements_text + block
    )


def never_name_hits(names: tuple[str, ...], text: str) -> list[str]:
    """Names that must not appear, case-insensitive on word boundaries (B1)."""
    return [
        name
        for name in names
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, flags=re.IGNORECASE)
    ]


_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


def count_sentences(text: str) -> int:
    """A sentence ends at `.`, `!` or `?` followed by whitespace and an
    uppercase letter, or at the end of the text, so "e.g. the" and "v1.2" do
    not split (B2)."""
    stripped = text.strip()
    if not stripped:
        return 0
    return len([part for part in _SENTENCE_BREAK.split(stripped) if part.strip()])


def limit_violations(text: str, limits: Limits, surface: str, where: str) -> list[str]:
    """Stated word and sentence limits for one field (B2). Paragraphs are a
    letter-only shape and are checked with the letter's other shape rules."""
    violations: list[str] = []
    words = len(text.split())
    if limits.max_words is not None and words > limits.max_words:
        violations.append(
            f"over the stated limit: {surface}.max_words {limits.max_words}, {where} has {words}"
        )
    sentences = count_sentences(text)
    if limits.max_sentences is not None and sentences > limits.max_sentences:
        violations.append(
            f"over the stated limit: {surface}.max_sentences {limits.max_sentences},"
            f" {where} has {sentences}"
        )
    return violations


def brief_instructions(brief: Brief, surface: str) -> str:
    """The brief, as instructions appended to the system prompt. Empty when
    there is no brief, so a job without one gets spec 065's prompt."""
    lines: list[str] = []
    if brief.never_name:
        lines.append(
            "- Never write these names; describe the work instead: " + "; ".join(brief.never_name)
        )
    if brief.employer_guidance:
        lines.append(
            "- Follow the employer's own guidance in employer_guidance. It also counts as "
            "employer evidence."
        )
    limits = brief.letter if surface == "letter" else brief.answers
    stated = [
        f"{name} {value}"
        for name, value in (
            ("max_words", limits.max_words),
            ("max_sentences", limits.max_sentences),
            ("paragraphs", limits.paragraphs),
        )
        if value is not None
    ]
    if stated:
        lines.append(
            "- Stated limits, which replace any default above: "
            + ", ".join(stated)
            + (" (each short and medium answer)" if surface == "answers" else "")
        )
    if brief.evidence:
        lines.append(
            "- operator_evidence holds facts the operator supplied for this application. "
            "Quote them as candidate evidence."
        )
    if surface == "answers" and brief.views:
        lines.append(
            "- operator_views holds the operator's own views. Use them for opinion questions "
            "and quote them as candidate evidence."
        )
    if not lines:
        return ""
    return "\nThis application's brief:\n" + "\n".join(lines) + "\n"
