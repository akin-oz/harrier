"""The posting analysis behind a letter (spec 160).

Each test goes through the decision: `ask_text`, `analysis_violations`,
`analyse_posting`, `generate_cover_letter`, `link_line`, `experience_line`
or `posting_company`. The letter fixtures are test_apply_claims.py's.
Everything is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from test_apply_claims import (
    CHECKOUT_EVIDENCE,
    COMPANY,
    INVOICES_EVIDENCE,
    POSTING,
    ROLE,
    letter_json,
    seed,
    stub_letter,
)

import harrier.apply.letters as letters_module
import harrier.apply.postingasks as postingasks
from harrier.apply.claims import ClaimCheckError
from harrier.apply.postingasks import (
    Analysis,
    Ask,
    analyse_posting,
    analysis_violations,
    ask_text,
    experience_line,
    link_line,
    posting_company,
)
from harrier.connection import Connection
from harrier.db import connect
from harrier.resume.content import ResumeBundle
from harrier.tracks import default_scope

SECTIONED = """Design Engineer

Example Studio is hiring a design engineer.

What You'll Do
Craft every detail of new features, from an idea to the implementation
Review code with the team

What We Look For
Proficient with Figma
A strong portfolio of previous design projects

What We Offer
A flat company structure and equity
"""


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return seed(connect())


def _analysis(*asks: dict[str, object]) -> str:
    return json.dumps({"asks": list(asks), "logistics": None, "product_fact_ids": []})


def _fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def _bundle(**fields: object) -> ResumeBundle:
    values: dict[str, object] = {"links": (), "github": "", "experience_statement": ""}
    values.update(fields)
    return cast("ResumeBundle", SimpleNamespace(**values))


def test_offer_and_perks_sections_never_reach_the_analysis(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    text, dropped = ask_text(SECTIONED)
    assert "Craft every detail of new features" in text
    assert "Proficient with Figma" in text
    assert "flat company structure" not in text
    assert "is hiring a design engineer" not in text
    assert dropped == ["What We Offer"]

    seen: list[str] = []

    def analysis(system_prompt: str, user_input: str) -> str:
        seen.append(user_input)
        facts = json.loads(user_input)["facts"]
        return _analysis({"ask": "craft", "quote": "Craft every detail", "fact_ids": list(facts)})

    monkeypatch.setattr(postingasks, "generate_text", analysis)
    stub_letter(monkeypatch, letter_json())
    letters_module.generate_cover_letter(
        db, COMPANY, ROLE, jd_text=SECTIONED, scope=default_scope(db)
    )
    assert "flat company structure" not in seen[0]


def test_an_ask_quote_must_be_verbatim_and_in_order() -> None:
    text, _ = ask_text(SECTIONED)
    facts = {"t1": "Built the checkout flow."}
    out_of_order = Analysis(
        asks=(
            Ask(1, "figma", "Proficient with Figma", ("t1",)),
            Ask(2, "craft", "Craft every detail", ("t1",)),
        )
    )
    assert analysis_violations(out_of_order, text, facts) == [
        "ask 2 is out of posting order: Craft every detail"
    ]
    invented = Analysis(asks=(Ask(1, "craft", "Design every pixel", ("t9",)),))
    assert analysis_violations(invented, text, facts) == [
        "ask 1 quote not in the posting: Design every pixel",
        "ask 1 names an unknown fact id: t9",
    ]

    calls: list[str] = []

    def always_wrong(system_prompt: str, user_input: str) -> str:
        calls.append(user_input)
        return _analysis({"ask": "craft", "quote": "Design every pixel", "fact_ids": []})

    with pytest.raises(ClaimCheckError, match="quote not in the posting"):
        analyse_posting(SECTIONED, facts, "Example Studio", "Design Engineer", always_wrong)
    assert len(calls) == 2
    assert "retry" in json.loads(calls[1])


def test_the_writer_gets_only_asks_and_matched_facts(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def analysis(system_prompt: str, user_input: str) -> str:
        facts = cast("dict[str, str]", json.loads(user_input)["facts"])
        matched = [key for key, text in facts.items() if CHECKOUT_EVIDENCE in text]
        return _analysis(
            {"ask": "build products", "quote": POSTING.split()[0], "fact_ids": matched}
        )

    seen: list[str] = []

    def writer(system_prompt: str, user_input: str) -> str:
        seen.append(user_input)
        return letter_json()

    monkeypatch.setattr(postingasks, "generate_text", analysis)
    monkeypatch.setattr(letters_module, "generate_text", writer)
    letters_module.generate_cover_letter(
        db, COMPANY, ROLE, jd_text=POSTING, scope=default_scope(db)
    )
    payload = seen[0]
    assert CHECKOUT_EVIDENCE in payload
    assert INVOICES_EVIDENCE not in payload
    assert "builds invoicing tools for small firms" not in payload
    assert "job_description_text" not in payload
    assert "truth_sources" not in payload


def test_the_asks_file_is_written_beside_the_letter(
    db: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_letter(monkeypatch, letter_json())
    letter = letters_module.generate_cover_letter(
        db, COMPANY, ROLE, jd_text=POSTING, scope=default_scope(db)
    )
    directory = tmp_path / "letters"
    letters_module.write_cover_letter_artifacts(
        db,
        COMPANY,
        ROLE,
        None,
        letter.short_version,
        letter.full_version,
        output_dir=directory,
        render=_fake_render,
        validate=lambda pdf_path, html_text: [],
        kind="industry",
        asks=letter.asks,
    )
    files = list(directory.glob("*.asks.json"))
    assert len(files) == 1
    document = json.loads(files[0].read_text(encoding="utf-8"))
    assert document["company"] == COMPANY
    assert document["asks"][0]["rank"] == 1
    assert document["asks"][0]["fact_ids"]
    assert document["asks"][0]["gap"] is False
    assert f"first-hand experience of {COMPANY}'s product" in document["gaps"]


def test_the_link_line_follows_the_posting() -> None:
    text, _ = ask_text(SECTIONED)
    with_link = _bundle(links=(("Portfolio", "https://portfolio.example.dev/"),))
    assert link_line(text, with_link) == ("Portfolio: portfolio.example.dev", None)
    assert link_line("Review code with the team", with_link) == (None, None)
    assert link_line(text, _bundle()) == (None, "a portfolio link")
    assert link_line("Share your GitHub", _bundle(github="https://github.com/example")) == (
        "GitHub: github.com/example",
        None,
    )


def test_a_senior_posting_gets_the_experience_line() -> None:
    statement = "Professional career since 2015, 6+ years in software."
    bundle = _bundle(experience_statement=statement)
    assert experience_line("Senior Frontend Developer", "Build features", bundle) == (
        statement,
        None,
    )
    assert experience_line("Frontend Developer", "You have 5+ years of React", bundle) == (
        statement,
        None,
    )
    assert experience_line("Frontend Developer", "Build features", bundle) == (None, None)
    assert experience_line("Senior Frontend Developer", "Build features", _bundle()) == (
        None,
        "an experience statement",
    )


def test_the_company_name_takes_the_postings_casing() -> None:
    posting = "Restream is hiring. At Restream you ship weekly. restream.io"
    assert posting_company("restream", posting) == "Restream"
    assert posting_company("Examplesoft", "no name here") == "Examplesoft"
