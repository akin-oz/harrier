"""Application answers refuse negative framing, passive growth and runs of
"I" sentences (spec 167).

The model path goes through `generate_answer_set` with a stubbed provider,
the decision rather than `tone_violations` alone. Everything is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from resume_support import DEFAULT_SCOPE
from test_apply_claims import (
    CHECKOUT,
    CHECKOUT_EVIDENCE,
    COMPANY,
    GROUNDED_CLAIMS,
    INVOICES,
    INVOICES_EVIDENCE,
    POSTING,
    QUESTION,
    ROLE,
    answer,
    answers_json,
    candidate,
    seed,
    stub_sequence,
)

import harrier.apply.answers as answers_module
from harrier.apply import build_deterministic_draft, generate_answer_set
from harrier.apply.claims import ClaimCheckError
from harrier.apply.letterlint import tone_violations
from harrier.connection import Connection
from harrier.db import connect
from harrier.tracks import TRACK_KINDS


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return seed(connect())


def generate(db: Connection) -> list[answers_module.AnswerDraft]:
    return generate_answer_set(db, COMPANY, ROLE, [QUESTION], jd_text=POSTING, scope=DEFAULT_SCOPE)


def test_an_answer_with_negative_framing_is_retried(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L18 refuses the first set and names answer 1; the retry carries the
    refusal, and the set that drops the phrase is kept."""
    framed = "In the launch weeks the team had to ship twice a week."
    refused = answers_json(answer(f"{CHECKOUT} {framed} {INVOICES}", GROUNDED_CLAIMS))
    passing = answers_json(answer(f"{CHECKOUT} {INVOICES}", GROUNDED_CLAIMS))

    calls = stub_sequence(monkeypatch, answers_module, [refused, passing])
    drafts = generate(db)
    assert len(calls) == 2
    assert "answer 1 medium: negative framing: had to" in calls[1][1]
    assert drafts[0].medium_answer == f"{CHECKOUT} {INVOICES}"

    # Refused twice, the set exits with the violation (spec 085).
    stub_sequence(monkeypatch, answers_module, [refused, refused])
    with pytest.raises(ClaimCheckError) as caught:
        generate(db)
    assert "answer 1 medium: negative framing: had to" in caught.value.violations


def test_passive_growth_in_a_short_answer_is_refused(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L19 reads the short answer as well as the medium one."""
    short = "My judgment was strengthened through the billing work."
    refused = answers_json(answer(f"{CHECKOUT} {INVOICES}", GROUNDED_CLAIMS, short=short))
    stub_sequence(monkeypatch, answers_module, [refused, refused])
    with pytest.raises(ClaimCheckError) as caught:
        generate(db)
    assert "answer 1 short: passive growth: was strengthened through" in caught.value.violations


def test_i_runs_read_the_medium_answer_only(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive "I" sentences refuse a medium answer. A short answer
    of one sentence starting with "I" passes (spec 167, failure modes)."""
    built = "I built the checkout flow in TypeScript and React."
    processed = "I processed 1,200 invoices over 2025 with the billing service."
    claims = [candidate(built, CHECKOUT_EVIDENCE), candidate(processed, INVOICES_EVIDENCE)]
    run = answers_json(answer(f"{built} {processed}", claims))
    stub_sequence(monkeypatch, answers_module, [run, run])
    with pytest.raises(ClaimCheckError) as caught:
        generate(db)
    assert (
        'answer 1 medium: "I" or "My" starts 2 of 2 sentences, two in a row'
        in caught.value.violations
    )

    short = "I build product features end to end."
    passing = answers_json(answer(f"{CHECKOUT} {INVOICES}", GROUNDED_CLAIMS, short=short))
    stub_sequence(monkeypatch, answers_module, [passing])
    assert generate(db)[0].short_answer == short


def test_i_runs_split_sentences_as_the_answer_limits_do(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "e.g. my" does not end a sentence, so this medium answer is one
    sentence that opens with "The", not two with the second opening "my"."""
    medium = "The checkout work covered quality checks, e.g. my lint step on v1.2 of the service."
    stub_sequence(monkeypatch, answers_module, [answers_json(answer(medium, []))])
    assert generate(db)[0].medium_answer == medium


def test_every_answers_prompt_states_the_tone_rules() -> None:
    """The prompt states each limit the check enforces, including L20's
    third, so a draft that follows it is not refused (review of PR #424)."""
    for kind in TRACK_KINDS:
        prompt = " ".join(answers_module.answers_prompt(kind).split())
        assert "Never frame a fact negatively" in prompt, kind
        assert "Write growth in the active voice" in prompt, kind
        assert (
            'Do not start two sentences in a row with "I" or "My", and in a medium answer'
            " start at most a third of the sentences with them." in prompt
        ), kind


def test_deterministic_answers_are_not_tone_checked() -> None:
    """The deterministic path writes the operator's own templates as they
    stand, even when the model path would refuse them."""
    medium = "I had to rebuild the checkout at {company}. I shipped it in TypeScript."
    profile: dict[str, object] = {
        "deterministic_answers": {"generic": {"short": "I had to.", "medium": medium}}
    }
    draft = build_deterministic_draft("Anything to add?", "exampleco", profile, {})
    assert draft.medium_answer == medium.replace("{company}", "Exampleco")
    # The same text fails every tone rule the model path applies.
    assert len(tone_violations(draft.medium_answer, i_runs=True)) == 2
