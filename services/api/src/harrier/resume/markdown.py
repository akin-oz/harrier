"""Resume markdown assembly and final-content validation (spec 013 port).

Internal labels ("Tailored for X", company prefixes) are scrubbed from
the visible title; the header comes from the grounded content plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from harrier.resume.content import ResumeBundle, TruthSources, forbidden_hits, year_counts
from harrier.resume.dashes import dash_marks, describe
from harrier.resume.facts import role_period_label
from harrier.resume.heading import role_heading
from harrier.resume.plan import ContentPlan, validate_content_plan
from harrier.tracks import INDUSTRY_CV_SECTIONS, rules_for

_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.translate(_TR_MAP).lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)


def normalize_visible_role_title(company: str, role: str) -> str:
    title = re.sub(r"^\s*Tailored for\s+", "", role, flags=re.IGNORECASE).strip()
    if company.strip():
        company_pattern = re.compile(
            rf"^\s*{re.escape(company.strip())}\s*(?:[—:\-|]\s*)+", flags=re.IGNORECASE
        )
        while True:
            updated = company_pattern.sub("", title).strip()
            if updated == title:
                break
            title = updated
    title = re.sub(r"\s*(?:\(\s*Remote\s*\)|[—-]\s*Remote)\s*$", "", title, flags=re.IGNORECASE)
    remote_suffixes = (
        r"Remote Europe|Remote EMEA|Remote EU|Remote UK/EU|Worldwide \(±?3 hours CET\)"
    )
    title = re.sub(
        rf"\s*-\s*({remote_suffixes})\s*$",
        r" — \1",
        title,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", title).strip(" -—:|")


def normalize_visible_url_text(url: str) -> str:
    visible = (url or "").strip()
    visible = re.sub(r"^https?://", "", visible, flags=re.IGNORECASE)
    return visible.rstrip("/")


@dataclass(frozen=True)
class RenderedResume:
    markdown: str
    plan: ContentPlan


class UnverifiedClaimError(ValueError):
    """A generated line is not supported by the truth documents.

    Raised rather than returned, because every caller that could return it
    would have to remember to check, and the previous behaviour was exactly
    a caller that did not.
    """

    def __init__(self, bullet_id: str, text: str) -> None:
        self.bullet_id = bullet_id
        self.text = text
        super().__init__(f"unverifiable claim {bullet_id}: {text[:120]}")


def resolve_bullets(
    bundle: ResumeBundle, sources: TruthSources, ids: list[str]
) -> tuple[list[str], list[str]]:
    """Resolve IDs to text with a final truthfulness check; return (texts,
    omitted ids)."""
    texts: list[str] = []
    omitted: list[str] = []
    for bullet_id in ids:
        text = bundle.bullet_pool.get(bullet_id)
        if not text:
            omitted.append(bullet_id)
            continue
        if sources.contains(text):
            texts.append(text)
        else:
            # An unverifiable line is not silently dropped (spec 034). Being
            # quietly shorter is how an empty or drifted truth document
            # produced a clean PDF with empty sections: the strictest possible
            # truth failure looked like success.
            raise UnverifiedClaimError(bullet_id, text)
    return texts, omitted


def _section_bullets(
    bundle: ResumeBundle,
    sources: TruthSources,
    ids: list[str],
    fallback_ids: list[str],
    minimum: int = 2,
) -> list[str]:
    texts, _ = resolve_bullets(bundle, sources, ids)
    if len(texts) < minimum:
        texts, _ = resolve_bullets(bundle, sources, fallback_ids)
    return texts


def _education_lines(bundle: ResumeBundle) -> list[str]:
    """One `### degree` heading and one school line per entry, in bundle
    order: the same entry marker the experience section uses (spec 059)."""
    lines: list[str] = []
    for entry in bundle.education:
        lines.extend([f"### {entry.degree}", entry.school])
    return lines


def build_markdown(
    bundle: ResumeBundle,
    sources: TruthSources,
    plan: ContentPlan,
    as_of: date | None = None,
    *,
    kind: str,
) -> str:
    """Assemble the resume markdown from a validated plan, in the section
    order of the track's kind (spec 101): education after experience on the
    industry kind, straight after the profile on the academic."""
    plan_errors = validate_content_plan(plan, bundle)
    if plan_errors:
        raise ValueError("invalid resume content plan: " + "; ".join(plan_errors))

    order = rules_for(kind).cv_sections
    if sorted(order) != sorted(INDUSTRY_CV_SECTIONS):
        raise ValueError(f"the {kind} CV section order is not a permutation of the CV sections")

    achievements = _section_bullets(
        bundle,
        sources,
        plan.selected_achievements,
        list(bundle.default_achievements),
    )
    experience = ["## EXPERIENCE", ""]
    for role in bundle.roles:
        bullets = _section_bullets(
            bundle,
            sources,
            plan.role_bullets.get(role.id, []),
            list(role.default_bullets),
        )
        experience.extend(
            [
                f"### {role_heading(role.organization, role.title, role.employment_type)}",
                plan.role_periods[role.id],
                *[f"- {bullet}" for bullet in bullets],
                "",
            ]
        )
    # Each section ends with the blank line that separates it from the next;
    # the last one's is dropped, so the industry order joins to exactly the
    # text it always has.
    sections = {
        "## PROFILE": ["## PROFILE", plan.profile, ""],
        "## SELECTED ACHIEVEMENTS": [
            "## SELECTED ACHIEVEMENTS",
            *[f"- {bullet}" for bullet in achievements],
            "",
        ],
        "## EXPERIENCE": experience,
        "## EDUCATION": ["## EDUCATION", *_education_lines(bundle), ""],
        "## CERTIFICATIONS": ["## CERTIFICATIONS", *bundle.certifications, ""],
        "## TECHNICAL SKILLS": ["## TECHNICAL SKILLS", ", ".join(plan.skills), ""],
    }
    lines = [
        f"# {bundle.name}",
        plan.title,
        f"{bundle.location} | {bundle.email} | {normalize_visible_url_text(bundle.linkedin)}",
        "",
    ]
    for heading in order:
        lines.extend(sections[heading])
    if lines[-1] == "":
        lines.pop()
    markdown = "\n".join(lines)
    markdown_errors = validate_rendered_markdown(markdown, plan, bundle)
    if markdown_errors:
        raise ValueError("invalid rendered resume content: " + "; ".join(markdown_errors))
    return markdown


# The markdown headings that must never render empty. An empty or drifted
# truth document produced a resume with both of these blank, a passing
# one-page check, and a tracker status advance (spec 034).
REQUIRED_SECTIONS = ("## SELECTED ACHIEVEMENTS", "## EXPERIENCE")


def _section_is_populated(markdown: str, heading: str) -> bool:
    """Whether anything renders between this heading and the next one."""
    lines = markdown.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return True  # absent sections are validate_content_plan's business
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        # The first non-blank line decides: another section heading means
        # this one rendered nothing.
        return not stripped.startswith("## ")
    return False


def validate_rendered_markdown(markdown: str, plan: ContentPlan, bundle: ResumeBundle) -> list[str]:
    """Heuristic final-content checks after markdown assembly."""
    errors = validate_content_plan(plan, bundle)
    for heading in REQUIRED_SECTIONS:
        if heading in markdown and not _section_is_populated(markdown, heading):
            errors.append(f"rendered resume has an empty {heading} section")
    forbidden = forbidden_hits(bundle.forbidden_phrases, markdown)
    if forbidden:
        errors.append(f"rendered resume contains forbidden phrases: {', '.join(forbidden)}")
    if "�" in markdown:
        errors.append("rendered resume contains replacement characters")
    if markdown.splitlines()[1] != plan.title:
        errors.append("rendered title differs from the grounded content plan")
    experience_text = bundle.experience_statement.rstrip(".") or plan.experience_label
    if experience_text.lower() not in markdown.lower():
        errors.append("rendered resume is missing the calculated experience length")
    marks = dash_marks(markdown)
    if marks:
        errors.append(f"rendered resume uses a dash as punctuation ({describe(marks)})")
    computed_years = int(plan.experience_label.split("+", 1)[0])
    inflated = [count for count in year_counts(markdown) if count > computed_years]
    if inflated:
        errors.append(
            f"rendered resume states {max(inflated)} years; the record holds {computed_years}"
        )
    if re.search(r"\bdecades?\b", markdown, re.IGNORECASE):
        errors.append("rendered resume counts experience in decades")
    for role in bundle.roles:
        if role_period_label(role) not in markdown:
            errors.append(f"rendered resume is missing canonical period for {role.id}")
    return errors


def build_internal_metadata(
    *,
    company: str,
    requested_role: str,
    visible_role_title: str,
    job_url: str,
    tracker_score: str,
    jd_source: str,
    plan: ContentPlan,
    fit_evaluation: dict[str, object] | None,
) -> dict[str, object]:
    """The sidecar only; nothing here may enter the visible artifact."""
    return {
        "company": company,
        "requested_role": requested_role,
        "visible_role_title": visible_role_title,
        "job_url": job_url,
        "tracker_score": tracker_score,
        "jd_source": jd_source,
        "ai_tailored": plan.ai_ordered,
        "selected_achievements": plan.selected_achievements,
        "role_bullets": plan.role_bullets,
        "fit_evaluation": fit_evaluation,
    }
