"""The application brief shapes and constrains a letter and answers, and
every draft ends with what to check (spec 066).

The tests go through the decisions: `generate_cover_letter`,
`generate_answer_set`, the artifact writer, and the CLI. Briefs, postings
and truth documents are synthetic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from resume_support import DEFAULT_SCOPE, example_bundle_raw, store_example
from test_apply_claims import (
    CHECKOUT,
    COMPANY,
    GROUNDED_CLAIMS,
    INVOICES,
    PARAGRAPH_ONE,
    PARAGRAPH_THREE,
    PLACEHOLDER,
    POSTING,
    REPO_ROOT,
    ROLE,
    answer,
    candidate,
    employer,
    letter_json,
    seed,
    stub_answers,
    stub_letter,
)

import harrier.apply.answers as answers_module
import harrier.apply.letters as letters_module
from harrier.apply import generate_answer_set, generate_cover_letter, write_cover_letter_artifacts
from harrier.apply.brief import (
    EMPTY_BRIEF,
    Brief,
    BriefError,
    load_brief,
    parse_brief,
    store_brief,
)
from harrier.apply.claims import ClaimCheckError, NeedsInputError
from harrier.apply.requirements import Flag
from harrier.apply.review import Review
from harrier.db import connect
from harrier.profile.store import get_document, put_document
from harrier.resume.tailor import run_tailor
from harrier.tracker.actions import add_manually
from harrier.tracks import default_scope
from harrier_cli.main import main

EXPERIENCE = "What relevant experience do you have?"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return seed(connect())


def brief(**fields: object) -> Brief:
    return parse_brief(fields)


def letter(db: sqlite3.Connection, with_brief: Brief = EMPTY_BRIEF, posting: str = POSTING):
    return generate_cover_letter(
        db, COMPANY, ROLE, jd_text=posting, brief=with_brief, scope=DEFAULT_SCOPE
    )


def refusal(db: sqlite3.Connection, with_brief: Brief, posting: str = POSTING) -> str:
    with pytest.raises(ClaimCheckError) as caught:
        letter(db, with_brief, posting)
    return str(caught.value)


def model_must_not_be_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(system_prompt: str, user_input: str) -> str:
        raise AssertionError("this question must not reach the model")

    monkeypatch.setattr(answers_module, "generate_text", fail)


def capture_letter_call(monkeypatch: pytest.MonkeyPatch, response: str) -> dict[str, str]:
    seen: dict[str, str] = {}

    def fake(system_prompt: str, user_input: str) -> str:
        seen["system"] = system_prompt
        seen["user"] = user_input
        return response

    monkeypatch.setattr(letters_module, "generate_text", fake)
    return seen


def add_job(db: sqlite3.Connection, url: str = "https://example.com/jobs/1") -> str:
    _, row = add_manually(db, default_scope(db), company=COMPANY, title=ROLE, url=url)
    assert row is not None
    return row["id"]


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def markdown_from(out: str) -> str:
    line = next(line for line in out.splitlines() if line.startswith(("markdown=", "answers=")))
    return Path(line.split("=", 1)[1]).read_text(encoding="utf-8")


# --- storing the brief ------------------------------------------------------


DOCKER_TRUTH_LINE = "Ran production services in Docker containers."


def seed_resume(db: sqlite3.Connection) -> None:
    """The synthetic resume bundle, with a truth document holding every
    bullet and one line naming Docker, which the bundle's skills omit. That
    is the example brief's confirmed skill (spec 071 O9)."""
    store_example(db)
    pool = cast("dict[str, str]", example_bundle_raw()["bullet_pool"])
    truth = "\n".join([*pool.values(), DOCKER_TRUTH_LINE])
    put_document(db, "resume_truth", "truth.md", "markdown", truth)


def test_a_brief_round_trips_through_put_document(db: sqlite3.Connection) -> None:
    seed_resume(db)
    text = (REPO_ROOT / "config" / "application-brief.example.json").read_text(encoding="utf-8")
    stored = store_brief(db, 7, text)
    assert get_document(db, "application_brief", "7") is not None
    assert load_brief(db, 7) == stored
    assert stored.letter.max_words == 200
    assert stored.view_for("which tool do you dislike most") == "The operator's own view."


def test_brief_set_refuses_an_unknown_key(
    db: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = add_job(db)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"never_name": ["Acme"], "salary": "EUR 1"}), encoding="utf-8")
    code, _, err = run_cli(["brief", "set", job_id, "--file", str(path)], capsys)
    assert code == 1
    assert "unknown brief keys: salary" in err
    assert get_document(db, "application_brief", job_id) is None


def test_brief_set_refuses_a_wrong_type(
    db: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = add_job(db)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"letter": {"max_words": "200"}}), encoding="utf-8")
    code, _, err = run_cli(["brief", "set", job_id, "--file", str(path)], capsys)
    assert code == 1
    assert "letter.max_words must be a positive integer" in err
    assert get_document(db, "application_brief", job_id) is None


# --- B1: names that must not appear ------------------------------------------


def test_a_never_name_in_the_letter_refuses_it(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(
        monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} I did that work for Northwind Retail.")
    )
    message = refusal(db, brief(never_name=["Northwind Retail"]))
    assert "named a redacted name: Northwind Retail" in message


def test_a_never_name_in_an_answer_note_refuses_the_set(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_answers(
        monkeypatch,
        [answer(CHECKOUT, GROUNDED_CLAIMS[:1], notes=["Mention the Northwind Retail project."])],
    )
    with pytest.raises(ClaimCheckError, match="named a redacted name: Northwind Retail"):
        generate_answer_set(
            db,
            COMPANY,
            ROLE,
            [EXPERIENCE],
            jd_text=POSTING,
            brief=brief(never_name=["Northwind Retail"]),
            scope=DEFAULT_SCOPE,
        )


def test_never_name_matches_case_insensitively_on_word_boundaries(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = brief(never_name=["Fabrikam"])
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} The client was fabrikam."))
    assert "named a redacted name: Fabrikam" in refusal(db, hidden)

    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} It ran in Fabrikamville."))
    assert letter(db, hidden).full_version


def test_the_never_name_list_reaches_the_prompt(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = capture_letter_call(monkeypatch, letter_json())
    letter(db, brief(never_name=["Northwind Retail"]))
    assert "Northwind Retail" in seen["system"]
    assert json.loads(seen["user"])["never_name"] == ["Northwind Retail"]


# --- B2: stated limits ---------------------------------------------------------


def test_a_letter_over_the_stated_word_limit_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(monkeypatch, letter_json())
    message = refusal(db, brief(letter={"max_words": 40}))
    assert "over the stated limit: letter.max_words 40" in message


def test_an_answer_over_the_stated_sentence_limit_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_answers(monkeypatch, [answer(f"{CHECKOUT} {INVOICES}", GROUNDED_CLAIMS)])
    with pytest.raises(ClaimCheckError) as caught:
        generate_answer_set(
            db,
            COMPANY,
            ROLE,
            [EXPERIENCE],
            jd_text=POSTING,
            brief=brief(answers={"max_sentences": 1}),
            scope=DEFAULT_SCOPE,
        )
    assert "over the stated limit: answers.max_sentences 1, answer 1 medium has 2" in str(
        caught.value
    )


def test_a_two_paragraph_brief_accepts_two_paragraphs(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    two = brief(letter={"paragraphs": 2})
    stub_letter(monkeypatch, letter_json(last=""))
    assert letter(db, two).full_version.count("\n\n") == 1

    stub_letter(monkeypatch, letter_json())
    assert "over the stated limit: letter.paragraphs 2, full_version has 3" in refusal(db, two)


def test_abbreviations_and_versions_do_not_split_sentences(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = "I checked the build, e.g. the lint step, on v1.2 of the billing service."
    stub_answers(monkeypatch, [answer(medium, [])])
    drafts = generate_answer_set(
        db,
        COMPANY,
        ROLE,
        [EXPERIENCE],
        jd_text=POSTING,
        brief=brief(answers={"max_sentences": 1}),
        scope=DEFAULT_SCOPE,
    )
    assert drafts[0].medium_answer == medium


# --- B4: operator evidence and employer guidance --------------------------------

ROTA = "I ran the on-call rota for the billing service."
ROTA_EVIDENCE = "Ran the on-call rota for the billing service"


def test_brief_evidence_verifies_only_for_its_own_job(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_brief(db, 1, json.dumps({"evidence": [ROTA_EVIDENCE]}))
    claims = [*GROUNDED_CLAIMS, candidate(ROTA, ROTA_EVIDENCE)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {ROTA}", claims))
    assert ROTA in letter(db, load_brief(db, 1)).full_version
    assert "unverified evidence" in refusal(db, load_brief(db, 2))


GUIDANCE = "We read every letter ourselves and reply within two weeks."


def test_employer_guidance_reaches_the_prompt(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = capture_letter_call(monkeypatch, letter_json())
    letter(db, brief(employer_guidance=GUIDANCE))
    assert json.loads(seen["user"])["employer_guidance"] == GUIDANCE
    assert "employer_guidance" in seen["system"]


def test_employer_guidance_verifies_an_employer_claim(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "Examplesoft reads every letter itself."
    claims = [*GROUNDED_CLAIMS, employer(sentence, "We read every letter ourselves")]
    stub_letter(monkeypatch, letter_json(first=f"{sentence} {PARAGRAPH_ONE}", claims=claims))
    assert sentence in letter(db, brief(employer_guidance=GUIDANCE)).full_version
    assert "employer evidence not in posting" in refusal(db, EMPTY_BRIEF)


# --- B5 and B6: hard requirements ---------------------------------------------------

REQUIREMENTS = {
    "time_zone": "You need four hours of overlap with CET working hours.",
    "travel": "Expect to travel to Lisbon twice a year.",
    "visa_sponsorship": "We cannot sponsor visas for this role.",
    "work_authorization": "You must be authorized to work in the EU.",
}


@pytest.mark.parametrize("kind", sorted(REQUIREMENTS))
def test_each_requirement_kind_is_flagged(
    kind: str,
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(db)
    posting = tmp_path / "posting.txt"
    posting.write_text(f"{POSTING} {REQUIREMENTS[kind]}", encoding="utf-8")
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} {PLACEHOLDER}"))
    code, out, _ = run_cli(["cover-letter", "--job-id", job_id, "--jd-file", str(posting)], capsys)
    assert code == 3
    assert f'- Flag ({kind}): "{REQUIREMENTS[kind]}"' in markdown_from(out)


def test_a_posting_without_requirements_has_no_flags(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(db)
    posting = tmp_path / "posting.txt"
    posting.write_text(POSTING, encoding="utf-8")
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} {PLACEHOLDER}"))
    _, out, _ = run_cli(["cover-letter", "--job-id", job_id, "--jd-file", str(posting)], capsys)
    assert "Flag (" not in markdown_from(out)


def test_a_work_authorization_question_is_not_sent_to_the_model(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_must_not_be_called(monkeypatch)
    posting = f"{POSTING} {REQUIREMENTS['work_authorization']}"
    drafts = generate_answer_set(
        db,
        COMPANY,
        ROLE,
        ["Are you authorized to work in the EU?"],
        jd_text=posting,
        scope=DEFAULT_SCOPE,
    )
    assert drafts[0].medium_answer == "[[TODO: your answer]]"
    assert drafts[0].notes == [f'Flag (work_authorization): "{REQUIREMENTS["work_authorization"]}"']


# --- B7: opinions -----------------------------------------------------------------

OPINION = "Which tool do you dislike most?"
VIEW = "I dislike tools that hide their configuration."


def test_an_opinion_question_without_a_view_is_a_placeholder(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_must_not_be_called(monkeypatch)
    drafts = generate_answer_set(db, COMPANY, ROLE, [OPINION], jd_text=POSTING, scope=DEFAULT_SCOPE)
    assert drafts[0].medium_answer == "[[TODO: your own view]]"


def test_love_working_here_is_not_an_opinion_question(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = "Why would you love working here?"
    stub_answers(monkeypatch, [answer(CHECKOUT, GROUNDED_CLAIMS[:1]) | {"question": question}])
    drafts = generate_answer_set(
        db, COMPANY, ROLE, [question], jd_text=POSTING, scope=DEFAULT_SCOPE
    )
    assert drafts[0].medium_answer == CHECKOUT


def test_a_supplied_view_is_sent_and_counts_as_evidence(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake(system_prompt: str, user_input: str) -> str:
        seen.update(json.loads(user_input))
        claims = [candidate(VIEW, VIEW)]
        return json.dumps({"answers": [answer(VIEW, claims) | {"question": OPINION}]})

    monkeypatch.setattr(answers_module, "generate_text", fake)
    with_view = brief(views={OPINION: VIEW})
    drafts = generate_answer_set(
        db, COMPANY, ROLE, [OPINION], jd_text=POSTING, brief=with_view, scope=DEFAULT_SCOPE
    )
    assert seen["operator_views"] == {OPINION: VIEW}
    assert drafts[0].medium_answer == VIEW


# --- B8: compensation ---------------------------------------------------------------

SALARY = "What are your salary expectations?"
PAID_POSTING = f"{POSTING} The salary range is €60,000 - €75,000 per year."


def test_the_salary_answer_quotes_the_posted_range(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_must_not_be_called(monkeypatch)
    drafts = generate_answer_set(
        db,
        COMPANY,
        ROLE,
        [SALARY],
        jd_text=PAID_POSTING,
        brief=brief(compensation_number="EUR 12,345"),
        scope=DEFAULT_SCOPE,
    )
    assert drafts[0].medium_answer == (
        "Draft for you to edit. This is not advice. "
        "Posted range: €60,000 - €75,000. My number: EUR 12,345."
    )


def test_the_salary_question_is_not_sent_to_the_model(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[list[str]] = []

    def fake(system_prompt: str, user_input: str) -> str:
        sent.append(json.loads(user_input)["questions"])
        return json.dumps({"answers": [answer(CHECKOUT, GROUNDED_CLAIMS[:1])]})

    monkeypatch.setattr(answers_module, "generate_text", fake)
    drafts = generate_answer_set(
        db, COMPANY, ROLE, [SALARY, EXPERIENCE], jd_text=PAID_POSTING, scope=DEFAULT_SCOPE
    )
    assert sent == [[EXPERIENCE]]
    assert drafts[0].question == SALARY
    assert drafts[1].medium_answer == CHECKOUT


def test_salary_without_range_or_number_says_so_and_asks(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_must_not_be_called(monkeypatch)
    drafts = generate_answer_set(db, COMPANY, ROLE, [SALARY], jd_text=POSTING, scope=DEFAULT_SCOPE)
    assert drafts[0].medium_answer == (
        "Draft for you to edit. This is not advice. "
        "Posted range: none in the posting. My number: [[TODO: your number]]"
    )


# --- B9 and B10: what to check, and one next action -------------------------------------

TRAVEL = Flag("travel", REQUIREMENTS["travel"])


def write(
    db: sqlite3.Connection, tmp_path: Path, full: str, review: Review
) -> tuple[Path, dict[str, Path] | None]:
    """Write the letter; return the markdown path and the artifacts, or None
    when the draft stopped on a placeholder."""

    def render(html_text: str, pdf_path: Path) -> None:
        pdf_path.write_bytes(b"%PDF-1.4\n")

    def validate(pdf_path: Path, html_text: str) -> list[str]:
        return []

    markdown_path = tmp_path / "examplesoft-senior-product-engineer.md"
    try:
        paths = write_cover_letter_artifacts(
            db,
            COMPANY,
            ROLE,
            None,
            "I build product features.",
            full,
            output_dir=tmp_path,
            template_dir=REPO_ROOT / "templates",
            render=render,
            validate=validate,
            review=review,
            kind="industry",
        )
    except NeedsInputError:
        return markdown_path, None
    return markdown_path, paths


def next_actions(markdown: str) -> list[str]:
    tail = markdown.split("## Next action", 1)[1]
    return [line for line in tail.splitlines() if line.strip()]


def test_the_letter_markdown_ends_with_verify_and_next_action(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_letter(monkeypatch, letter_json())
    draft = letter(db)
    path, _ = write(db, tmp_path, draft.full_version, Review(claims=draft.claims))
    markdown = path.read_text(encoding="utf-8")
    verify = markdown.index("## To verify")
    action = markdown.index("## Next action")
    assert markdown.index("## Full Version") < verify < action
    assert f'- Claim: "{INVOICES}" rests on:' in markdown
    assert "- Number: 1,200" in markdown
    assert markdown.rstrip().endswith(f"Read {path} once against the posting.")


def test_the_review_lists_a_number_in_inline_code(db: sqlite3.Connection, tmp_path: Path) -> None:
    """Spec 113: the checklist reads the same number tokens as C5."""
    path, _ = write(db, tmp_path, f"{PARAGRAPH_ONE} I led `12` engineers.", Review())
    assert "- Number: 12" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("full", "flags", "expected"),
    [
        (f"{PARAGRAPH_ONE} {PLACEHOLDER}", (TRAVEL,), f'Replace "{PLACEHOLDER}" in'),
        (
            PARAGRAPH_ONE,
            (TRAVEL,),
            f'Decide your answer to the travel requirement: "{TRAVEL.sentence}".',
        ),
        (PARAGRAPH_ONE, (), "once against the posting."),
    ],
    ids=["placeholder-first", "then-a-flag", "then-reading"],
)
def test_next_action_priority(
    full: str, flags: tuple[Flag, ...], expected: str, db: sqlite3.Connection, tmp_path: Path
) -> None:
    path, _ = write(db, tmp_path, full, Review(flags=flags))
    assert expected in next_actions(path.read_text(encoding="utf-8"))[0]


def test_there_is_exactly_one_next_action(db: sqlite3.Connection, tmp_path: Path) -> None:
    second = "[[TODO: the team size]]"
    path, _ = write(
        db, tmp_path, f"{PARAGRAPH_ONE} {PLACEHOLDER} {second}", Review(flags=(TRAVEL,))
    )
    assert len(next_actions(path.read_text(encoding="utf-8"))) == 1


def test_the_letter_html_has_no_review_section_or_flag(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    path, paths = write(db, tmp_path, PARAGRAPH_ONE, Review(flags=(TRAVEL,)))
    assert paths is not None
    assert "## To verify" in path.read_text(encoding="utf-8")
    html = paths["html"].read_text(encoding="utf-8")
    for absent in ("To verify", "Next action", TRAVEL.sentence, "Flag ("):
        assert absent not in html


def test_the_answers_markdown_ends_with_verify_and_next_action(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model_must_not_be_called(monkeypatch)
    job_id = add_job(db)
    posting = tmp_path / "posting.txt"
    posting.write_text(f"{POSTING} {REQUIREMENTS['travel']}", encoding="utf-8")
    code, out, _ = run_cli(
        [
            "answers",
            "--job-id",
            job_id,
            "--question",
            "Are you willing to travel?",
            "--jd-file",
            str(posting),
        ],
        capsys,
    )
    assert code == 3
    markdown = markdown_from(out)
    assert "- Placeholder: [[TODO: your answer]]" in markdown
    assert f'- Flag (travel): "{REQUIREMENTS["travel"]}"' in markdown
    assert next_actions(markdown) == [
        f'Replace "[[TODO: your answer]]" in {out.split("answers=", 1)[1].splitlines()[0]}.'
    ]


# --- the CLI ------------------------------------------------------------------------------


def test_cli_cover_letter_uses_the_brief_for_its_job(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = add_job(db)
    brief_file = tmp_path / "brief.json"
    brief_file.write_text(json.dumps({"never_name": ["Northwind Retail"]}), encoding="utf-8")
    assert run_cli(["brief", "set", job_id, "--file", str(brief_file)], capsys)[0] == 0
    posting = tmp_path / "posting.txt"
    posting.write_text(POSTING, encoding="utf-8")
    stub_letter(
        monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} I did that work for Northwind Retail.")
    )
    code, _, err = run_cli(["cover-letter", "--job-id", job_id, "--jd-file", str(posting)], capsys)
    assert code == 1
    assert "named a redacted name: Northwind Retail" in err


# --- confirmed skills (spec 071 O9) -----------------------------------------


def _fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def _skills_line(markdown_path: Path) -> str:
    text = markdown_path.read_text(encoding="utf-8")
    return text.split("## TECHNICAL SKILLS\n", 1)[1].splitlines()[0]


def test_confirmed_skill_enters_skills_for_its_job_only(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    seed_resume(db)
    confirmed = int(add_job(db, "https://example.com/jobs/confirmed"))
    _, other_row = add_manually(
        db,
        default_scope(db),
        company="Other Example Co",
        title=ROLE,
        url="https://example.com/jobs/other",
    )
    assert other_row is not None
    other = int(other_row["id"])
    store_brief(db, confirmed, json.dumps({"confirmed_skills": ["Docker"]}))

    def run(job_id: int) -> str:
        result = run_tailor(
            db,
            default_scope(db),
            job_id,
            jd_text="React and TypeScript product role.",
            no_ai=True,
            render=_fake_render,
            validate=lambda pdf_path, html_text: [],
            output_dir=tmp_path / f"resumes-{job_id}",
        )
        return _skills_line(result.markdown_path)

    assert "Docker" in run(confirmed)
    assert "Docker" not in run(other)


def test_confirmed_skill_without_source_is_refused(db: sqlite3.Connection) -> None:
    seed_resume(db)
    with pytest.raises(BriefError, match="not found in all_skills or the truth documents: Haskell"):
        store_brief(db, 7, json.dumps({"confirmed_skills": ["Docker", "Haskell"]}))
    assert get_document(db, "application_brief", "7") is None


def test_a_confirmed_skill_must_be_a_whole_word_in_the_truth(db: sqlite3.Connection) -> None:
    # "duct" sits inside "production" in the truth line. A substring match
    # would accept a skill the candidate never named.
    seed_resume(db)
    with pytest.raises(BriefError, match="duct"):
        store_brief(db, 7, json.dumps({"confirmed_skills": ["duct"]}))
