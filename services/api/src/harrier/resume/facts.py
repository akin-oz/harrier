"""Canonical resume facts and date derivations (resume_facts.py port).

Facts and derivations only; presentation and tailoring live in the other
resume modules. All functions operate on the parsed bundle (content.py).
"""

from __future__ import annotations

from datetime import date

from harrier.resume.content import ResumeBundle, ResumeRole


class ResumeContentError(ValueError):
    pass


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ResumeContentError(f"invalid ISO date in resume content: {value!r}") from exc


def role_start_date(role: ResumeRole) -> date:
    return parse_iso_date(role.period_start)


def role_end_date(role: ResumeRole) -> date | None:
    return parse_iso_date(role.period_end) if role.period_end else None


def professional_career_start(bundle: ResumeBundle) -> date:
    """Earliest marked professional work period; never a derived prose string."""
    starts = [
        role_start_date(role)
        for role in bundle.roles
        if role.counts_towards_professional_experience
    ]
    if not starts:
        raise ResumeContentError("resume content has no professional work periods")
    earliest = min(starts)
    configured = parse_iso_date(bundle.professional_career_start)
    if configured != earliest:
        raise ResumeContentError(
            "professional_career_start must match the earliest role marked "
            "counts_towards_professional_experience"
        )
    return earliest


def completed_years_since(start: date, as_of: date | None = None) -> int:
    """Count completed calendar years; never round a partial anniversary up."""
    reference = as_of or date.today()
    if reference < start:
        raise ResumeContentError("as_of date cannot be before professional career start")
    return (
        reference.year - start.year - ((reference.month, reference.day) < (start.month, start.day))
    )


def professional_experience_years(bundle: ResumeBundle, as_of: date | None = None) -> int:
    return completed_years_since(professional_career_start(bundle), as_of)


def professional_experience_label(bundle: ResumeBundle, as_of: date | None = None) -> str:
    return f"{professional_experience_years(bundle, as_of)}+ years"


def format_month_year(value: date) -> str:
    return value.strftime("%b %Y")


def role_period_label(role: ResumeRole, as_of: date | None = None) -> str:
    """The actual engagement/role period; an ended engagement never reads
    Present, no matter what a parent relationship says."""
    start = role_start_date(role)
    end = role_end_date(role)
    end_label = format_month_year(end) if end else "Present"
    return f"{format_month_year(start)} – {end_label}"  # noqa: RUF001 (parity: en dash)
