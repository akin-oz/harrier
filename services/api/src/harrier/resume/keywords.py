"""The posting's skills the truth does not hold (spec 140).

A posting can ask for a technology the candidate has used and never recorded,
or one the candidate has not used. Either way the CV leaves it out, which is
correct. This module names what was left out, so the operator can record a
true skill in the truth sources or leave an untrue one alone. It writes the
names into a sidecar beside the draft and never into the CV.

Two kinds of term are named, each with the posting line that names it:

- a term in the skill vocabulary that is not verified (spec 065), and
- a term in the framing's `watch_terms` that no truth line holds as a whole
  word. "Git" is not held by a line naming only "GitHub Actions".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harrier.resume.content import ResumeBundle, TruthSources


@dataclass(frozen=True)
class MissingKeyword:
    term: str
    posting_line: str


def _word(term: str) -> re.Pattern[str]:
    """A term on word boundaries. Two-letter terms match their case, as the
    claims check matches them (`harrier.apply.claims`)."""
    flags = 0 if len(term.strip()) <= 2 else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(term.strip())}(?!\w)", flags)


def _vocabulary(bundle: ResumeBundle) -> tuple[list[str], set[str]]:
    """The bundle's skill terms and the verified ones, casefolded. A verified
    skill verifies its aliases too, as `load_skill_vocabulary` reads them."""
    verified = {name.casefold() for name in bundle.verified_skills}
    terms = list(bundle.all_skills)
    for key, names in bundle.technology_aliases.items():
        terms.extend([key, *names])
        if key.casefold() in verified or any(name.casefold() in verified for name in names):
            verified.add(key.casefold())
            verified.update(name.casefold() for name in names)
    return list(dict.fromkeys(term for term in terms if term.strip())), verified


def _posting_line(pattern: re.Pattern[str], posting: str) -> str | None:
    for line in posting.splitlines():
        if pattern.search(line):
            return line.strip()
    return None


def _truth_holds(sources: TruthSources, term: str) -> bool:
    pattern = _word(term)
    return any(pattern.search(line) for line in sources.lines_containing(term))


def missing_keywords(
    bundle: ResumeBundle, sources: TruthSources, posting: str
) -> list[MissingKeyword]:
    """Each posting term the CV leaves out because the truth does not hold
    it, once, in the order found: unverified vocabulary first, then watch
    terms."""
    terms, verified = _vocabulary(bundle)
    found: dict[str, MissingKeyword] = {}
    for term in terms:
        if term.casefold() in verified or term.casefold() in found:
            continue
        line = _posting_line(_word(term), posting)
        if line is not None:
            found[term.casefold()] = MissingKeyword(term, line)
    # A watch term is checked against the truth sources whatever the
    # vocabulary says: an alias can verify "Git" through "GitHub Actions"
    # (review of PR #343).
    for term in bundle.watch_terms:
        if term.casefold() in found:
            continue
        line = _posting_line(_word(term), posting)
        if line is not None and not _truth_holds(sources, term):
            found[term.casefold()] = MissingKeyword(term, line)
    return list(found.values())


def missing_keywords_markdown(company: str, role: str, missing: list[MissingKeyword]) -> str:
    """The sidecar's text: internal metadata, never part of the CV."""
    lines = [
        f"# Missing keywords: {company}, {role}",
        "",
        "Terms the posting names that the truth sources do not hold. None of them is",
        "on the CV. Record a true one in the truth sources with its evidence, then",
        "tailor again; leave an untrue one alone.",
        "",
    ]
    if not missing:
        lines.append("None.")
    for item in missing:
        lines.append(f'- **{item.term}**: "{item.posting_line}"')
    return "\n".join(lines) + "\n"
