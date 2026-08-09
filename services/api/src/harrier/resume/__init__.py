"""Tailored resume generation (spec 013).

Verified content only, PDF or failure, no internal labels visible. The
engine is persona-free: all candidate content arrives as the resume
content bundle from the profile store (content.py).
"""

from __future__ import annotations

from harrier.resume.content import (
    ResumeBundle,
    ResumeBundleError,
    TruthSources,
    load_bundle,
    load_truth_sources,
    parse_bundle,
    require_truth,
)
from harrier.resume.evaluation import evaluate_resume_fit, format_fit_evaluation_markdown
from harrier.resume.facts import (
    professional_experience_label,
    professional_experience_years,
    role_period_label,
)
from harrier.resume.htmlrender import render_html
from harrier.resume.markdown import (
    build_internal_metadata,
    build_markdown,
    normalize_visible_role_title,
    normalize_visible_url_text,
    slugify,
)
from harrier.resume.pdf import render_pdf, validate_rendered_pdf
from harrier.resume.plan import (
    ContentPlan,
    apply_ai_bullet_order,
    build_content_plan,
    build_presentation_title,
    validate_content_plan,
)
from harrier.resume.ranking import rank_skills

__all__ = [
    "ContentPlan",
    "ResumeBundle",
    "ResumeBundleError",
    "TruthSources",
    "apply_ai_bullet_order",
    "build_content_plan",
    "build_internal_metadata",
    "build_markdown",
    "build_presentation_title",
    "evaluate_resume_fit",
    "format_fit_evaluation_markdown",
    "load_bundle",
    "load_truth_sources",
    "normalize_visible_role_title",
    "normalize_visible_url_text",
    "parse_bundle",
    "professional_experience_label",
    "professional_experience_years",
    "rank_skills",
    "render_html",
    "render_pdf",
    "require_truth",
    "role_period_label",
    "slugify",
    "validate_content_plan",
    "validate_rendered_pdf",
]
