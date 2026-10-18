"""A focused track's documents present a chosen smaller scope honestly (spec 140).

Every database is built under `tmp_path`, and every candidate, link, company
and posting is synthetic: the committed examples, with a synthetic GitHub and
demo link added to the facts (spec 060, ADR-008). The PDF render is faked;
the HTML is the real template's.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from resume_support import REPO_ROOT, example_documents, store_documents
from test_apply_claims import seed

import harrier.apply.letters as letters_module
import harrier.resume.tailor as tailor_module
from harrier.apply.claims import NeedsInputError
from harrier.connection import Connection
from harrier.db import connect
from harrier.profile.store import put_document
from harrier.resume.content import ResumeBundleError, TruthSources, bundle_from_documents
from harrier.resume.htmlrender import render_html
from harrier.resume.keywords import missing_keywords
from harrier.resume.markdown import build_markdown
from harrier.resume.plan import build_content_plan
from harrier.tracker import add_job
from harrier.tracks import AI_CV_SECTION, Scope, add_track, default_scope, resolve_scope, rules_for
from harrier_cli.main import main

SLUG = "bounded"
FOCUSED_FRAMING = REPO_ROOT / "config" / "resume-framing.focused.example.json"
GITHUB = "https://github.com/example-dev"
DEMO = "https://demo.example.com"
POSTING = (
    "We build product interfaces in React and TypeScript.\n"
    "You use Git every day and review pull requests.\n"
    "You turn Figma designs into accessible components."
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    # The templates resolve against the working directory, as in production.
    monkeypatch.chdir(REPO_ROOT)
    return seed(connect())


def facts_with_links() -> dict[str, Any]:
    facts, _ = example_documents()
    candidate = cast("dict[str, Any]", facts["candidate"])
    candidate["github"] = GITHUB
    candidate["links"] = [{"label": "Demo", "url": DEMO}]
    return facts


def focused_framing() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(FOCUSED_FRAMING.read_text(encoding="utf-8")))


def truth_text(framing: dict[str, Any]) -> str:
    pool = cast("dict[str, str]", framing["bullet_pool"])
    # GitHub Actions is held; Git, as a skill of its own, is not.
    return "\n".join([*pool.values(), "Built CI pipelines with GitHub Actions."])


def focused_scope(db: Connection) -> Scope:
    """A focused track holding the example framing over facts with links."""
    _, industry = example_documents()
    store_documents(db, facts_with_links(), industry)
    add_track(db, SLUG, "focused", "Bounded roles")
    scope = resolve_scope(db, SLUG)
    framing = focused_framing()
    put_document(
        db, "resume_framing", "focused.json", "json", json.dumps(framing), track_id=scope.track.id
    )
    put_document(db, "resume_truth", "truth.md", "markdown", truth_text(framing))
    return scope


def fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def tailor(db: Connection, scope: Scope, tmp_path: Path) -> tailor_module.TailorResult:
    job_id = add_job(
        db,
        {
            "company": "Example Studio",
            "title": "Frontend Developer",
            "url": f"https://jobs.example.com/{scope.track.slug}",
            "source": "manual",
            "status": "shortlisted",
        },
        scope=scope,
    )
    return tailor_module.run_tailor(
        db,
        scope,
        job_id,
        jd_text=POSTING,
        no_ai=True,
        output_dir=tmp_path / "resumes",
        render=fake_render,
        validate=lambda pdf_path, html_text: [],
    )


def sidecar(result: tailor_module.TailorResult) -> Path:
    return result.markdown_path.with_name(
        result.markdown_path.name.removesuffix(".md") + ".missing-keywords.md"
    )


# --- the industry CV -------------------------------------------------------------


def test_the_industry_cv_is_unchanged() -> None:
    """The new facts and framing fields change nothing an industry CV prints
    while the framing sets no headline. The industry markdown's own hash is
    pinned by test_industry_tailor_is_unchanged_by_kind_prompts in
    tests/test_academic_documents.py."""
    facts, framing = example_documents()
    plain = bundle_from_documents(facts, framing)
    widened_framing = dict(framing)
    widened_framing["candidate"] = {
        **cast("dict[str, Any]", framing["candidate"]),
        "entity_line": "Independent contractor, based in Exampleland (UTC+0)",
    }
    widened_framing["ai_bullets"] = ["ach_5"]
    widened = bundle_from_documents(facts_with_links(), widened_framing)
    pool = cast("dict[str, str]", framing["bullet_pool"])
    sources = TruthSources(truth_text="\n".join(pool.values()), achievements_text="")
    outputs = []
    for bundle in (plain, widened):
        plan = build_content_plan(bundle, POSTING, "Frontend Developer")
        markdown = build_markdown(bundle, sources, plan, kind="industry")
        html = render_html(markdown, bundle, REPO_ROOT / "templates", kind="industry")
        outputs.append((markdown, html))
    assert outputs[0] == outputs[1]


# --- the focused CV ----------------------------------------------------------------


def test_the_focused_header_carries_links_and_the_entity_line(
    db: Connection, tmp_path: Path
) -> None:
    result = tailor(db, focused_scope(db), tmp_path)
    framing = focused_framing()
    candidate = cast("dict[str, str]", framing["candidate"])
    lines = result.markdown_path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == candidate["headline"]
    assert (
        lines[2] == "deniz@example.com | linkedin.com/in/deniz-ornek | "
        "github.com/example-dev | demo.example.com"
    )
    assert lines[3] == candidate["entity_line"]
    html = result.html_path.read_text(encoding="utf-8")
    assert f'href="{GITHUB}"' in html
    assert f'href="{DEMO}"' in html
    assert f'<p class="header__entity">{candidate["entity_line"]}</p>' in html
    assert "+00 000 000 0000" not in html
    assert '<p class="headline">Frontend Engineer | React &amp; TypeScript</p>' in html


def test_the_focused_cv_has_an_ai_section(db: Connection, tmp_path: Path) -> None:
    result = tailor(db, focused_scope(db), tmp_path)
    markdown = result.markdown_path.read_text(encoding="utf-8")
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert headings == list(rules_for("focused").cv_sections)
    pool = cast("dict[str, str]", focused_framing()["bullet_pool"])
    ai_line = f"- {pool['ach_9']}"
    section = markdown.split(AI_CV_SECTION, 1)[1].split("## ", 1)[0]
    assert ai_line in section
    achievements = markdown.split("## SELECTED ACHIEVEMENTS", 1)[1].split("## ", 1)[0]
    assert ai_line not in achievements
    assert "AI-Assisted Engineering" in result.html_path.read_text(encoding="utf-8")


def test_a_focused_profile_is_at_most_three_sentences(
    db: Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    focused_scope(db)
    capsys.readouterr()
    framing = focused_framing()
    framing["profile_summary"] = "Works daily with AI-assisted development. Ships small, often."
    long_profile = tmp_path / "framing.json"
    long_profile.write_text(json.dumps(framing), encoding="utf-8")
    put = ["--track", SLUG, "profile", "put", "resume_framing", "--file"]
    assert main([*put, str(long_profile)]) == 1
    assert "has 4 sentences, at most 3" in capsys.readouterr().err
    assert main([*put, str(FOCUSED_FRAMING)]) == 0


# --- the missing-keywords sidecar ------------------------------------------------------


def test_an_unverified_posting_skill_goes_to_the_sidecar(db: Connection, tmp_path: Path) -> None:
    result = tailor(db, focused_scope(db), tmp_path)
    assert not re.search(r"\bGit\b", result.markdown_path.read_text(encoding="utf-8"))
    text = sidecar(result).read_text(encoding="utf-8")
    assert '- **Git**: "You use Git every day and review pull requests."' in text
    # Figma is a watch term the truth does not hold either; WCAG is not asked for.
    assert "**Figma**" in text
    assert "WCAG" not in text


def test_the_sidecar_is_focused_only(db: Connection, tmp_path: Path) -> None:
    focused_scope(db)
    result = tailor(db, default_scope(db), tmp_path)
    assert result.markdown_path.is_file()
    assert not sidecar(result).exists()


# --- the focused letter ------------------------------------------------------------------

PARAGRAPH = "I build accessible interfaces in React and TypeScript for product teams."


def letter(paragraphs: list[str]) -> dict[str, str]:
    return {"short_version": PARAGRAPH, "full_version": "\n\n".join(paragraphs)}


def test_the_focused_letter_has_six_paragraphs() -> None:
    """The name is spec 140's. Since spec 160 the focused letter holds five
    to ten blocks: the role line, two to five body paragraphs, the link and
    experience lines, the logistics block and the close."""
    six = letter(["Frontend Developer, Example Studio", *[PARAGRAPH] * 5])
    assert letters_module.cover_letter_violations(six, kind="focused") == []
    three = letters_module.cover_letter_violations(letter([PARAGRAPH] * 3), kind="focused")
    assert (
        "cover letter should contain 5 to 10 blocks, role line and close included: it has 3"
        in three
    )
    long_words = letter([" ".join([PARAGRAPH] * 5)] * 6)
    assert any(
        v.startswith("over the word limit")
        for v in letters_module.cover_letter_violations(long_words, kind="focused")
    )


def test_a_focused_letter_placeholder_blocks_the_pdf(db: Connection, tmp_path: Path) -> None:
    full = "\n\n".join([PARAGRAPH] * 5 + ["I invoice through my entity, [[TODO: tax or VAT ID]]."])
    directory = tmp_path / "letters"
    with pytest.raises(NeedsInputError) as needs:
        letters_module.write_cover_letter_artifacts(
            db,
            "Example Studio",
            "Frontend Developer",
            None,
            PARAGRAPH,
            full,
            output_dir=directory,
            template_dir=REPO_ROOT / "templates",
            render=fake_render,
            validate=lambda pdf_path, html_text: [],
            kind="focused",
        )
    assert needs.value.placeholders == ["[[TODO: tax or VAT ID]]"]
    paths = letters_module.cover_letter_paths_for("Example Studio", "Frontend Developer", directory)
    assert paths["markdown"].is_file()
    assert not paths["pdf"].exists()
    assert not paths["html"].exists()


def test_the_focused_letter_has_a_salutation(db: Connection) -> None:
    full = "\n\n".join([f"Paragraph {n}. {PARAGRAPH}" for n in range(1, 7)])
    html = letters_module.render_cover_letter_html(
        db,
        "Example Studio",
        "Frontend Developer",
        full,
        template_dir=REPO_ROOT / "templates",
        kind="focused",
    )
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL)
    assert paragraphs[0] == "Hello Example Studio team,"
    assert paragraphs[-1] == "Deniz Örnek"
    assert paragraphs[1].startswith("Paragraph 1.")
    assert paragraphs[-2].startswith("Paragraph 6.")
    # No header: the focused letter opens with the salutation.
    assert 'class="header"' not in html


# --- review of PR #343 ---------------------------------------------------------------


@pytest.mark.parametrize("key", ["ai_bullets", "watch_terms"])
def test_a_non_string_entry_is_refused_not_dropped(key: str) -> None:
    facts, _ = example_documents()
    framing = focused_framing()
    framing[key] = ["ach_9", 123]
    with pytest.raises(ResumeBundleError, match=rf"{key}\[1\] must be a non-empty string"):
        bundle_from_documents(facts, framing)


@pytest.mark.parametrize(
    ("field", "value"),
    [("links", [{"label": "Demo", "url": "javascript:alert(1)"}]), ("github", "javascript:x")],
)
def test_an_unsafe_link_scheme_is_refused(field: str, value: object) -> None:
    facts = facts_with_links()
    cast("dict[str, Any]", facts["candidate"])[field] = value
    with pytest.raises(ResumeBundleError, match="must be an http or https URL"):
        bundle_from_documents(facts, focused_framing())


def test_a_watch_term_an_alias_verifies_is_still_checked_against_the_truth() -> None:
    """An alias can verify "Git" through "GitHub Actions"; the truth names
    only GitHub Actions, so Git is still missing."""
    facts = facts_with_links()
    facts["all_skills"] = [*cast("list[str]", facts["all_skills"]), "GitHub Actions"]
    facts["verified_skills"] = [*cast("list[str]", facts["verified_skills"]), "GitHub Actions"]
    facts["technology_aliases"] = {
        **cast("dict[str, Any]", facts.get("technology_aliases", {})),
        "GitHub Actions": ["Git"],
    }
    bundle = bundle_from_documents(facts, focused_framing())
    sources = TruthSources(
        truth_text="Built CI pipelines with GitHub Actions.", achievements_text=""
    )
    found = missing_keywords(bundle, sources, "You use Git every day.")
    assert [item.term for item in found] == ["Git"]


def test_a_quoted_sentence_counts_toward_the_profile_limit(
    db: Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    focused_scope(db)
    capsys.readouterr()
    framing = focused_framing()
    framing["profile_summary"] = '"Ships small." "Ships often."'
    quoted = tmp_path / "framing.json"
    quoted.write_text(json.dumps(framing), encoding="utf-8")
    put = ["--track", SLUG, "profile", "put", "resume_framing", "--file", str(quoted)]
    assert main(put) == 1
    assert "has 4 sentences, at most 3" in capsys.readouterr().err


def test_a_later_industry_run_removes_a_stale_sidecar(db: Connection, tmp_path: Path) -> None:
    """A focused run's sidecar at the same path must not sit beside a later
    industry draft as if it were that run's."""
    focused_scope(db)
    stale = tailor_module.resume_paths_for(
        "Deniz Örnek", "Example Studio", "Frontend Developer", tmp_path / "resumes"
    )["missing_keywords"]
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# Missing keywords: from an earlier focused run\n", encoding="utf-8")
    result = tailor(db, default_scope(db), tmp_path)
    assert sidecar(result) == stale
    assert not stale.exists()
