"""Behavior pins for tailored resume generation (spec 013), ported from the
old repo's tests/test_tailor_resume.py onto the synthetic persona in
config/resume-content.example.json (which these tests thereby prove valid).
"""

import copy
import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from harrier.db import connect
from harrier.profile.store import put_document
from harrier.resume import (
    EducationEntry,
    ResumeBundle,
    ResumeBundleError,
    TruthSources,
    apply_ai_bullet_order,
    build_content_plan,
    build_markdown,
    build_presentation_title,
    evaluate_resume_fit,
    normalize_visible_role_title,
    normalize_visible_url_text,
    parse_bundle,
    professional_experience_label,
    professional_experience_years,
    rank_skills,
    render_html,
    slugify,
    validate_content_plan,
)
from harrier.resume.markdown import (
    UnverifiedClaimError,
    resolve_bullets,
    validate_rendered_markdown,
)
from harrier.resume.ranking import rank_bullet_ids
from harrier.resume.tailor import run_tailor
from harrier.tracker import add_job, get_job

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_BUNDLE_PATH = REPO_ROOT / "config" / "resume-content.example.json"
AS_OF = date(2026, 8, 1)


def load_raw_bundle() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(EXAMPLE_BUNDLE_PATH.read_text(encoding="utf-8")))


@pytest.fixture()
def bundle() -> ResumeBundle:
    return parse_bundle(load_raw_bundle())


@pytest.fixture()
def sources(bundle: ResumeBundle) -> TruthSources:
    return TruthSources(truth_text="\n".join(bundle.bullet_pool.values()), achievements_text="")


# ---------------------------------------------------------------------------
# Visible title and URL scrubbing
# ---------------------------------------------------------------------------


def test_normalize_visible_role_title_removes_internal_tailored_label() -> None:
    title = normalize_visible_role_title(
        "exampleco",
        "Tailored for exampleco — Senior Frontend Engineer (Node/Vue/TypeScript) - Remote Europe",
    )
    assert title == "Senior Frontend Engineer (Node/Vue/TypeScript) — Remote Europe"


def test_normalize_visible_role_title_removes_company_prefix_and_remote_suffix() -> None:
    assert (
        normalize_visible_role_title(
            "examplesoft", "examplesoft — Senior Frontend Engineer (Remote)"
        )
        == "Senior Frontend Engineer"
    )


def test_slug_still_keeps_company_for_output_filename() -> None:
    slug = slugify("exampleco-Senior Frontend Engineer (Node/Vue/TypeScript) - Remote Europe")
    assert slug.startswith("exampleco-")


def test_slugify_transliterates_turkish_characters() -> None:
    assert slugify("Deniz Örnek-Şirket") == "deniz-ornek-sirket"


def test_normalize_visible_url_text_strips_scheme_for_contact_line() -> None:
    assert (
        normalize_visible_url_text("https://linkedin.com/in/deniz-ornek")
        == "linkedin.com/in/deniz-ornek"
    )


# ---------------------------------------------------------------------------
# Grounded header and HTML
# ---------------------------------------------------------------------------

REQUESTED_ROLE = (
    "Tailored for exampleco — Senior Frontend Engineer (Node/Vue/TypeScript) - Remote Europe"
)


def test_markdown_header_uses_grounded_title_not_requested_identity(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    lines = [line for line in markdown.splitlines() if line.strip()]
    assert lines[0] == "# Deniz Örnek"
    assert lines[1] == "Senior Frontend Engineer — TypeScript & Vue 3"
    assert lines[2] == "Exampleland | deniz@example.com | linkedin.com/in/deniz-ornek"
    assert "Tailored for" not in markdown
    # Against the CURRENT request. The old string named a role REQUESTED_ROLE
    # no longer contains, so it passed without testing anything (review of #50).
    assert "Node/Vue/TypeScript" not in markdown
    assert "exampleco" not in markdown


def test_a_forbidden_phrase_refuses_the_rendered_resume(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    """Tested against the validator rather than the helper (spec 034).

    An earlier version of this suite only exercised `forbidden_hits`
    directly, so removing the call from `validate_rendered_markdown` changed
    nothing and the suite still passed: a test of the helper instead of the
    decision, which is the same mistake this project keeps making.
    """
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    # A phrase the rendered document definitely contains, declared forbidden.
    banned = replace(bundle, forbidden_phrases=("Senior Frontend Engineer",))
    errors = validate_rendered_markdown(markdown, plan, banned)
    assert any("forbidden" in error for error in errors)


def test_a_clean_resume_reports_no_forbidden_phrases(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    clean = replace(bundle, forbidden_phrases=("world-class expert",))
    assert not [
        error for error in validate_rendered_markdown(markdown, plan, clean) if "forbidden" in error
    ]


def test_an_empty_required_section_refuses_the_rendered_resume(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    """An empty or drifted truth document produced a resume with both
    required sections blank, a passing one-page check, and a tracker status
    advance."""
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    emptied = markdown.replace("## EXPERIENCE", "## EXPERIENCE\n").split("## EXPERIENCE")[0]
    emptied += "## EXPERIENCE\n\n## SKILLS\n\nTypeScript\n"
    errors = validate_rendered_markdown(emptied, plan, bundle)
    assert any("empty" in error and "EXPERIENCE" in error for error in errors)


def test_html_header_uses_grounded_markdown_title(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    html = render_html(markdown, bundle, template_dir=REPO_ROOT / "templates")
    assert "Tailored for" not in html
    assert ">Senior Frontend Engineer — TypeScript &amp; Vue 3<" in html
    assert ">LinkedIn<" not in html
    assert ">linkedin.com/in/deniz-ornek<" in html
    assert 'href="https://linkedin.com/in/deniz-ornek"' in html


# ---------------------------------------------------------------------------
# Education entries (spec 059)
# ---------------------------------------------------------------------------

TWO_DEGREES: list[dict[str, str]] = [
    {"degree": "MSc, X", "school": "U1"},
    {"degree": "BSc, Y", "school": "U2"},
]


def _bundle_with_education(education: object) -> ResumeBundle:
    raw = load_raw_bundle()
    raw["education"] = education
    return parse_bundle(raw)


def test_education_entries_keep_bundle_order() -> None:
    parsed = _bundle_with_education(copy.deepcopy(TWO_DEGREES))
    assert parsed.education == (
        EducationEntry(degree="MSc, X", school="U1"),
        EducationEntry(degree="BSc, Y", school="U2"),
    )


def test_education_flat_line_list_is_refused_by_name() -> None:
    """The old shape is named, not reinterpreted: the renderer used to keep
    its first two lines and silently drop the rest."""
    with pytest.raises(
        ResumeBundleError, match=r"education must be a list of \{degree, school\} objects"
    ):
        _bundle_with_education(["MSc, X", "U1"])


def test_education_entry_missing_school_is_refused() -> None:
    with pytest.raises(ResumeBundleError, match=r"education\[0\] missing school"):
        _bundle_with_education([{"degree": "MSc, X"}])


def test_education_entry_with_empty_degree_is_refused() -> None:
    with pytest.raises(ResumeBundleError, match=r"education\[1\] missing degree"):
        _bundle_with_education([TWO_DEGREES[0], {"degree": " ", "school": "U2"}])


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"degree": "MSc, X\n## CERTIFICATIONS\nInvented Cert", "school": "U1"}, "degree"),
        ({"degree": "MSc, X", "school": "U1\r\n## TECHNICAL SKILLS"}, "school"),
        ({"degree": "MSc, X\rY", "school": "U1"}, "degree"),
    ],
)
def test_education_line_break_in_either_field_is_refused(entry: dict[str, str], field: str) -> None:
    """The markdown is line-oriented and re-parsed for the PDF: a line break
    in `degree` closed the section, dropped the school, and rendered an
    invented certification (review finding on PR #68)."""
    with pytest.raises(ResumeBundleError, match=rf"education\[0\] {field} must be a single line"):
        _bundle_with_education([entry])


@pytest.mark.parametrize("school", ["### Not A Degree", "## CERTIFICATIONS", "  ### indented"])
def test_education_school_that_looks_like_a_heading_is_refused(school: str) -> None:
    """A school opening with `### ` was read back as a second degree, and one
    opening with `## ` ended the education section."""
    with pytest.raises(
        ResumeBundleError, match=r"education\[0\] school must not start with a heading marker"
    ):
        _bundle_with_education([{"degree": "MSc, X", "school": school}])


def test_degree_starting_with_heading_marker_stays_one_entry(sources: TruthSources) -> None:
    """The degree sits behind the writer's own `### ` marker, so its text is
    free: only the line prefix is parsed."""
    parsed = _bundle_with_education([{"degree": "### odd", "school": "U1"}])
    plan = build_content_plan(parsed, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(parsed, sources, plan)
    html = render_html(markdown, parsed, template_dir=REPO_ROOT / "templates")
    assert html.count('class="education-item"') == 1
    assert ">### odd<" in html
    assert ">U1<" in html


def test_empty_education_is_valid_and_renders_an_empty_block(sources: TruthSources) -> None:
    parsed = _bundle_with_education([])
    plan = build_content_plan(parsed, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(parsed, sources, plan)
    assert "## EDUCATION\n\n## CERTIFICATIONS" in markdown
    html = render_html(markdown, parsed, template_dir=REPO_ROOT / "templates")
    assert ">Education<" in html
    assert 'class="education-item"' not in html
    assert "{{" not in html


def test_markdown_writes_each_degree_as_a_heading_then_its_school(
    sources: TruthSources,
) -> None:
    parsed = _bundle_with_education(copy.deepcopy(TWO_DEGREES))
    plan = build_content_plan(parsed, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(parsed, sources, plan)
    section = markdown.split("## EDUCATION\n", 1)[1].split("\n## ", 1)[0]
    assert section.splitlines()[:4] == ["### MSc, X", "U1", "### BSc, Y", "U2"]


def test_html_renders_every_degree_newest_first(sources: TruthSources) -> None:
    """The PDF used to carry only the first two education lines, so a second
    degree was silently dropped."""
    parsed = _bundle_with_education(copy.deepcopy(TWO_DEGREES))
    plan = build_content_plan(parsed, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(parsed, sources, plan)
    html = render_html(markdown, parsed, template_dir=REPO_ROOT / "templates")
    assert html.count('class="education-item"') == 2
    assert html.index("MSc, X") < html.index("BSc, Y")
    assert ">U1<" in html
    assert ">U2<" in html
    assert "{{" not in html


def test_html_escapes_degree_text(sources: TruthSources) -> None:
    parsed = _bundle_with_education([{"degree": "<b>x</b>", "school": "U1"}])
    plan = build_content_plan(parsed, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(parsed, sources, plan)
    html = render_html(markdown, parsed, template_dir=REPO_ROOT / "templates")
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_stale_template_with_old_education_placeholders_fails_the_render(
    bundle: ResumeBundle, sources: TruthSources, tmp_path: Path
) -> None:
    templates = REPO_ROOT / "templates"
    stale = (templates / "resume-template.html").read_text(encoding="utf-8")
    stale = stale.replace("{{education_html}}", "{{education_degree}}")
    (tmp_path / "resume-template.html").write_text(stale, encoding="utf-8")
    (tmp_path / "resume-template.css").write_text(
        (templates / "resume-template.css").read_text(encoding="utf-8"), encoding="utf-8"
    )
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    with pytest.raises(ValueError, match="education_degree"):
        render_html(markdown, bundle, template_dir=tmp_path)


# ---------------------------------------------------------------------------
# Bundle strings cannot forge the markdown's structure (spec 062)
# ---------------------------------------------------------------------------

# Every bundle string build_markdown emits, spelled as the error spells it.
EMITTED_PATHS = [
    "candidate.name",
    "candidate.location",
    "candidate.email",
    "candidate.linkedin",
    "candidate.primary_identity",
    "candidate.positioning_technologies[0]",
    "profile_summary",
    "roles[0].organization",
    "roles[0].title",
    "roles[0].employment_type",
    "all_skills[0]",
    "bullet_pool[r1_b1]",
    "certifications[0]",
    "education[0] degree",
    "education[0] school",
]

# The values that begin a markdown line with no writer marker in front.
UNMARKED_LINE_STARTS = [
    "candidate.primary_identity",
    "candidate.location",
    "all_skills[0]",
    "certifications[0]",
]

LINE_BOUNDARY_CODES = (0x0A, 0x0D, 0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)
TITLE_SEPARATOR = " \u2014 "

_PATH_STEP = re.compile(r"([a-z_]+)(?:\[([^\]]+)\])?")


def _mutated(*changes: tuple[str, str]) -> dict[str, object]:
    """The example bundle with the named strings replaced."""
    raw = load_raw_bundle()
    for path, value in changes:
        steps: list[str | int] = []
        for name, index in _PATH_STEP.findall(path):
            steps.append(name)
            if index:
                steps.append(int(index) if index.isdigit() else index)
        node: Any = raw
        for step in steps[:-1]:
            node = node[step]
        node[steps[-1]] = value
    return raw


def _render(raw: dict[str, object]) -> str:
    parsed = parse_bundle(raw)
    truth = TruthSources(truth_text="\n".join(parsed.bullet_pool.values()), achievements_text="")
    plan = build_content_plan(parsed, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(parsed, truth, plan)
    return render_html(markdown, parsed, template_dir=REPO_ROOT / "templates")


@pytest.mark.parametrize("path", EMITTED_PATHS)
def test_line_break_in_any_emitted_bundle_string_is_refused_by_name(path: str) -> None:
    """The markdown is line-oriented and the renderer parses it back, so a
    line break in any emitted string edits the document's structure. In
    `profile_summary` it forged a whole achievements section."""
    raw = _mutated((path, "Plausible\n## TECHNICAL SKILLS\nCOBOL"))
    with pytest.raises(ResumeBundleError, match=re.escape(f"{path} must be a single line")):
        parse_bundle(raw)


@pytest.mark.parametrize("code", LINE_BOUNDARY_CODES, ids=lambda code: f"U+{code:04X}")
def test_every_line_boundary_character_is_refused(code: int) -> None:
    """Both parsers split with `str.splitlines()`, which breaks on far more
    than CR and LF."""
    boundary = chr(code)
    assert len(f"a{boundary}b".splitlines()) == 2
    raw = _mutated(("certifications[0]", f"Real Cert{boundary}## TECHNICAL SKILLS{boundary}COBOL"))
    with pytest.raises(ResumeBundleError, match=r"certifications\[0\] must be a single line"):
        parse_bundle(raw)


@pytest.mark.parametrize("path", ["candidate.name", "bullet_pool[r1_b1]"])
def test_trailing_line_break_is_refused(path: str) -> None:
    """Harmless when rendered, refused anyway: one rule with no exceptions
    about where in the string the character sits."""
    raw = _mutated((path, "Deniz Örnek\n"))
    with pytest.raises(ResumeBundleError, match=re.escape(f"{path} must be a single line")):
        parse_bundle(raw)


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"degree": "MSc\u2028## CERTIFICATIONS\u2028Invented Cert", "school": "U1"}, "degree"),
        ({"degree": "MSc", "school": "U1\u2028### Invented Degree"}, "school"),
    ],
)
def test_education_line_boundary_beyond_cr_lf_is_refused(entry: dict[str, str], field: str) -> None:
    """Spec 059's guard read CR and LF only, so U+2028 in a degree still
    rendered an invented certification."""
    with pytest.raises(ResumeBundleError, match=rf"education\[0\] {field} must be a single line"):
        _bundle_with_education([entry])


def test_profile_summary_cannot_inject_an_unverified_achievement() -> None:
    """The forged section sat above the real one, so the renderer read it
    first and the truth gate, which only sees bullet IDs, never saw the
    line at all."""
    raw = _mutated(
        ("profile_summary", "Fine.\n## SELECTED ACHIEVEMENTS\n- INVENTED claim never in truth")
    )
    with pytest.raises(ResumeBundleError, match="profile_summary must be a single line"):
        parse_bundle(raw)


@pytest.mark.parametrize("value", ["## PROFILE", "### x", "   ## x"])
@pytest.mark.parametrize("path", UNMARKED_LINE_STARTS)
def test_heading_marker_at_an_unmarked_line_start_is_refused(path: str, value: str) -> None:
    raw = _mutated((path, value))
    with pytest.raises(
        ResumeBundleError, match=re.escape(f"{path} must not start with a heading marker")
    ):
        parse_bundle(raw)


def _rendered_parts(html: str) -> dict[str, list[str]]:
    """What the renderer put where, read back out of the real template."""
    patterns = {
        "name": r"<h1>(.*?)</h1>",
        "summary": r'<p class="summary">(.*?)</p>',
        "role_titles": r'<h3 class="role-title">(.*?)</h3>',
        "companies": r'<p class="company">(.*?)</p>',
        "periods": r'<p class="period">(.*?)</p>',
        "items": r"<li>(.*?)</li>",
        "skills": r'<p class="skills-inline">(.*?)</p>',
        "education": r'<div class="education-item">(.*?)</div>',
    }
    return {key: re.findall(pattern, html, flags=re.S) for key, pattern in patterns.items()}


def test_values_behind_a_writer_marker_may_start_with_hash() -> None:
    """A name, an organization, and a bullet always sit behind the writer's
    own `# `, `### `, and `- `, so only their prefix is parsed and their text
    stays free, as a degree's does."""
    raw = load_raw_bundle()
    pool = cast("dict[str, str]", raw["bullet_pool"])
    candidate = cast("dict[str, str]", raw["candidate"])
    role = cast("list[dict[str, object]]", raw["roles"])[0]
    certifications = cast("list[str]", raw["certifications"])
    changes = [("candidate.name", f"# {candidate['name']}")]
    changes.append(("roles[0].organization", f"# {role['organization']}"))
    changes.extend((f"bullet_pool[{bullet_id}]", f"# {text}") for bullet_id, text in pool.items())

    plain = _rendered_parts(_render(raw))
    marked = _rendered_parts(_render(_mutated(*changes)))

    assert marked["name"] == [f"# {plain['name'][0]}"]
    assert marked["companies"] == [f"# {plain['companies'][0]}", *plain["companies"][1:]]
    # Every bullet kept its place and gained only its marker; the
    # certifications, which share the list markup, gained nothing.
    assert [item.removeprefix("# ") for item in marked["items"]] == plain["items"]
    bullets = len(plain["items"]) - len(certifications)
    assert sum(item.startswith("# ") for item in marked["items"]) == bullets > 0
    for untouched in ("summary", "role_titles", "periods", "skills", "education"):
        assert marked[untouched] == plain[untouched]


def test_organization_containing_the_title_separator_is_refused() -> None:
    """The renderer splits the role heading on the first separator, so an
    organization carrying one lost its tail to the title."""
    raw = _mutated(("roles[0].organization", f"Acme{TITLE_SEPARATOR}Talent"))
    with pytest.raises(
        ResumeBundleError,
        match=r"roles\[0\]\.organization must not contain or end with the title separator",
    ):
        parse_bundle(raw)


@pytest.mark.parametrize("tail", [" \u2014", " \u2014 ", " \u2014\t"])
def test_organization_ending_in_the_separators_dash_is_refused(tail: str) -> None:
    """The writer trims the organization and appends its own separator, so a
    trailing dash made a doubled one: the split fell a dash early and the
    title rendered with the organization's dash in front of it (local review
    of PR #73)."""
    raw = _mutated(("roles[0].organization", f"Acme Talent{tail}"))
    with pytest.raises(
        ResumeBundleError,
        match=r"roles\[0\]\.organization must not contain or end with the title separator",
    ):
        parse_bundle(raw)


def test_organization_made_of_dashes_elsewhere_still_splits_exactly() -> None:
    """The rule is about where the first separator falls, not about the
    dash: a leading one, or one with no spaces around it, splits cleanly."""
    for organization in ("\u2014 Acme", "Acme\u2014Talent", "Acme \u2014Talent"):
        html = _render(_mutated(("roles[0].organization", organization)))
        assert f'<p class="company">{organization}</p>' in html


def test_title_containing_the_separator_stays_one_role() -> None:
    raw = load_raw_bundle()
    role = cast("list[dict[str, object]]", raw["roles"])[0]
    title = f"Engineer{TITLE_SEPARATOR}Platform"

    html = _render(_mutated(("roles[0].title", title)))

    assert html.count('class="experience-item"') == _render(raw).count('class="experience-item"')
    assert f'<p class="company">{role["organization"]}</p>' in html
    assert f'<h3 class="role-title">{title} ({role["employment_type"]})</h3>' in html


def _string_leaves(node: object, path: str = "") -> list[tuple[str, list[str | int]]]:
    """Every string value in the bundle, with a label and the steps to it."""

    def walk(
        value: object, label: str, steps: list[str | int]
    ) -> list[tuple[str, list[str | int]]]:
        if isinstance(value, str):
            return [(label, steps)]
        found: list[tuple[str, list[str | int]]] = []
        if isinstance(value, dict):
            for key, item in cast("dict[str, object]", value).items():
                found.extend(walk(item, f"{label}.{key}" if label else key, [*steps, key]))
        elif isinstance(value, list):
            for index, item in enumerate(cast("list[object]", value)):
                found.extend(walk(item, f"{label}[{index}]", [*steps, index]))
        return found

    return walk(node, path, [])


def _markdown_or_refusal(raw: dict[str, object]) -> str | None:
    """The markdown resume, or None when any gate before it refused."""
    try:
        parsed = parse_bundle(raw)
        truth = TruthSources(
            truth_text="\n".join(parsed.bullet_pool.values()), achievements_text=""
        )
        plan = build_content_plan(parsed, "", REQUESTED_ROLE, AS_OF)
        return build_markdown(parsed, truth, plan)
    except ValueError:
        return None


def test_no_bundle_string_carries_a_line_break_or_heading_into_the_markdown() -> None:
    """The list of emitted strings above is written by hand, and so is the
    validator's. This asks the writer instead: every string in the bundle, one
    at a time, is given a forged section, and none may reach the markdown as
    the start of a line. A field the writer starts emitting tomorrow fails here
    before anyone remembers to add it to either list."""
    leaves = _string_leaves(load_raw_bundle())
    assert len(leaves) > len(EMITTED_PATHS)

    forged_line = "## FORGED SECTION"
    escaped: list[str] = []
    for label, steps in leaves:
        for payload in (f"{{original}}\n{forged_line}", forged_line):
            raw = load_raw_bundle()
            node: Any = raw
            for step in steps[:-1]:
                node = node[step]
            node[steps[-1]] = payload.format(original=node[steps[-1]])
            markdown = _markdown_or_refusal(raw)
            # A prefix match, as the renderer's own section scan is: whatever
            # the writer appends after the value does not unmake the heading.
            if markdown is not None and any(
                line.startswith(forged_line) for line in markdown.splitlines()
            ):
                escaped.append(label)
    assert not escaped, f"bundle strings that forged a markdown line: {sorted(set(escaped))}"


def test_every_line_problem_is_reported_in_one_error() -> None:
    raw = _mutated(
        ("candidate.email", "deniz@example.com\n"),
        ("roles[0].title", "Engineer\n- forged bullet"),
        ("certifications[0]", "## TECHNICAL SKILLS"),
    )
    with pytest.raises(ResumeBundleError) as refused:
        parse_bundle(raw)
    message = str(refused.value)
    assert message.startswith("invalid resume content bundle: ")
    assert "candidate.email must be a single line" in message
    assert "roles[0].title must be a single line" in message
    assert "certifications[0] must not start with a heading marker" in message


# ---------------------------------------------------------------------------
# Facts and identity gating
# ---------------------------------------------------------------------------


def test_experience_years_use_completed_anniversaries(bundle: ResumeBundle) -> None:
    assert professional_experience_years(bundle, date(2025, 12, 31)) == 11
    assert professional_experience_years(bundle, date(2026, 1, 1)) == 12
    assert professional_experience_label(bundle, date(2026, 7, 21)) == "12+ years"


def test_frontend_evidence_is_not_promoted_to_full_stack_from_jd(
    bundle: ResumeBundle,
) -> None:
    title = build_presentation_title(
        bundle,
        "Senior Frontend / Full-Stack Engineer",
        "React and TypeScript; some Node.js collaboration is useful.",
        AS_OF,
    )
    assert title.startswith("Senior Frontend Engineer")
    assert "Full-Stack" not in title


def test_genuine_full_stack_evidence_can_support_full_stack_title() -> None:
    raw = load_raw_bundle()
    roles = cast("list[dict[str, object]]", raw["roles"])
    cast("list[str]", roles[1]["competencies"]).append("backend")
    roles[2]["competencies"] = ["frontend", "backend"]
    full_stack_bundle = parse_bundle(raw)
    title = build_presentation_title(
        full_stack_bundle,
        "Senior Full-Stack Engineer",
        "React, TypeScript, and backend API development.",
        AS_OF,
    )
    assert title.startswith("Senior Full-Stack Engineer")


def test_react_target_prioritizes_react_and_typescript_over_vue(
    bundle: ResumeBundle,
) -> None:
    skills = rank_skills(bundle, "React TypeScript frontend product role", as_of=AS_OF)
    assert skills.index("React") < skills.index("Vue 3")
    assert skills.index("TypeScript") < skills.index("Nuxt")


def test_vue_target_prioritizes_vue_and_nuxt_over_react(bundle: ResumeBundle) -> None:
    skills = rank_skills(bundle, "Vue 3 and Nuxt frontend product role", as_of=AS_OF)
    assert skills.index("Vue 3") < skills.index("React")
    assert skills.index("Nuxt") < skills.index("React")


def test_ended_client_engagement_never_renders_as_present(bundle: ResumeBundle) -> None:
    plan = build_content_plan(bundle, "React TypeScript", "Senior Frontend Engineer", AS_OF)
    assert plan.role_periods["r1"] == "Oct 2023 – Mar 2025"  # noqa: RUF001
    assert "Present" not in plan.role_periods["r1"]
    assert validate_content_plan(plan, bundle) == []


# ---------------------------------------------------------------------------
# Evidence ranking and plan validation
# ---------------------------------------------------------------------------


def test_quantified_evidence_outranks_generic_jd_matching_bullet(
    bundle: ResumeBundle,
) -> None:
    ranked = rank_bullet_ids(
        bundle,
        ["ach_1", "r2_b7"],
        "Build scalable product applications and onboarding experiences.",
        "Senior Frontend Engineer",
    )
    assert ranked[0] == "ach_1"


def test_unsupported_jd_technology_cannot_enter_skills(bundle: ResumeBundle) -> None:
    plan = build_content_plan(bundle, "Kubernetes platform engineer", "Platform Engineer", AS_OF)
    assert "Kubernetes" not in plan.skills
    invalid = copy.deepcopy(plan)
    invalid.skills.insert(0, "Kubernetes")
    assert "skills lack candidate evidence: Kubernetes" in validate_content_plan(invalid, bundle)


def test_plan_rejects_duplicate_evidence_across_achievements_and_experience(
    bundle: ResumeBundle,
) -> None:
    plan = build_content_plan(bundle, "React TypeScript", "Senior Frontend Engineer", AS_OF)
    invalid = copy.deepcopy(plan)
    # Force the achievement's evidence group into a role selection too.
    invalid.role_bullets["r1"] = ["r1_b2", *invalid.role_bullets["r1"]]
    invalid.selected_achievements = ["ach_4", *invalid.selected_achievements]
    errors = validate_content_plan(invalid, bundle)
    assert any("duplicates the same evidence" in error for error in errors)


def test_ai_order_cannot_add_evidence(bundle: ResumeBundle) -> None:
    plan = build_content_plan(bundle, "React TypeScript", "Senior Frontend Engineer", AS_OF)
    reordered = apply_ai_bullet_order(
        plan, bundle, {"role1_bullets": ["r1_b9", "made_up"], "selected_achievements": []}
    )
    assert set(reordered.role_bullets["r1"]) == set(plan.role_bullets["r1"])
    assert validate_content_plan(reordered, bundle) == []


def test_bullet_failing_truth_check_refuses_rather_than_omitting(bundle: ResumeBundle) -> None:
    """Changed behaviour (spec 034). This used to drop the line and render a
    shorter document, which is how an empty truth document produced a clean
    PDF with empty sections: the strictest possible truth failure looked
    exactly like success."""
    partial_sources = TruthSources(truth_text=bundle.bullet_pool["r1_b1"], achievements_text="")
    with pytest.raises(UnverifiedClaimError) as raised:
        resolve_bullets(bundle, partial_sources, ["r1_b1", "r1_b2"])
    assert raised.value.bullet_id == "r1_b2"


def test_bundle_validation_rejects_invalid_bullet_count() -> None:
    raw = load_raw_bundle()
    roles = cast("list[dict[str, object]]", raw["roles"])
    roles[0]["bullet_count"] = "4"
    with pytest.raises(ResumeBundleError, match="bullet_count"):
        parse_bundle(raw)


def test_bundle_validation_requires_verified_and_positioning_skills() -> None:
    raw = load_raw_bundle()
    raw["verified_skills"] = []
    with pytest.raises(ResumeBundleError, match="verified_skills"):
        parse_bundle(raw)
    raw = load_raw_bundle()
    cast("dict[str, object]", raw["candidate"])["positioning_technologies"] = []
    with pytest.raises(ResumeBundleError, match="positioning_technologies"):
        parse_bundle(raw)


def test_sparse_role_pool_yields_shorter_section_not_duplicate_evidence() -> None:
    # With only grouped bullets left after achievements claim their groups,
    # the plan accepts a shorter role section instead of refilling with a
    # duplicated evidence group (review finding on PR #10).
    raw = load_raw_bundle()
    pool = cast("dict[str, str]", raw["bullet_pool"])
    for bullet_id in ("r1_b4", "r1_b6"):
        del pool[bullet_id]
    roles = cast("list[dict[str, object]]", raw["roles"])
    roles[0]["default_bullets"] = ["r1_b1"]
    sparse_bundle = parse_bundle(raw)
    plan = build_content_plan(
        sparse_bundle, "architecture migration design system", "Engineer", AS_OF
    )
    assert len(plan.role_bullets["r1"]) < 4
    assert validate_content_plan(plan, sparse_bundle) == []


def test_ai_id_validation_deduplicates_before_minimum_count(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    from harrier.resume.ai import (
        _validate_bullet_ids,  # pyright: ignore[reportPrivateUsage]
    )

    repeated: list[object] = ["r1_b1", "r1_b1", "r1_b1"]
    assert _validate_bullet_ids(bundle, sources, repeated, "r1_", 3) == []


def test_ai_tailored_content_is_none_on_llm_failure_or_garbage(
    bundle: ResumeBundle, sources: TruthSources, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harrier.resume.ai as ai_module
    from harrier.llm import LLMClientError

    def raising_generate(system_prompt: str, user_input: str) -> str:
        raise LLMClientError("all auto AI providers failed")

    monkeypatch.setattr(ai_module, "generate_text", raising_generate)
    assert ai_module.build_ai_tailored_content(bundle, sources, "jd", "Co", "Role") is None

    def garbage_generate(system_prompt: str, user_input: str) -> str:
        return "not json at all"

    monkeypatch.setattr(ai_module, "generate_text", garbage_generate)
    assert ai_module.build_ai_tailored_content(bundle, sources, "jd", "Co", "Role") is None


def test_ai_tailoring_tolerates_trailing_comma(
    bundle: ResumeBundle, sources: TruthSources, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same ordering with and without the trailing commas (spec 058): the
    # `sources` fixture holds every pool bullet, so validation cannot thin
    # the result.
    import harrier.resume.ai as ai_module

    payload: dict[str, list[str]] = {
        f"role{position}_bullets": [
            bullet_id
            for bullet_id in bundle.bullet_pool
            if bullet_id.startswith(f"{role_entry.id}_")
        ]
        for position, role_entry in enumerate(bundle.roles, start=1)
    }
    payload["selected_achievements"] = [
        bullet_id for bullet_id in bundle.bullet_pool if bullet_id.startswith("ach_")
    ]
    clean = json.dumps(payload)
    with_trailing = clean.replace("]", ",]").replace("}", ",}")

    def clean_generate(_system_prompt: str, _user_input: str) -> str:
        return clean

    def trailing_generate(_system_prompt: str, _user_input: str) -> str:
        return with_trailing

    monkeypatch.setattr(ai_module, "generate_text", clean_generate)
    expected = ai_module.build_ai_tailored_content(bundle, sources, "jd", "Co", "Role")
    assert expected is not None

    monkeypatch.setattr(ai_module, "generate_text", trailing_generate)
    tolerated = ai_module.build_ai_tailored_content(bundle, sources, "jd", "Co", "Role")
    assert tolerated == expected


def test_standalone_compensation_requirement_still_raises_question(
    bundle: ResumeBundle,
) -> None:
    evaluation = evaluate_resume_fit(bundle, "Compensation range is required.", "Engineer")
    questions = cast("list[str]", evaluation["candidate_questions"])
    assert any("compensation range" in question for question in questions)


def test_bundle_validation_names_unknown_refs() -> None:
    raw = load_raw_bundle()
    cast("dict[str, str]", raw["evidence_groups"])["ghost_bullet"] = "some_group"
    with pytest.raises(ResumeBundleError, match="ghost_bullet"):
        parse_bundle(raw)


# ---------------------------------------------------------------------------
# Fit evaluation
# ---------------------------------------------------------------------------


def test_fit_evaluation_marks_architecture_strong_and_backend_partial(
    bundle: ResumeBundle,
) -> None:
    jd = (
        "Own architecture and code boundaries for a scalable React and TypeScript "
        "product; backend API ownership is useful."
    )
    evaluation = evaluate_resume_fit(bundle, jd, "Senior Product Engineer")
    dimensions = {
        str(item["dimension"]): item
        for item in cast("list[dict[str, object]]", evaluation["dimensions"])
    }
    assert dimensions["architecture and code boundaries"]["evidence_status"] == "Strong evidence"
    assert dimensions["backend/full-stack ownership"]["evidence_status"] == "Partial evidence"
    assert "r1_b2" in cast(
        "list[str]", dimensions["architecture and code boundaries"]["evidence_refs"]
    )


def test_fit_evaluation_does_not_invent_game_or_ai_experience(
    bundle: ResumeBundle,
) -> None:
    jd = (
        "Build polished interfaces for a board-game product using React. "
        "An AI-first workflow is a plus."
    )
    evaluation = evaluate_resume_fit(bundle, jd, "Colonist Product Engineer")
    dimensions = {
        str(item["dimension"]): item
        for item in cast("list[dict[str, object]]", evaluation["dimensions"])
    }
    assert dimensions["domain motivation"]["evidence_status"] == "No evidence"
    assert dimensions["AI fluency"]["evidence_status"] == "No evidence"
    questions = cast("list[str]", evaluation["candidate_questions"])
    assert any("game/board-game" in question for question in questions)


def test_fit_evaluation_does_not_assume_salary_information(bundle: ResumeBundle) -> None:
    jd = "Senior frontend engineer. Salary range and relational database experience are required."
    evaluation = evaluate_resume_fit(bundle, jd, "Senior Frontend Engineer")
    questions = cast("list[str]", evaluation["candidate_questions"])
    assert any("compensation range" in question for question in questions)
    database = next(
        item
        for item in cast("list[dict[str, object]]", evaluation["dimensions"])
        if item["dimension"] == "databases and APIs"
    )
    assert database["evidence_status"] == "No evidence"


# ---------------------------------------------------------------------------
# End-to-end tailor run and the PDF gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def tailor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(REPO_ROOT)
    conn = connect()
    raw = EXAMPLE_BUNDLE_PATH.read_text(encoding="utf-8")
    put_document(conn, "resume_data", "resume-content.json", "json", raw)
    bundle_pool = cast("dict[str, str]", cast("dict[str, object]", json.loads(raw))["bullet_pool"])
    put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(bundle_pool.values()))
    job_id = add_job(
        conn,
        {
            "company": "Example Co",
            "title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "url": "https://example.test/job",
            "source": "greenhouse",
            "status": "shortlisted",
        },
    )
    return job_id


def _fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def test_failing_pdf_gate_leaves_tracker_row_unchanged(tailor_env: int) -> None:
    conn = connect()
    before = get_job(conn, tailor_env)

    def failing_validate(pdf_path: Path, html_text: str) -> list[str]:
        return ["rendered PDF has 2 pages; expected 1"]

    with pytest.raises(RuntimeError, match="render validation failed"):
        run_tailor(
            conn,
            tailor_env,
            jd_text="React and TypeScript product role.",
            no_ai=True,
            render=_fake_render,
            validate=failing_validate,
        )
    after = get_job(conn, tailor_env)
    assert after["status"] == before["status"] == "shortlisted"
    assert after["next_action"] == before["next_action"]


def test_bundle_with_a_line_break_fails_tailor_before_any_file_is_written(
    tailor_env: int, tmp_path: Path
) -> None:
    conn = connect()
    before = get_job(conn, tailor_env)
    raw = _mutated(("certifications[0]", "Real Cert\n## TECHNICAL SKILLS\nCOBOL"))
    put_document(conn, "resume_data", "resume-content.json", "json", json.dumps(raw))
    output_dir = tmp_path / "resumes"

    with pytest.raises(ResumeBundleError, match=r"certifications\[0\] must be a single line"):
        run_tailor(
            conn,
            tailor_env,
            jd_text="React and TypeScript product role.",
            no_ai=True,
            output_dir=output_dir,
            render=_fake_render,
            validate=lambda pdf_path, html_text: [],
        )

    assert not output_dir.exists()
    assert get_job(conn, tailor_env)["status"] == before["status"] == "shortlisted"


def test_passing_pdf_gate_updates_tracker_and_writes_artifacts(tailor_env: int) -> None:
    conn = connect()

    def passing_validate(pdf_path: Path, html_text: str) -> list[str]:
        return []

    result = run_tailor(
        conn,
        tailor_env,
        jd_text="React and TypeScript product role.",
        no_ai=True,
        render=_fake_render,
        validate=passing_validate,
    )
    assert result.pdf_path.exists()
    assert result.metadata_path.exists()
    assert result.evaluation_path is not None and result.evaluation_path.exists()
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["ai_tailored"] is False
    assert "Tailored for" not in result.markdown_path.read_text(encoding="utf-8")
    assert get_job(conn, tailor_env)["status"] == "tailored_cv_requested"
