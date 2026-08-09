"""Evidence-grounded JD fit evaluation (spec 013 port).

The JD identifies requirements and their wording only; candidate evidence
always attaches from the verified bullet pool. Dimension behavior is keyed
by the bundle's dimension kind, not hardcoded dimension names (stated
change: the engine is persona-free).
"""

from __future__ import annotations

import re
from typing import cast

from harrier.resume.content import EvaluationDimension, ResumeBundle

EVIDENCE_STATUSES = ("Strong evidence", "Partial evidence", "No evidence", "Contradiction")
JD_IMPORTANCE = ("core", "important", "nice-to-have")

_NO_EVIDENCE_NOTE = "The CV does not establish this requirement; absence is not proof of inability."


def _jd_units(jd_text: str) -> list[str]:
    """Readable JD bullets/sentences, never treated as candidate facts."""
    units: list[str] = []
    for raw in re.split(r"(?:\r?\n+|(?<=[.!?])\s+)", jd_text or ""):
        unit = re.sub(r"^\s*[-*•\d.)]+\s*", "", raw).strip()
        if unit and len(unit) >= 12:
            units.append(unit)
    return list(dict.fromkeys(units))


def extract_jd_requirements(
    bundle: ResumeBundle, jd_text: str, role: str = ""
) -> list[dict[str, str]]:
    """Extract atomic, auditable requirement records from JD text."""
    requirements: list[dict[str, str]] = []
    seen: set[str] = set()
    for unit in _jd_units(jd_text):
        lower = unit.lower()
        matched = [
            dimension.name
            for dimension in bundle.evaluation_dimensions
            if any(signal in lower for signal in dimension.signals)
        ]
        if not matched:
            continue
        importance = (
            "core"
            if any(term in lower for term in ("must", "required", "requirement", "need to", "own "))
            else "important"
        )
        if any(term in lower for term in ("nice to have", "bonus", "plus", "preferred")):
            importance = "nice-to-have"
        for dimension_name in matched:
            key = f"{dimension_name}|{lower}"
            if key in seen:
                continue
            seen.add(key)
            requirements.append(
                {"requirement": unit, "jd_importance": importance, "dimension": dimension_name}
            )
    return requirements


def _evidence_for(bundle: ResumeBundle, dimension: EvaluationDimension) -> list[dict[str, str]]:
    return [
        {"ref": ref, "quote": bundle.bullet_pool[ref]}
        for ref in dimension.evidence_refs
        if ref in bundle.bullet_pool
    ]


def _status_for_evidence(
    bundle: ResumeBundle, dimension: EvaluationDimension, jd_requirement: str
) -> tuple[str, list[dict[str, str]], str]:
    evidence = _evidence_for(bundle, dimension)
    lower = jd_requirement.lower()
    if dimension.kind == "backend_ownership":
        backend_roles = sum(
            "backend" in role.competencies or "full-stack" in role.competencies
            for role in bundle.roles
        )
        note = (
            "The CV shows API collaboration and/or backend-adjacent utilities, "
            "but not substantial backend ownership."
        )
        if backend_roles == 0:
            if evidence:
                return "Partial evidence", evidence, note
            return (
                "No evidence",
                [],
                "The CV does not establish substantial backend or full-stack ownership.",
            )
        return "Partial evidence", evidence, note
    if dimension.kind == "absent_by_default":
        return "No evidence", [], _NO_EVIDENCE_NOTE
    if not evidence:
        return "No evidence", [], _NO_EVIDENCE_NOTE
    if (
        dimension.kind == "database"
        and "database" in lower
        and not any(term in lower for term in ("api", "rest", "graphql"))
    ):
        return (
            "No evidence",
            [],
            "The JD requests database experience, but the CV does not name a "
            "database or relational database.",
        )
    if len(evidence) >= 2:
        return (
            "Strong evidence",
            evidence,
            "Multiple concrete CV examples support this requirement.",
        )
    return (
        "Partial evidence",
        evidence,
        "The CV contains adjacent or limited evidence for this requirement.",
    )


def evaluate_resume_fit(bundle: ResumeBundle, jd_text: str, role: str = "") -> dict[str, object]:
    """Build a fact-grounded fit analysis suitable for metadata or a report."""
    requirements = extract_jd_requirements(bundle, jd_text, role)
    by_name = {dimension.name: dimension for dimension in bundle.evaluation_dimensions}
    matrix: list[dict[str, object]] = []
    for item in requirements:
        dimension = by_name[item["dimension"]]
        status, evidence, interpretation = _status_for_evidence(
            bundle, dimension, item["requirement"]
        )
        confidence = "high" if len(evidence) >= 2 else "medium" if evidence else "low"
        if status in {"Strong evidence", "Partial evidence"}:
            action = (
                "Prioritize supported evidence refs: "
                + ", ".join(entry["ref"] for entry in evidence)
                + "."
            )
        elif dimension.candidate_question:
            action = "Keep as a neutral gap note; do not imply unsupported experience."
        else:
            action = (
                "Do not add a claim; ask the candidate only if this would "
                "materially change the application."
            )
        matrix.append(
            {
                **item,
                "evidence_status": status,
                "confidence": confidence,
                "exact_cv_evidence": evidence,
                "interpretation": interpretation,
                "truthful_tailoring_action": action,
            }
        )

    dimensions: list[dict[str, object]] = []
    for dimension in bundle.evaluation_dimensions:
        related = [item for item in matrix if item["dimension"] == dimension.name]
        evidence = _evidence_for(bundle, dimension)
        statuses = [str(item["evidence_status"]) for item in related]
        status = (
            "Strong evidence"
            if "Strong evidence" in statuses
            else "Partial evidence"
            if "Partial evidence" in statuses
            else "No evidence"
        )
        importance = next(
            (str(item["jd_importance"]) for item in related if item["jd_importance"] == "core"),
            next(
                (
                    str(item["jd_importance"])
                    for item in related
                    if item["jd_importance"] == "important"
                ),
                "nice-to-have",
            ),
        )
        dimensions.append(
            {
                "dimension": dimension.name,
                "importance": importance,
                "evidence_status": status,
                "confidence": "high" if len(evidence) >= 2 else "medium" if evidence else "low",
                "evidence_refs": [entry["ref"] for entry in evidence],
            }
        )

    strengths = [item for item in dimensions if item["evidence_status"] == "Strong evidence"]
    partial = [
        item
        for item in dimensions
        if item["evidence_status"] in {"Partial evidence", "No evidence"}
    ]
    questions: list[str] = []
    if any(
        "salary" in item["requirement"].lower() or "compensation" in item["requirement"].lower()
        for item in requirements
    ):
        questions.append(
            "What salary or compensation range should be used? The CV does not provide one."
        )
    asked_dimensions = {item["dimension"] for item in requirements}
    for dimension in bundle.evaluation_dimensions:
        if dimension.candidate_question and dimension.name in asked_dimensions:
            questions.append(dimension.candidate_question)

    strong_names = [str(item["dimension"]) for item in strengths]
    return {
        "executive_conclusion": (
            "Strong transferable fit in " + ", ".join(strong_names)
            if strong_names
            else "No strong-evidence dimensions for this JD"
        )
        + "; unsupported requirements remain bounded (no invented claims).",
        "evidence_matrix": matrix,
        "dimensions": dimensions,
        "strengths": strengths,
        "unsupported_or_partial_requirements": partial,
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

    lines = [
        f"# Resume fit evaluation: {company}",
        f"Role: {role}",
        "",
        "## 1. Executive conclusion",
        str(evaluation.get("executive_conclusion", "")),
        "",
        "## 2. Evidence matrix",
        "| Requirement | Importance | Status | Confidence "
        "| Exact CV evidence | Interpretation | Tailoring action |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in rows("evidence_matrix"):
        evidence_entries = item.get("exact_cv_evidence")
        entry_dicts = (
            [
                cast("dict[str, object]", entry)
                for entry in cast("list[object]", evidence_entries)
                if isinstance(entry, dict)
            ]
            if isinstance(evidence_entries, list)
            else []
        )
        evidence = (
            "; ".join(f"{entry['ref']}: {entry['quote']}" for entry in entry_dicts)
            or "None established"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    cell(item.get("requirement")),
                    cell(item.get("jd_importance")),
                    cell(item.get("evidence_status")),
                    cell(item.get("confidence")),
                    cell(evidence),
                    cell(item.get("interpretation")),
                    cell(item.get("truthful_tailoring_action")),
                ]
            )
            + " |"
        )
    lines.extend(["", "## 3. Strengths"])
    for item in rows("strengths"):
        refs = item.get("evidence_refs")
        refs_list = cast("list[object]", refs) if isinstance(refs, list) else []
        refs_text = ", ".join(str(ref) for ref in refs_list)
        lines.append(f"- {item.get('dimension')}: {refs_text}")
    lines.extend(["", "## 4. Unsupported or partial requirements"])
    lines.extend(
        f"- {item.get('dimension')}: {item.get('evidence_status')}"
        for item in rows("unsupported_or_partial_requirements")
    )
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
    return "\n".join(lines) + "\n"
