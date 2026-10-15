"""End-to-end tailoring run (spec 013): plan, optional AI ordering,
markdown, HTML, PDF gate, then and only then the tracker update.

A failing PDF gate leaves the tracker row unchanged (spec acceptance).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from harrier.apply.brief import load_brief
from harrier.connection import Connection
from harrier.filestore import files, files_in, join_key, local_file, save_text
from harrier.resume.ai import build_ai_tailored_content
from harrier.resume.content import load_bundle, load_truth_sources
from harrier.resume.evaluation import evaluate_resume_fit, format_fit_evaluation_markdown
from harrier.resume.htmlrender import render_html
from harrier.resume.keywords import missing_keywords, missing_keywords_markdown
from harrier.resume.markdown import build_internal_metadata, build_markdown, slugify
from harrier.resume.pdf import render_pdf, render_validated_pdf, validate_rendered_pdf
from harrier.resume.plan import (
    apply_ai_bullet_order,
    build_content_plan,
    order_achievements_by_core,
    validate_content_plan,
    with_confirmed_skills,
)
from harrier.screening.descriptions import load_cached_description
from harrier.tracker import get_job, set_status
from harrier.tracks import AI_CV_SECTION, Scope, rules_for

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


RESUMES = "resumes"


def resume_keys_for(
    candidate_name: str, company: str, role: str, prefix: str = RESUMES
) -> dict[str, str]:
    """Where a tailoring run puts its files, by artifact kind.

    `run_tailor` calls this rather than building the names inline, so the
    reader that serves an artifact back and the writer that produced it cannot
    disagree about where it is (spec 047). Duplicating the slug in the reader
    is the second implementation the spec forbids.
    """
    slug = slugify(f"{candidate_name}-{company}-{role}")
    return {
        "markdown": join_key(prefix, f"{slug}.md"),
        "html": join_key(prefix, f"{slug}.html"),
        "pdf": join_key(prefix, f"{slug}.pdf"),
        "metadata": join_key(prefix, f"{slug}.metadata.json"),
        "evaluation": join_key(prefix, f"{slug}.evaluation.md"),
        # The focused kind's sidecar (spec 140).
        "missing_keywords": join_key(prefix, f"{slug}.missing-keywords.md"),
    }


def resume_paths_for(
    candidate_name: str,
    company: str,
    role: str,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """The local files behind `resume_keys_for`, or in `output_dir`."""
    with files_in(output_dir, RESUMES) as prefix:
        keys = resume_keys_for(candidate_name, company, role, prefix)
        return {kind: local_file(key) for kind, key in keys.items()}


def run_tailor(
    conn: Connection,
    scope: Scope,
    job_id: int,
    *,
    jd_text: str | None = None,
    no_ai: bool = False,
    output_dir: Path | None = None,
    render: RenderPdfFn = render_pdf,
    validate: ValidatePdfFn | None = None,
) -> TailorResult:
    """Tailor for one tracker job; the row updates only after the PDF gate
    passes.

    The scope's track kind picks the ranking prompt, the CV's section order
    and template, and the page counts the gate allows (spec 101). `validate`
    replaces the gate in tests; left out, it is the kind's.
    """
    kind = scope.track.kind
    gate = (
        validate
        if validate is not None
        else partial(validate_rendered_pdf, allowed_pages=rules_for(kind).pages)
    )
    row = get_job(conn, scope, job_id)
    company = row.get("company", "")
    requested_role = row.get("title", "")
    job_url = row.get("url", "")

    jd_source = "arg_text" if jd_text else "none"
    if not jd_text:
        cached = load_cached_description(job_url)
        if cached:
            jd_text = cached
            jd_source = "cache"

    # Skills the candidate confirmed in this job's brief count for this job
    # only (spec 071 O9).
    bundle = with_confirmed_skills(
        load_bundle(conn, scope), load_brief(conn, job_id).confirmed_skills
    )
    sources = load_truth_sources(conn)
    rules = rules_for(kind)

    # A kind that prints the AI-assisted section keeps its lines out of the
    # achievements (spec 140).
    exclude = (
        frozenset(bundle.ai_bullets) if AI_CV_SECTION in rules.cv_sections else frozenset[str]()
    )
    plan = build_content_plan(bundle, jd_text or "", requested_role, exclude=exclude)
    plan_errors = validate_content_plan(plan, bundle)
    if plan_errors:
        raise ValueError("invalid resume content plan: " + "; ".join(plan_errors))

    if jd_text and jd_text.strip() and not no_ai:
        ai_content = build_ai_tailored_content(
            bundle, sources, jd_text, company, requested_role, kind=kind
        )
        if ai_content:
            plan = apply_ai_bullet_order(plan, bundle, ai_content)
            # Core-requirement evidence leads whatever the model preferred;
            # the model's order breaks ties (spec 071 O4).
            plan = order_achievements_by_core(plan, bundle, jd_text, requested_role)
            ai_errors = validate_content_plan(plan, bundle)
            if ai_errors:
                raise ValueError("invalid AI-ordered resume content plan: " + "; ".join(ai_errors))
            logger.info("AI evidence ordering applied for %s", company)
        else:
            logger.info("using validated deterministic evidence plan for %s", company)

    markdown = build_markdown(bundle, sources, plan, kind=kind)
    html_text = render_html(markdown, bundle, kind=kind)
    fit_evaluation = evaluate_resume_fit(bundle, jd_text, requested_role) if jd_text else None

    # Outside this block the store is the data directory's again: the status
    # change below reads the description cache.
    with files_in(output_dir, RESUMES) as prefix:
        keys = resume_keys_for(bundle.name, company, requested_role, prefix)
        store = files()
        save_text(keys["markdown"], markdown + "\n", atomic=False)
        # From here an earlier run's PDF no longer describes the markdown
        # beside it, and the artifact endpoint would serve it as current
        # (spec 067).
        store.delete(keys["pdf"])
        try:
            save_text(keys["html"], html_text, atomic=False)
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
            save_text(keys["metadata"], json.dumps(metadata, indent=2) + "\n", atomic=False)
            if not rules.missing_keywords:
                # The same key an earlier focused run wrote: it would sit
                # beside this draft as if it were this run's (review of PR
                # #343).
                store.delete(keys["missing_keywords"])
            else:
                # Beside the draft and never in it: the posting's skills the
                # truth does not hold (spec 140).
                save_text(
                    keys["missing_keywords"],
                    missing_keywords_markdown(
                        company, requested_role, missing_keywords(bundle, sources, jd_text or "")
                    ),
                    atomic=False,
                )

            pdf_errors = render_validated_pdf(html_text, keys["pdf"], render, gate)
            if pdf_errors:
                raise RuntimeError("resume render validation failed: " + "; ".join(pdf_errors))
        except BaseException:
            # The draft and its sidecar stay for the operator. The HTML is
            # only an intermediate, and an earlier evaluation report would be
            # served beside the new draft as if it were this run's.
            store.delete(keys["html"])
            store.delete(keys["evaluation"])
            raise

        evaluation_path: Path | None = None
        if fit_evaluation is not None:
            save_text(
                keys["evaluation"],
                format_fit_evaluation_markdown(fit_evaluation, company, requested_role),
                atomic=False,
            )
            evaluation_path = local_file(keys["evaluation"])
        paths = {artifact: local_file(key) for artifact, key in keys.items()}

    # The PDF gate passed; only now does the tracker row change. The candidate
    # asked for this CV, so the move is theirs (spec 079).
    set_status(conn, scope, job_id, "tailored_cv_requested", actor="candidate")
    return TailorResult(
        markdown_path=paths["markdown"],
        html_path=paths["html"],
        pdf_path=paths["pdf"],
        metadata_path=paths["metadata"],
        evaluation_path=evaluation_path,
        ai_tailored=plan.ai_ordered,
    )
