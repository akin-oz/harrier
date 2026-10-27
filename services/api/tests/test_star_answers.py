"""A competency question is answered in situation, task, action, result
order (spec 166).

Every check goes through `generate_answer_set` with the provider stubbed, so
removing a check from the decision fails its test. Everything is synthetic.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from resume_support import DEFAULT_SCOPE
from test_apply_claims import (
    CHECKOUT,
    CHECKOUT_EVIDENCE,
    COMPANY,
    INVOICES,
    INVOICES_EVIDENCE,
    POSTING,
    ROLE,
    answer,
    candidate,
    seed,
    stub_answers,
)

import harrier.apply.answers as answers_module
from harrier.apply import classify_question, generate_answer_set
from harrier.apply.answers import AnswerDraft, render_markdown
from harrier.apply.brief import EMPTY_BRIEF, Brief, parse_brief
from harrier.apply.claims import ClaimCheckError
from harrier.connection import Connection
from harrier.db import connect

COMPETENCY = "Describe a time when you improved how your team shipped work."
EXPERIENCE = "What relevant experience do you have?"
# Spec 167: each part is one sentence, so the filler does not open with "I"
# or the joined answer would be a run of "I" sentences (L20).
FILLER = "Together with the team we went through each step and wrote down what had changed"
CHECKOUT_CLAIM = candidate(CHECKOUT, CHECKOUT_EVIDENCE)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return seed(connect())


def words(count: int) -> str:
    """Exactly `count` words of plain first-person filler, with no number or
    technology in it for the claim checks to read."""
    return " ".join(itertools.islice(itertools.cycle(FILLER.split()), count)) + "."


def star(situation: int, task: int, action: int, result: int) -> dict[str, str]:
    """Four parts of these word counts. The action opens with a sentence the
    truth sources back, so the claims are checked on a competency answer too."""
    checkout = len(CHECKOUT.split())
    return {
        "situation": words(situation),
        "task": words(task),
        "action": f"{CHECKOUT} {words(action - checkout)}",
        "result": words(result),
    }


def competency_answer(parts: dict[str, str]) -> dict[str, object]:
    item = answer("The model's own medium answer, which code ignores.", [CHECKOUT_CLAIM])
    return {**item, "question": COMPETENCY, "star": parts}


def answers_for(
    db: Connection, questions: list[str], brief: Brief = EMPTY_BRIEF
) -> list[AnswerDraft]:
    return generate_answer_set(
        db, COMPANY, ROLE, questions, jd_text=POSTING, brief=brief, scope=DEFAULT_SCOPE
    )


def refusal(db: Connection, brief: Brief = EMPTY_BRIEF) -> str:
    with pytest.raises(ClaimCheckError) as caught:
        answers_for(db, [COMPETENCY], brief)
    return str(caught.value)


def test_competency_questions_are_classified() -> None:
    phrases = [
        "describe a time",
        "describe a situation",
        "tell us about a time",
        "tell me about a time",
        "give an example",
        "give us an example",
        "provide an example",
        "share an example",
        "an example of when",
        "a time when",
    ]
    for phrase in phrases:
        question = f"Please {phrase.upper()} you made a positive contribution to a team."
        assert classify_question(question) == "competency", phrase
    assert classify_question("What is your notice period?") == "availability"
    # Availability is checked first, so it is not misread (Failure modes).
    assert classify_question("Give an example of your notice period") == "availability"
    # Word boundaries: a phrase inside a longer word does not match.
    assert classify_question("Describe a timeline for the project") == "generic"


def test_the_action_carries_the_answer(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_answers(monkeypatch, [competency_answer(star(120, 30, 100, 80))])
    refused = refusal(db)
    assert (
        f'S2: answer 1 ("{COMPETENCY}") action has 100 words, not more than its situation (120)'
        in refused
    )

    stub_answers(monkeypatch, [competency_answer(star(30, 30, 200, 80))])
    [draft] = answers_for(db, [COMPETENCY])
    assert draft.star is not None
    assert len(draft.star["action"].split()) == 200


def test_every_star_part_is_required(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    parts = {**star(30, 30, 200, 80), "result": ""}
    stub_answers(monkeypatch, [competency_answer(parts)])
    refused = refusal(db)
    assert f'S1: answer 1 ("{COMPETENCY}") has an empty result' in refused


def test_a_part_that_is_not_text_is_empty(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """A list is not printed as "['...']" prose: it counts as empty (review
    of PR #423)."""
    parts: dict[str, object] = {**star(30, 30, 200, 80), "result": ["Shipped the change."]}
    stub_answers(monkeypatch, [{**competency_answer(star(30, 30, 200, 80)), "star": parts}])
    refused = refusal(db)
    assert f'S1: answer 1 ("{COMPETENCY}") has an empty result' in refused


def test_the_brief_limit_replaces_the_star_range(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_answers(monkeypatch, [competency_answer(star(25, 25, 140, 60))])
    refused = refusal(db)
    assert f'S4: answer 1 ("{COMPETENCY}") medium answer has 250 words, not 300 to 500' in refused

    [draft] = answers_for(db, [COMPETENCY], parse_brief({"answers": {"max_words": 250}}))
    assert len(draft.medium_answer.split()) == 250


def test_the_medium_answer_joins_the_parts(
    db: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parts = star(30, 25, 200, 80)
    seen: dict[str, object] = {}

    def fake(system_prompt: str, user_input: str) -> str:
        seen.update(json.loads(user_input))
        return json.dumps({"answers": [competency_answer(parts)]})

    monkeypatch.setattr(answers_module, "generate_text", fake)
    [draft] = answers_for(db, [COMPETENCY])

    assert seen["competency_questions"] == [COMPETENCY]
    assert draft.medium_answer == " ".join(
        parts[part] for part in ("situation", "task", "action", "result")
    )
    assert draft.short_answer == "The billing work is close to what I have shipped."

    markdown = render_markdown(COMPANY, ROLE, None, None, [draft], review_path=tmp_path / "a.md")
    drafts_section, review_section = markdown.split("## To verify")
    assert "The model's own medium answer" not in markdown
    assert draft.medium_answer in drafts_section
    assert "situation" not in drafts_section
    assert f'- STAR: "{COMPETENCY}"\n' in review_section
    for part, count in (("situation", 30), ("task", 25), ("action", 200), ("result", 80)):
        assert f"  - {part}: {count} words\n" in review_section


def test_other_questions_are_unchanged(
    db: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    medium = f"{CHECKOUT} {INVOICES}"
    item = answer(medium, [CHECKOUT_CLAIM, candidate(INVOICES, INVOICES_EVIDENCE)])
    # A stray "star" on another question is not read.
    stub_answers(monkeypatch, [{**item, "star": star(30, 30, 200, 80)}])

    # Two sentences pass: S1 to S4 do not apply, so 300 words is not asked for.
    [draft] = answers_for(db, [EXPERIENCE])
    assert draft == AnswerDraft(
        question=EXPERIENCE,
        short_answer="The billing work is close to what I have shipped.",
        medium_answer=medium,
        notes=[],
        claims=draft.claims,
    )
    assert draft.star is None

    markdown = render_markdown(COMPANY, ROLE, None, None, [draft], review_path=tmp_path / "a.md")
    assert "STAR" not in markdown

    # The checks every answer had still refuse it.
    with pytest.raises(ClaimCheckError, match=r"answers\.max_sentences 1, answer 1 medium has 2"):
        answers_for(db, [EXPERIENCE], parse_brief({"answers": {"max_sentences": 1}}))
