"""The application documents take their voice, shape and page count from the
track's kind, and the truth rules stay shared (spec 101).

These tests cover the kind-dependent pieces: the prompts, the CV's section
order, the PDF gate and the letter's shape, and the passed-deadline warning.
Every company, institution, person and posting is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from resume_support import REPO_ROOT, example_bundle_raw, example_documents, store_example
from test_apply_claims import (
    CHECKOUT,
    COMPANY,
    INVOICES,
    PARAGRAPH_ONE,
    PARAGRAPH_THREE,
    POSTING,
    ROLE,
    answer,
    candidate,
    letter_json,
    seed,
)

import harrier.apply.answers as answers_module
import harrier.apply.letters as letters_module
import harrier.resume.ai as ai_module
import harrier.resume.facts as facts_module
import harrier.resume.htmlrender as htmlrender_module
import harrier.resume.markdown as markdown_module
import harrier.resume.pdf as pdf_module
import harrier.resume.ranking as ranking_module
import harrier.resume.tailor as tailor_module
import harrier.tracker.queue as queue_module
from harrier.db import connect
from harrier.profile.store import put_document
from harrier.resume.content import TruthSources, load_bundle, load_truth_sources, parse_bundle
from harrier.resume.plan import build_content_plan
from harrier.tracker import add_job, get_job
from harrier.tracker.actions import add_manually
from harrier.tracks import TRACK_KINDS, Scope, add_track, default_scope, resolve_scope
from harrier_cli.main import main

# --- the prompts ---------------------------------------------------------------

# sha256 of each prompt as main held it before spec 101 split it into a shared
# block and an industry voice (SYSTEM_PROMPT_TAILOR in resume/ai.py, and
# SYSTEM_PROMPT_BASE in apply/letters.py and apply/answers.py at 807f6cb).
INDUSTRY_PROMPT_SHA256 = {
    "tailor": "e4d6e724cbb0bca98688928bfe01dcac3f4eaa77b5d14c94f52a8bc530ec8525",
    "letter": "e12345003dc70d13af48d960dc80fbf575c78e1bb1513794dcc2c1f924a632fe",
    "answers": "5f4c2ce48f22338680ed62668bf294116c01931a608407710d7377791acb1a00",
}

# Truth rules the spec names, flattened to single spaces. Each must reach the
# model whatever the kind.
NAMED_TRUTH_RULES = {
    "tailor": ["Return only IDs from the supplied bullet_pool"],
    "letter": [
        "Every fact about the candidate must come from resume_truth_source_md or "
        "latest_project_achievements_md",
        "No invented experience",
        "Never cite job_description_text or the application profile as candidate evidence",
    ],
    "answers": [
        "Do not invent experience, tools, domains, or responsibilities",
        "Every fact about the candidate comes from resume_truth_source_md or "
        "latest_project_achievements_md",
        "Never cite job_description_text or the application profile as candidate evidence",
    ],
}


def _prompts() -> dict[str, tuple[object, tuple[str, ...]]]:
    return {
        "tailor": (ai_module.tailor_prompt, ai_module.TAILOR_SHARED),
        "letter": (letters_module.letter_prompt, letters_module.LETTER_SHARED),
        "answers": (answers_module.answers_prompt, answers_module.ANSWERS_SHARED),
    }


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_both_kinds_share_every_truth_rule() -> None:
    """One shared block per prompt, in every kind's prompt; the industry
    prompts byte for byte what they were; and the academic voice is its own."""
    for name, (build, shared) in _prompts().items():
        assert callable(build)
        prompts = {kind: cast("str", build(kind)) for kind in TRACK_KINDS}
        for kind, prompt in prompts.items():
            for segment in shared:
                assert segment in prompt, f"{name} on {kind} is missing a shared segment"
            for rule in NAMED_TRUTH_RULES[name]:
                assert rule in _flat(prompt), f"{name} on {kind} is missing {rule!r}"
        industry = hashlib.sha256(prompts["industry"].encode("utf-8")).hexdigest()
        assert industry == INDUSTRY_PROMPT_SHA256[name], f"the industry {name} prompt changed"
        assert "selection committee" in prompts["academic"]
        assert "selection committee" not in prompts["industry"]


def test_the_academic_letter_prompt_is_written_for_a_committee() -> None:
    """The academic voice covers what spec 101 lists, and no recruiter."""
    academic = letters_module.letter_prompt("academic")
    assert "recruiter" not in academic
    for topic in ("Research fit", "group or department", "Why this institution"):
        assert topic in academic
    assert "no word count" in academic
    assert "at most 240 words" in letters_module.letter_prompt("industry")


# --- the generators send their kind's prompt ----------------------------------


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.chdir(REPO_ROOT)
    return seed(connect())


def _capture(monkeypatch: pytest.MonkeyPatch, module: object, response: str) -> list[str]:
    seen: list[str] = []

    def fake(system_prompt: str, user_input: str) -> str:
        seen.append(system_prompt)
        return response

    monkeypatch.setattr(module, "generate_text", fake)
    return seen


def _scope_of_kind(conn: sqlite3.Connection, kind: str) -> Scope:
    """The default track for the industry kind; otherwise a new track of the
    kind, holding a copy of the default track's application profile, which
    every track owns for itself (spec 101)."""
    if kind == "industry":
        return default_scope(conn)
    add_track(conn, "lab", kind, "Example lab search")
    scope = resolve_scope(conn, "lab")
    rows = conn.execute(
        "SELECT name, format, content FROM profile_documents "
        "WHERE kind = 'application_profile' AND track_id = 1"
    ).fetchall()
    for name, fmt, content in rows:
        put_document(conn, "application_profile", name, fmt, content, track_id=scope.track.id)
    return scope


@pytest.mark.parametrize("kind", TRACK_KINDS)
def test_each_generator_sends_its_kinds_prompt(
    kind: str, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = _scope_of_kind(db, kind)
    letters = _capture(monkeypatch, letters_module, letter_json())
    letters_module.generate_cover_letter(db, COMPANY, ROLE, jd_text=POSTING, scope=scope)
    assert letters == [letters_module.letter_prompt(kind)]

    claims = [candidate(CHECKOUT, "Built the checkout flow in TypeScript and React")]
    answers = _capture(
        monkeypatch, answers_module, json.dumps({"answers": [answer(CHECKOUT, claims)]})
    )
    answers_module.generate_answer_set(
        db, COMPANY, ROLE, ["What relevant experience do you have?"], jd_text=POSTING, scope=scope
    )
    assert answers[0].startswith(answers_module.answers_prompt(kind))

    store_example(db)
    bundle = load_bundle(db, default_scope(db))
    ranking = _capture(monkeypatch, ai_module, "{}")
    ai_module.build_ai_tailored_content(
        bundle, load_truth_sources(db), "a posting", "Example Co", "Engineer", kind=kind
    )
    assert ranking == [ai_module.tailor_prompt(kind)]


# --- the letter's shape -------------------------------------------------------

LONG_PARAGRAPH = (
    "The research group's work on verified data pipelines is close to the checkout and "
    "billing systems I built, and the posting asks for exactly that kind of care. "
) * 4


def test_the_academic_letter_has_no_word_count_or_paragraph_count() -> None:
    """One to two pages with no word count (spec 101, decision 2): the page
    gate holds the length. The industry letter keeps its three paragraphs and
    240 words; the floor on a paragraph holds for both."""
    full = "\n\n".join(
        [PARAGRAPH_ONE, f"{CHECKOUT} {INVOICES}", *[LONG_PARAGRAPH] * 3, PARAGRAPH_THREE]
    )
    letter = {"short_version": "I would like to join the group.", "full_version": full}
    assert len(full.split()) > 240
    assert letters_module.cover_letter_violations(letter, kind="academic") == []
    industry = letters_module.cover_letter_violations(letter, kind="industry")
    assert "too many paragraphs: 6, at most 3" in industry
    assert any(item.startswith("over the word limit") for item in industry)

    stub = {
        "short_version": "Hello.",
        "full_version": f"{PARAGRAPH_ONE}\n\nToo short.\n\n{PARAGRAPH_THREE}",
    }
    for kind in TRACK_KINDS:
        assert "stub paragraph: Too short." in letters_module.cover_letter_violations(
            stub, kind=kind
        )


# --- the CV's section order ---------------------------------------------------

INDUSTRY_ORDER = [
    "## PROFILE",
    "## SELECTED ACHIEVEMENTS",
    "## EXPERIENCE",
    "## EDUCATION",
    "## CERTIFICATIONS",
    "## TECHNICAL SKILLS",
]
ACADEMIC_ORDER = [
    "## PROFILE",
    "## EDUCATION",
    "## SELECTED ACHIEVEMENTS",
    "## EXPERIENCE",
    "## CERTIFICATIONS",
    "## TECHNICAL SKILLS",
]


def _headings(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith("## ")]


def _cv(kind: str) -> tuple[str, str]:
    raw = example_bundle_raw()
    bundle = parse_bundle(raw)
    pool = cast("dict[str, str]", raw["bullet_pool"])
    sources = TruthSources(truth_text="\n".join(pool.values()), achievements_text="")
    plan = build_content_plan(bundle, "", "Senior Frontend Engineer")
    markdown = markdown_module.build_markdown(bundle, sources, plan, kind=kind)
    html = htmlrender_module.render_html(markdown, bundle, REPO_ROOT / "templates", kind=kind)
    return markdown, html


def test_the_academic_cv_puts_education_before_achievements() -> None:
    """The order comes from the kind, in the markdown and in the PDF's HTML.
    The industry CV keeps its order, and the required sections render on
    both."""
    industry_md, industry_html = _cv("industry")
    academic_md, academic_html = _cv("academic")
    assert _headings(industry_md) == INDUSTRY_ORDER
    assert _headings(academic_md) == ACADEMIC_ORDER
    # The same content, reordered: no line is lost or added.
    assert sorted(industry_md.splitlines()) == sorted(academic_md.splitlines())

    def position(html: str, label: str) -> int:
        return html.index(f'<h2 class="section-label">{label}</h2>')

    assert position(academic_html, "Education") < position(academic_html, "Selected Achievements")
    assert position(industry_html, "Education") > position(industry_html, "Experience")
    for html in (industry_html, academic_html):
        assert "{{" not in html
        for label in ("Profile", "Selected Achievements", "Experience", "Technical Skills"):
            assert f'<h2 class="section-label">{label}</h2>' in html
    raw = example_bundle_raw()
    degree = cast("list[dict[str, str]]", raw["education"])[0]["degree"]
    assert degree in academic_html


# --- the page gate ------------------------------------------------------------


def _pages(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = f"Producer: stub\nPages:          {count}\n"
        return subprocess.CompletedProcess(args=["pdfinfo"], returncode=0, stdout=stdout)

    monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)


def _fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def _tailor_job(
    conn: sqlite3.Connection,
    scope: Scope,
    *,
    company: str = "Example University",
    title: str = "Research Software Engineer",
) -> int:
    store_example(conn)
    if scope.track.id != default_scope(conn).track.id:
        # A non-default track reads only a framing of its own (spec 099).
        framing = json.dumps(example_documents()[1])
        name = f"{scope.track.kind}.json"
        put_document(conn, "resume_framing", name, "json", framing, track_id=scope.track.id)
    pool = cast("dict[str, str]", example_bundle_raw()["bullet_pool"])
    put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(pool.values()))
    return add_job(
        conn,
        {
            "company": company,
            "title": title,
            "url": "https://calls.example.test/1",
            "source": "manual",
            "status": "shortlisted",
        },
        scope=scope,
    )


def _academic_scope(conn: sqlite3.Connection) -> Scope:
    add_track(conn, "lab", "academic", "Example lab search")
    return resolve_scope(conn, "lab")


@pytest.mark.parametrize(
    ("kind", "pages", "error"),
    [
        ("academic", 3, "rendered PDF has 3 pages; expected 1 or 2"),
        ("industry", 2, "rendered PDF has 2 pages; expected 1"),
        ("academic", 2, None),
        ("industry", 1, None),
    ],
)
def test_page_gate_reads_the_kind(
    kind: str,
    pages: int,
    error: str | None,
    db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume run and the letter writer each gate on the kind's counts.
    A failed resume gate leaves the row's status as it was (spec 079)."""
    scope = _academic_scope(db) if kind == "academic" else default_scope(db)
    job_id = _tailor_job(db, scope)
    _pages(monkeypatch, pages)

    def tailor() -> None:
        tailor_module.run_tailor(
            db,
            scope,
            job_id,
            jd_text="A research posting.",
            no_ai=True,
            output_dir=tmp_path / "resumes",
            render=_fake_render,
        )

    def letter() -> None:
        letters_module.write_cover_letter_artifacts(
            db,
            COMPANY,
            ROLE,
            None,
            "I would like to join.",
            "\n\n".join([PARAGRAPH_ONE, CHECKOUT + " " + INVOICES, PARAGRAPH_THREE]),
            output_dir=tmp_path / "letters",
            template_dir=REPO_ROOT / "templates",
            render=_fake_render,
            kind=kind,
        )

    if error is None:
        tailor()
        letter()
        assert get_job(db, scope, job_id)["status"] == "tailored_cv_requested"
        return
    with pytest.raises(RuntimeError, match=error):
        tailor()
    assert get_job(db, scope, job_id)["status"] == "shortlisted"
    with pytest.raises(RuntimeError, match=error):
        letter()


# --- the industry CV is unchanged ---------------------------------------------

# sha256 of the markdown `run_tailor` wrote for this job on main before
# spec 101 (807f6cb): the synthetic example bundle, no model, and the date
# pinned, since the experience length is counted to today.
INDUSTRY_MARKDOWN_SHA256 = "f23e8803bf329f107e0c7ae9723e6fd6fca547c680634dec3ce26ad365bba937"
PINNED_TODAY = date(2026, 10, 10)


class _PinnedDate(date):
    @classmethod
    def today(cls) -> date:
        return PINNED_TODAY


def test_industry_tailor_is_unchanged_by_kind_prompts(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(facts_module, "date", _PinnedDate)
    monkeypatch.setattr(ranking_module, "date", _PinnedDate)
    scope = default_scope(db)
    job_id = _tailor_job(db, scope, company="Example Co", title="Senior Frontend Engineer")
    result = tailor_module.run_tailor(
        db,
        scope,
        job_id,
        jd_text="React and TypeScript product role.",
        no_ai=True,
        output_dir=tmp_path / "resumes",
        render=_fake_render,
        validate=lambda pdf_path, html_text: [],
    )
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert hashlib.sha256(markdown.encode("utf-8")).hexdigest() == INDUSTRY_MARKDOWN_SHA256


# --- the deadline -------------------------------------------------------------


def test_the_deadline_warning_is_decided_by_the_date() -> None:
    warn = queue_module.deadline_warning
    assert warn({"deadline": "2026-10-09"}, "2026-10-10") == (
        "warning: the deadline for this call passed on 2026-10-09"
    )
    assert warn({"deadline": "2026-10-10"}, "2026-10-10") is None
    assert warn({"deadline": "2026-11-01"}, "2026-10-10") is None
    assert warn({"deadline": ""}, "2026-10-10") is None
    assert warn({}, "2026-10-10") is None


def test_a_passed_deadline_warns_and_continues(
    db: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A warning on stderr, and the command still writes its draft. The
    question is one code answers, so no model is involved."""
    _, row = add_manually(
        db,
        default_scope(db),
        company=COMPANY,
        title=ROLE,
        url="https://example.test/jobs/late",
        deadline="2020-01-02",
    )
    assert row is not None
    posting = tmp_path / "posting.txt"
    posting.write_text(POSTING, encoding="utf-8")
    code = main(
        [
            "answers",
            "--job-id",
            row["id"],
            "--question",
            "What are your salary expectations?",
            "--jd-file",
            str(posting),
        ]
    )
    captured = capsys.readouterr()
    assert code in (0, 3)
    assert "warning: the deadline for this call passed on 2020-01-02" in captured.err
    assert "answers=" in captured.out
