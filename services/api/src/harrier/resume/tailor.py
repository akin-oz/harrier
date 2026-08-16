"""End-to-end tailoring run (spec 013): plan, optional AI ordering,
markdown, HTML, PDF gate, then and only then the tracker update.

A failing PDF gate leaves the tracker row unchanged (spec acceptance).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harrier.db import data_dir
from harrier.resume.ai import build_ai_tailored_content
from harrier.resume.content import load_bundle, load_truth_sources
from harrier.resume.evaluation import evaluate_resume_fit, format_fit_evaluation_markdown
from harrier.resume.htmlrender import render_html
from harrier.resume.markdown import build_internal_metadata, build_markdown, slugify
from harrier.resume.pdf import render_pdf, validate_rendered_pdf
from harrier.resume.plan import apply_ai_bullet_order, build_content_plan, validate_content_plan
from harrier.screening.descriptions import load_cached_description
from harrier.tracker import get_job, set_status

logger = logging.getLogger(__name__)

RenderPdfFn = Callable[[str, Path], None]
ValidatePdfFn = Callable[[Path, str], list[str]]


@dataclass(frozen=True)
class TailorResult:
    markdown_path: Path
    html_path: Path
    pdf_path: Path
    metadata_path: Path
    evaluation_path: Path | None
    ai_tailored: bool


def resumes_dir() -> Path:
    return data_dir() / "resumes"


def resume_paths_for(
    candidate_name: str,
    company: str,
    role: str,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """Where a tailoring run puts its files, by artifact kind.

    `run_tailor` calls this rather than building the names inline, so the
    reader that serves an artifact back and the writer that produced it cannot
    disagree about where it is (spec 047). Duplicating the slug in the reader
    is the second implementation the spec forbids.
    """
    directory = output_dir if output_dir is not None else resumes_dir()
    slug = slugify(f"{candidate_name}-{company}-{role}")
    return {
        "markdown": directory / f"{slug}.md",
        "html": directory / f"{slug}.html",
        "pdf": directory / f"{slug}.pdf",
        "metadata": directory / f"{slug}.metadata.json",
        "evaluation": directory / f"{slug}.evaluation.md",
    }


def run_tailor(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    jd_text: str | None = None,
    no_ai: bool = False,
    output_dir: Path | None = None,
    render: RenderPdfFn = render_pdf,
    validate: ValidatePdfFn = validate_rendered_pdf,
) -> TailorResult:
    """Tailor for one tracker job; the row updates only after the PDF gate
    passes."""
    row = get_job(conn, job_id)
    company = row.get("company", "")
    requested_role = row.get("title", "")
    job_url = row.get("url", "")

    jd_source = "arg_text" if jd_text else "none"
    if not jd_text:
        cached = load_cached_description(job_url)
        if cached:
            jd_text = cached
            jd_source = "cache"

    bundle = load_bundle(conn)
    sources = load_truth_sources(conn)

    plan = build_content_plan(bundle, jd_text or "", requested_role)
    plan_errors = validate_content_plan(plan, bundle)
    if plan_errors:
        raise ValueError("invalid resume content plan: " + "; ".join(plan_errors))

    if jd_text and jd_text.strip() and not no_ai:
        ai_content = build_ai_tailored_content(bundle, sources, jd_text, company, requested_role)
        if ai_content:
            plan = apply_ai_bullet_order(plan, bundle, ai_content)
            ai_errors = validate_content_plan(plan, bundle)
            if ai_errors:
                raise ValueError("invalid AI-ordered resume content plan: " + "; ".join(ai_errors))
            logger.info("AI evidence ordering applied for %s", company)
        else:
            logger.info("using validated deterministic evidence plan for %s", company)

    markdown = build_markdown(bundle, sources, plan)
    html_text = render_html(markdown, bundle)
    fit_evaluation = evaluate_resume_fit(bundle, jd_text, requested_role) if jd_text else None

    directory = output_dir if output_dir is not None else resumes_dir()
    directory.mkdir(parents=True, exist_ok=True)
    paths = resume_paths_for(bundle.name, company, requested_role, directory)
    markdown_path = paths["markdown"]
    html_path = paths["html"]
    pdf_path = paths["pdf"]
    metadata_path = paths["metadata"]

    markdown_path.write_text(markdown + "\n", encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    metadata = build_internal_metadata(
        company=company,
        requested_role=requested_role,
        visible_role_title=markdown.splitlines()[1],
        job_url=job_url,
        tracker_score=row.get("fit_score", ""),
        jd_source=jd_source,
        plan=plan,
        fit_evaluation=fit_evaluation,
    )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    render(html_text, pdf_path)
    pdf_errors = validate(pdf_path, html_text)
    if pdf_errors:
        raise RuntimeError("resume render validation failed: " + "; ".join(pdf_errors))

    evaluation_path: Path | None = None
    if fit_evaluation is not None:
        evaluation_path = paths["evaluation"]
        evaluation_path.write_text(
            format_fit_evaluation_markdown(fit_evaluation, company, requested_role),
            encoding="utf-8",
        )

    # The PDF gate passed; only now does the tracker row change.
    set_status(conn, job_id, "tailored_cv_requested")
    return TailorResult(
        markdown_path=markdown_path,
        html_path=html_path,
        pdf_path=pdf_path,
        metadata_path=metadata_path,
        evaluation_path=evaluation_path,
        ai_tailored=plan.ai_ordered,
    )
