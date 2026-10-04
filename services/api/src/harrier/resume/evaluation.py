"""Requirement-grounded JD fit evaluation (spec 070).

The posting is read by section: requirements and responsibilities become
evidence matrix rows, benefits are listed apart, and company context is
dropped (X1 to X4). Each row is rated on the bullets that name what it
asks for, not on the dimension it falls under (R1 to R9): a bullet in the
right area that does not name the technology or concept is Adjacent at
most. Every row that is not Direct is a gap, and every gap is a question
for the candidate (G1 to G5). The JD never supplies a candidate fact.
Proof: tests/test_resume_evaluation.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import cast

from harrier.apply.requirements import requirement_kind
from harrier.resume.content import EvaluationDimension, ResumeBundle
from harrier.resume.facts import professional_experience_label, professional_experience_years
from harrier.resume.vocabulary import (
    COMPANY_CONTEXT_PATTERNS,
    CONCEPTS,
    FAMILIES,
    NICE_TO_HAVE_HEADERS,
    SECTION_HEADERS,
    SENIORITY_LEVELS,
    TECHNOLOGIES,
)

EVIDENCE_STATUSES = ("Direct", "Partial", "Adjacent", "Unsupported")
JD_IMPORTANCE = ("core", "important", "nice-to-have")
ITEM_CLASSES = ("requirement", "responsibility", "company_context", "benefit")
CONFIDENCE = {"Direct": "high", "Partial": "medium", "Adjacent": "low", "Unsupported": "low"}
GAP_ACTION = "Do not claim this. Ask the candidate (section 6)."

_MAX_CITED = 3
_BOUNDARY_BEFORE = r"(?<![A-Za-z0-9])"
_BOUNDARY_AFTER = r"(?![A-Za-z0-9])"


# --- posting structure (X1 to X3) ---


@dataclass(frozen=True)
class PostingItem:
    text: str
    item_class: str
    section_position: int
    nice_to_have_section: bool = False
    in_first_requirement_section: bool = False


def _header_kind(line: str) -> tuple[str, bool] | None:
    """The section kind a line opens, and whether it marks nice-to-have."""
    stripped = re.sub(r"^\s*#+\s*", "", line).strip().rstrip(":").strip()
    if not stripped or len(stripped.split()) > 5 or re.search(r"[.!?]$", stripped):
        return None
    # Postings mix straight and typographic apostrophes.
    lower = stripped.lower().replace("\u2019", "'")
    nice = any(term in lower for term in NICE_TO_HAVE_HEADERS)
    for kind, phrases in SECTION_HEADERS.items():
        if lower in phrases:
            return kind, nice
    if lower.startswith("about ") and lower not in ("about you", "about the role"):
        return "company_context", False
    return None


def _units(line: str) -> list[str]:
    units: list[str] = []
    for raw in re.split(r"(?<=[.!?])\s+", line):
        unit = re.sub(r"^\s*[-*•\d.)]+\s+", "", raw).strip()
        unit = re.sub(r"^\s*[-*•]\s*", "", unit).strip()
        if unit and len(unit) >= 12:
            units.append(unit)
    return units


def classify_posting(jd_text: str) -> list[PostingItem]:
    """Every readable unit of the posting with its class (spec 070 X1 to X3).

    Header lines are never items. Text before the first recognized header
    is company context; a posting with no recognized header at all is read
    as requirements, apart from invitations and legal notices.
    """
    lines = (jd_text or "").splitlines()
    has_headers = any(_header_kind(line) for line in lines)
    current = "company_context" if has_headers else "requirement"
    nice = False
    position = 0
    requirement_sections_seen = 0
    first_requirement_section = False
    items: list[PostingItem] = []
    for line in lines:
        header = _header_kind(line)
        if header is not None:
            current, nice = header
            position = 0
            if current == "requirement":
                requirement_sections_seen += 1
                first_requirement_section = requirement_sections_seen == 1
            else:
                first_requirement_section = False
            continue
        # An invitation or legal notice is context, and so is the rest of its
        # paragraph: the sentences after "equal opportunity employer" belong
        # to the notice, while the duties before "let's talk" stay duties.
        in_context = False
        for unit in _units(line):
            in_context = in_context or any(
                pattern.search(unit) for pattern in COMPANY_CONTEXT_PATTERNS
            )
            item_class = "company_context" if in_context else current
            items.append(
                PostingItem(
                    text=unit,
                    item_class=item_class,
                    section_position=position,
                    nice_to_have_section=nice,
                    in_first_requirement_section=(first_requirement_section or not has_headers)
                    and current == "requirement",
                )
            )
            position += 1
    return items


# --- term detection ---


@dataclass(frozen=True)
class Term:
    """A technology or concept a requirement names, by canonical label."""

    label: str
    kind: str  # "technology" or "concept"


def _alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(_BOUNDARY_BEFORE + re.escape(alias) + _BOUNDARY_AFTER, re.IGNORECASE)


@dataclass
class Vocabulary:
    """Technologies (posting vocabulary merged with the bundle's aliases),
    families, and concept terms with the dimensions that own them."""

    technologies: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    families: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    concepts: dict[str, list[EvaluationDimension]] = field(
        default_factory=dict[str, list[EvaluationDimension]]
    )

    def spans(self, text: str) -> list[tuple[int, int, Term]]:
        """Non-overlapping term mentions, longest alias first, so "AWS
        Lambda" is one mention and never also "AWS"."""
        candidates: list[tuple[int, int, Term]] = []
        for label, aliases in self.technologies.items():
            for alias in aliases:
                for match in _alias_pattern(alias).finditer(text):
                    candidates.append((match.start(), match.end(), Term(label, "technology")))
        for concept in self.concepts:
            for match in _alias_pattern(concept).finditer(text):
                candidates.append((match.start(), match.end(), Term(concept, "concept")))
        candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0]))
        taken: list[tuple[int, int, Term]] = []
        for start, end, term in candidates:
            if all(end <= other_start or start >= other_end for other_start, other_end, _ in taken):
                taken.append((start, end, term))
        return sorted(taken, key=lambda item: item[0])

    def terms(self, text: str) -> list[Term]:
        return list(dict.fromkeys(term for _, _, term in self.spans(text)))

    def family_of(self, label: str) -> str | None:
        return next((family for family, members in self.families.items() if label in members), None)


def build_vocabulary(bundle: ResumeBundle) -> Vocabulary:
    vocabulary = Vocabulary()
    for label, aliases in TECHNOLOGIES.items():
        vocabulary.technologies[label] = {alias.lower() for alias in aliases}
    for label, aliases in bundle.technology_aliases.items():
        own = {alias.lower() for alias in aliases} | {label.lower()}
        # A bundle technology sharing an alias with a vocabulary entry is that
        # technology under the candidate's own label ("Vue 3" is Vue).
        same = next(
            (
                existing
                for existing, existing_aliases in vocabulary.technologies.items()
                if existing.lower() == label.lower() or existing_aliases & own
            ),
            None,
        )
        if same is None:
            vocabulary.technologies[label] = own
        else:
            vocabulary.technologies[same] |= own
    vocabulary.families = dict(FAMILIES)
    for concept in CONCEPTS:
        vocabulary.concepts.setdefault(concept, [])
    for dimension in bundle.evaluation_dimensions:
        for signal in dimension.signals:
            vocabulary.concepts.setdefault(signal.lower(), []).append(dimension)
    return vocabulary


# --- requirement extraction (X4 to X7) ---


_IMPORTANCE_CORE = re.compile(r"\b(?:must|required|expert|essential)\b", re.IGNORECASE)
_IMPORTANCE_NICE = re.compile(
    r"\b(?:nice to have|nice-to-have|bonus|plus|preferred)\b", re.IGNORECASE
)
_LIST_GAP = re.compile(r"^[\s,/+&]*(?:\b(?:and|or)\b)?[\s,/+&]*$", re.IGNORECASE)
_YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*years?\b", re.IGNORECASE)
_COMPENSATION = re.compile(r"\b(?:salary|compensation)\b", re.IGNORECASE)


def _importance(item: PostingItem) -> str:
    if _IMPORTANCE_NICE.search(item.text) or item.nice_to_have_section:
        return "nice-to-have"
    if _IMPORTANCE_CORE.search(item.text):
        return "core"
    if item.item_class == "requirement" and item.in_first_requirement_section:
        return "core" if item.section_position < 3 else "important"
    return "important"


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".;:!").strip().lower()


def _split_compound(unit: str, vocabulary: Vocabulary) -> list[tuple[str, str]]:
    """A unit naming a list of two or more technologies becomes one row per
    technology, plus the text left over when it still names a concept
    (spec 070 X5). Returns (text, source) pairs; source is "" when the unit
    was not split."""
    technology_spans = [span for span in vocabulary.spans(unit) if span[2].kind == "technology"]
    groups: list[list[tuple[int, int, Term]]] = []
    for span in technology_spans:
        if groups and _LIST_GAP.match(unit[groups[-1][-1][1] : span[0]]):
            groups[-1].append(span)
        else:
            groups.append([span])
    group = next((candidate for candidate in groups if len(candidate) >= 2), None)
    if group is None:
        return [(unit, "")]
    lead = unit[: group[0][0]].strip()
    rows = [(f"{lead} {unit[start:end]}".strip(), unit) for start, end, _ in group]
    remainder = (unit[: group[0][0]] + unit[group[-1][1] :]).strip()
    if any(term.kind == "concept" for term in vocabulary.terms(remainder)):
        rows.append((re.sub(r"\s+", " ", remainder), unit))
    return rows


def extract_jd_requirements(
    bundle: ResumeBundle, jd_text: str, role: str = ""
) -> list[dict[str, str]]:
    """One row per requirement or responsibility, deduplicated (X4 to X7).

    A compensation line naming nothing else is left to the compensation
    question (G4), so it never becomes a second, redundant gap row.
    """
    vocabulary = build_vocabulary(bundle)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in classify_posting(jd_text):
        if item.item_class not in ("requirement", "responsibility"):
            continue
        if _COMPENSATION.search(item.text) and not vocabulary.terms(item.text):
            continue
        importance = _importance(item)
        if item.item_class == "responsibility" and importance == "core":
            importance = "core" if re.search(r"\bmust\b", item.text, re.IGNORECASE) else "important"
        for text, source in _split_compound(item.text, vocabulary):
            key = _normalized(text)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "requirement": text,
                    "source": source,
                    "item_class": item.item_class,
                    "jd_importance": importance,
                }
            )
    return rows


def posting_benefits(jd_text: str) -> list[str]:
    return [item.text for item in classify_posting(jd_text) if item.item_class == "benefit"]


# --- evidence rating (R1 to R9) ---


@dataclass(frozen=True)
class Coverage:
    direct: tuple[str, ...]
    partial: tuple[str, ...]  # one readable note per partly covered term
    missing: tuple[str, ...]
    members: tuple[tuple[str, str], ...] = ()  # (family, member named instead)
    capped: tuple[str, ...] = ()  # named, but capped by its dimension kind


NO_COVERAGE = Coverage((), (), ())


def _dimensions_for(terms: list[Term], vocabulary: Vocabulary) -> list[EvaluationDimension]:
    dimensions: list[EvaluationDimension] = []
    for term in terms:
        keys = (
            vocabulary.technologies.get(term.label, set())
            if term.kind == "technology"
            else {term.label}
        )
        for key in sorted(keys):
            for dimension in vocabulary.concepts.get(key, []):
                if dimension not in dimensions:
                    dimensions.append(dimension)
    return dimensions


def _has_backend_role(bundle: ResumeBundle) -> bool:
    return any(
        "backend" in role.competencies or "full-stack" in role.competencies for role in bundle.roles
    )


def _coverage(
    bundle: ResumeBundle, vocabulary: Vocabulary, terms: list[Term], bullet: str
) -> Coverage:
    bullet_labels = {term.label for term in vocabulary.terms(bullet)}
    direct: list[str] = []
    partial: list[str] = []
    missing: list[str] = []
    members_named: list[tuple[str, str]] = []
    capped: list[str] = []
    for term in terms:
        owners = vocabulary.concepts.get(term.label, []) if term.kind == "concept" else []
        if any(owner.kind == "absent_by_default" for owner in owners):
            # R9: an absent-by-default dimension's terms are never covered.
            missing.append(term.label)
            continue
        if term.label in bullet_labels:
            if any(owner.kind == "backend_ownership" for owner in owners) and not _has_backend_role(
                bundle
            ):
                # R9: no role lists backend or full-stack, so mentioning it is
                # not ownership.
                partial.append(f"mentions {term.label}, but no role lists backend ownership")
                capped.append(term.label)
            else:
                direct.append(term.label)
            continue
        members = vocabulary.families.get(term.label, ())
        covered_members = [member for member in members if member in bullet_labels]
        if covered_members:
            partial.append(f"names {', '.join(covered_members)}, one part of {term.label}")
            members_named.append((term.label, covered_members[0]))
            continue
        missing.append(term.label)
    return Coverage(
        tuple(direct), tuple(partial), tuple(missing), tuple(members_named), tuple(capped)
    )


def _reason(coverage: Coverage, terms: list[Term]) -> str:
    kinds = {term.label: term.kind for term in terms}
    parts: list[str] = []
    named = [label for label in coverage.direct if kinds.get(label) == "technology"]
    shown = [label for label in coverage.direct if kinds.get(label) == "concept"]
    if named:
        parts.append("names " + ", ".join(named))
    if shown:
        parts.append("shows " + ", ".join(shown))
    parts.extend(coverage.partial)
    if coverage.missing and (named or shown or coverage.partial):
        parts.append("not " + ", ".join(coverage.missing))
    return "; ".join(parts)


def _database_cap(dimensions: list[EvaluationDimension], requirement: str) -> bool:
    """The database dimension's refs are API work; a database requirement
    with no API in it gets no Adjacent evidence from them (spec 013 pin)."""
    lower = requirement.lower()
    return any(dimension.kind == "database" for dimension in dimensions) and (
        "database" in lower and not any(term in lower for term in ("api", "rest", "graphql"))
    )


@dataclass(frozen=True)
class Rating:
    status: str
    evidence: list[dict[str, str]]
    terms: list[Term]
    dimensions: list[EvaluationDimension]
    coverage: Coverage  # of the best cited bullet; NO_COVERAGE when none


def _rate(
    bundle: ResumeBundle,
    vocabulary: Vocabulary,
    requirement: str,
    as_of: date | None,
) -> Rating:
    terms = vocabulary.terms(requirement)
    dimensions = _dimensions_for(terms, vocabulary)

    years = _YEARS.search(requirement)
    if years and not terms:
        # R4: a years requirement against the computed career length.
        if professional_experience_years(bundle, as_of) >= int(years.group(1)):
            label = professional_experience_label(bundle, as_of)
            reason = f"{label} of professional experience"
            entry = {"ref": "computed", "quote": "", "reason": reason}
            return Rating("Direct", [entry], terms, dimensions, NO_COVERAGE)
        return Rating("Unsupported", [], terms, dimensions, NO_COVERAGE)

    if terms:
        direct: list[tuple[int, dict[str, str], Coverage]] = []
        partial: list[tuple[int, dict[str, str], Coverage]] = []
        for ref, bullet in bundle.bullet_pool.items():
            coverage = _coverage(bundle, vocabulary, terms, bullet)
            entry = {"ref": ref, "quote": bullet, "reason": _reason(coverage, terms)}
            if coverage.direct and not coverage.missing and not coverage.partial:
                direct.append((len(coverage.direct), entry, coverage))
            elif coverage.direct or coverage.partial:
                partial.append((len(coverage.direct) + len(coverage.partial), entry, coverage))
        for status, found in (("Direct", direct), ("Partial", partial)):
            if found:
                found.sort(key=lambda item: -item[0])
                cited = [entry for _, entry, _ in found[:_MAX_CITED]]
                return Rating(status, cited, terms, dimensions, found[0][2])

    # R6: the right area, but no bullet names what the requirement asks for.
    if not _database_cap(dimensions, requirement):
        named = ", ".join(term.label for term in terms) or "this requirement"
        adjacent: list[dict[str, str]] = []
        for dimension in dimensions:
            if dimension.kind == "absent_by_default":
                continue
            for ref in dimension.evidence_refs:
                if ref in bundle.bullet_pool and all(entry["ref"] != ref for entry in adjacent):
                    adjacent.append(
                        {
                            "ref": ref,
                            "quote": bundle.bullet_pool[ref],
                            "reason": f"same area ({dimension.name}), does not name {named}",
                        }
                    )
        if adjacent:
            return Rating("Adjacent", adjacent[:_MAX_CITED], terms, dimensions, NO_COVERAGE)
    return Rating("Unsupported", [], terms, dimensions, NO_COVERAGE)


# --- gaps and questions (G1 to G5) ---


def _question(requirement: str, rating: Rating) -> str:
    """One concrete question for a gap row (G2)."""
    for dimension in rating.dimensions:
        if dimension.candidate_question:
            return dimension.candidate_question
    coverage = rating.coverage
    if coverage.members:
        family, member = coverage.members[0]
        return f"Beyond {member}, which {family} services have you used in production?"
    covered = set(coverage.direct)
    open_terms = [term for term in rating.terms if term.label not in covered]
    for term in open_terms:
        if term.kind == "technology":
            return f"Do you have hands-on {term.label} experience you can add to the CV?"
    for term in open_terms:
        return f"Which project shows {term.label}, and can it go on the CV?"
    if requirement_kind(requirement):
        return f'Can you confirm this requirement: "{requirement}"?'
    return f'Which experience supports this requirement: "{requirement}"?'


def _seniority_level(bundle: ResumeBundle, role: str) -> str | None:
    """G3: a level the requested role names that no record of the
    candidate's holds."""
    held = " ".join([bundle.primary_identity, *(item.title for item in bundle.roles)]).lower()
    for level in SENIORITY_LEVELS:
        if re.search(rf"\b{level}\b", role, re.IGNORECASE) and not re.search(rf"\b{level}\b", held):
            return level
    return None


def _action(status: str, evidence: list[dict[str, str]]) -> str:
    refs = ", ".join(entry["ref"] for entry in evidence if entry["ref"] != "computed")
    if status == "Direct":
        return f"Lead with {refs}." if refs else "State the computed fact as is."
    if status == "Partial":
        return f"Cite {refs} only for what each reason names. {GAP_ACTION}"
    return GAP_ACTION


def _row(item: dict[str, str], rating: Rating) -> dict[str, object]:
    return {
        **item,
        "dimension": rating.dimensions[0].name if rating.dimensions else "",
        "dimensions": [dimension.name for dimension in rating.dimensions],
        "evidence_status": rating.status,
        "confidence": CONFIDENCE[rating.status],
        "exact_cv_evidence": rating.evidence,
        "interpretation": rating.evidence[0]["reason"]
        if rating.evidence
        else "No bullet names what this asks for.",
        "truthful_tailoring_action": _action(rating.status, rating.evidence),
    }


def evaluate_resume_fit(
    bundle: ResumeBundle, jd_text: str, role: str = "", as_of: date | None = None
) -> dict[str, object]:
    """Build a fact-grounded fit analysis suitable for metadata or a report
    (proof: tests/test_resume_evaluation.py)."""
    vocabulary = build_vocabulary(bundle)
    rated: list[tuple[dict[str, object], str]] = []  # (row, its gap question)
    for item in extract_jd_requirements(bundle, jd_text, role):
        rating = _rate(bundle, vocabulary, item["requirement"], as_of)
        question = "" if rating.status == "Direct" else _question(item["requirement"], rating)
        rated.append((_row(item, rating), question))

    level = _seniority_level(bundle, role)
    if level:
        label = level.capitalize()
        item = {
            "requirement": f"{label}-level scope",
            "source": role,
            "item_class": "requirement",
            "jd_importance": "important",
        }
        row = _row(item, Rating("Unsupported", [], [], [], NO_COVERAGE))
        row["interpretation"] = f"No role title holds the {level} level."
        rated.append(
            (
                row,
                f"Which work shows {label}-level scope, such as technical decisions "
                "across several teams?",
            )
        )

    matrix = [row for row, _ in rated]
    order = {status: index for index, status in enumerate(EVIDENCE_STATUSES)}
    gaps = sorted(
        (pair for pair in rated if pair[0]["evidence_status"] != "Direct"),
        key=lambda pair: order[str(pair[0]["evidence_status"])],
    )

    questions: list[str] = []
    # G4: compensation is scanned on the whole posting, whatever the class.
    if _COMPENSATION.search(jd_text or ""):
        questions.append(
            "What salary or compensation range should be used? The CV does not provide one."
        )
    for _, question in gaps:
        if question not in questions:
            questions.append(question)

    dimensions_summary: list[dict[str, object]] = []
    for dimension in bundle.evaluation_dimensions:
        related = [row for row in matrix if dimension.name in cast("list[str]", row["dimensions"])]
        statuses = [str(row["evidence_status"]) for row in related]
        status = (
            "Unsupported"
            if dimension.kind == "absent_by_default" or not statuses
            else min(statuses, key=lambda value: order[value])
        )
        refs = list(
            dict.fromkeys(
                entry["ref"]
                for row in related
                if row["evidence_status"] == status
                for entry in cast("list[dict[str, str]]", row["exact_cv_evidence"])
                if entry["ref"] != "computed"
            )
        )
        importances = [str(row["jd_importance"]) for row in related]
        importance = next(
            (value for value in JD_IMPORTANCE if value in importances), "nice-to-have"
        )
        dimensions_summary.append(
            {
                "dimension": dimension.name,
                "importance": importance,
                "evidence_status": status,
                "confidence": CONFIDENCE[status],
                "evidence_refs": refs,
            }
        )

    strengths = [item for item in dimensions_summary if item["evidence_status"] == "Direct"]
    direct_count = sum(row["evidence_status"] == "Direct" for row in matrix)
    return {
        "executive_conclusion": (
            f"Direct evidence for {direct_count} of {len(matrix)} requirements; "
            f"{len(gaps)} need the candidate's answer before any claim (section 6). "
            "No claim is invented."
        ),
        "evidence_matrix": matrix,
        "dimensions": dimensions_summary,
        "strengths": strengths,
        "unsupported_or_partial_requirements": [row for row, _ in gaps],
        "benefits": posting_benefits(jd_text),
        "recommended_positioning": (
            f"{bundle.primary_identity}; describe adjacent work as supported "
            "collaboration, never as unsupported ownership."
        ),
        "candidate_questions": questions,
    }


def format_fit_evaluation_markdown(evaluation: dict[str, object], company: str, role: str) -> str:
    """Render the structured evaluator output without introducing new claims."""

    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    def rows(key: str) -> list[dict[str, object]]:
        value = evaluation.get(key)
        if not isinstance(value, list):
            return []
        return [
            cast("dict[str, object]", item)
            for item in cast("list[object]", value)
            if isinstance(item, dict)
        ]

    def entries(item: dict[str, object]) -> list[dict[str, object]]:
        value = item.get("exact_cv_evidence")
        if not isinstance(value, list):
            return []
        return [
            cast("dict[str, object]", entry)
            for entry in cast("list[object]", value)
            if isinstance(entry, dict)
        ]

    lines = [
        f"# Resume fit evaluation: {company}",
        f"Role: {role}",
        "",
        "## 1. Executive conclusion",
        str(evaluation.get("executive_conclusion", "")),
        "",
        "## 2. Evidence matrix",
        "| Requirement | Importance | Status | Confidence "
        "| Exact CV evidence | Reason | Tailoring action |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in rows("evidence_matrix"):
        cited = entries(item)
        evidence = (
            "; ".join(
                f"{entry['ref']}: {entry['quote']}" if entry.get("quote") else str(entry["ref"])
                for entry in cited
            )
            or "None established"
        )
        reasons = "; ".join(str(entry.get("reason", "")) for entry in cited) or "None"
        lines.append(
            "| "
            + " | ".join(
                [
                    cell(item.get("requirement")),
                    cell(item.get("jd_importance")),
                    cell(item.get("evidence_status")),
                    cell(item.get("confidence")),
                    cell(evidence),
                    cell(reasons),
                    cell(item.get("truthful_tailoring_action")),
                ]
            )
            + " |"
        )
    lines.extend(["", "## 3. Strengths"])
    strengths = rows("strengths")
    for item in strengths:
        refs = item.get("evidence_refs")
        refs_list = cast("list[object]", refs) if isinstance(refs, list) else []
        lines.append(f"- {item.get('dimension')}: {', '.join(str(ref) for ref in refs_list)}")
    if not strengths:
        lines.append("- No dimension has Direct evidence.")
    lines.extend(["", "## 4. Unsupported or partial requirements"])
    gaps = rows("unsupported_or_partial_requirements")
    for status in EVIDENCE_STATUSES[1:]:
        lines.extend(
            f"- {status}: {item.get('requirement')}"
            for item in gaps
            if item.get("evidence_status") == status
        )
    if not gaps:
        lines.append("- Every requirement has Direct evidence.")
    lines.extend(
        [
            "",
            "## 5. Recommended truthful positioning",
            str(evaluation.get("recommended_positioning", "")),
        ]
    )
    lines.extend(["", "## 6. Questions for the candidate"])
    questions = evaluation.get("candidate_questions")
    if isinstance(questions, list) and questions:
        lines.extend(f"- {question}" for question in cast("list[object]", questions))
    else:
        lines.append("- None.")
    lines.extend(["", "## 7. Benefits listed in the posting"])
    benefits = evaluation.get("benefits")
    if isinstance(benefits, list) and benefits:
        lines.extend(f"- {benefit}" for benefit in cast("list[object]", benefits))
    else:
        lines.append("- None listed.")
    return "\n".join(lines) + "\n"
