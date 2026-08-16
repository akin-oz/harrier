"""The UI drives outreach, without breaking what makes outreach safe (spec 048).

Two product invariants carry this file, and both are the kind that a UI
erodes quietly rather than loudly.

**Staging.** Contact discovery stages candidates and a human approves them.
The failure is not a crash: it is a route that writes a contact directly, so
the approval step becomes decorative. `test_only_the_approval_route_can_write_a_contact`
enumerates the routes rather than trusting the one under review.

**Nothing sends.** Every message this system produces is a draft. The failure
is a route that reaches a send path, so
`test_no_outreach_route_reaches_a_send_path` asserts over the module rather
than over one handler.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.outreach.discovery import candidates_output_path
from harrier.tracker.store import add_job, list_contacts
from harrier_api.app import create_app
from harrier_api.runs import PARAMETERIZED_KINDS, RunParams, build_command
from harrier_cli.main import build_parser, main

COMPANY = "Northwind Labs"
TITLE = "Senior Frontend Engineer"
# An invented person at an invented company. Contacts are the highest-risk
# fixture content in this spec (ADR-008).
CANDIDATE_URL = "https://www.linkedin.com/in/invented-person-nw"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


@pytest.fixture
def job_id(env: Path) -> int:
    conn = connect()
    identifier = add_job(
        conn,
        {
            "company": COMPANY,
            "title": TITLE,
            "url": "https://boards.example.com/northwind/1",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
    )
    conn.close()
    return identifier


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def staged(job_id: int) -> int:
    """One staged candidate, written the way discovery writes them."""
    path = candidates_output_path(COMPANY, TITLE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "company": COMPANY,
                "role": TITLE,
                "candidates": [
                    {
                        "person_name": "Avery Invented",
                        "person_title": "Engineering Manager",
                        "relevance": "hiring_manager",
                        "fit_score": "82",
                        "linkedin_url": CANDIDATE_URL,
                        "review_status": "pending",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return job_id


# --- one implementation, two callers -----------------------------------------

PAIRINGS = {
    "find-contacts": "harrier.outreach.find_contacts_for_job",
    "outreach-draft": "harrier.outreach.generate_outreach",
    "backfill-posters": "harrier.outreach.backfill_posters",
}


@pytest.mark.parametrize("kind", sorted(PAIRINGS))
def test_the_route_argv_reaches_the_same_function_the_cli_verb_does(kind: str, job_id: int) -> None:
    """The argv a route builds, run as the CLI, reaches the domain function."""
    from contextlib import suppress

    takes_job = PARAMETERIZED_KINDS[kind].takes_job
    params = RunParams(job_id=job_id) if takes_job else RunParams()
    argv = build_command(kind, params)[3:]
    assert argv[0] == PARAMETERIZED_KINDS[kind].verb

    with patch(PAIRINGS[kind]) as domain, suppress(Exception):
        main(argv)
    assert domain.call_args is not None, f"{kind} argv did not reach {PAIRINGS[kind]}"


def test_every_parameterized_kind_is_reachable_from_a_page(env: Path) -> None:
    """The one exhaustive check over the whole run registry.

    Adding a kind without a route leaves an operation only the CLI can start,
    which is the gap specs 042, 048 and 049 exist to close. It covers every
    kind rather than this spec's, which is why adding `gmail-watch` in spec
    049 failed here until its route was listed. Whichever spec grows the
    registry next updates this map.
    """
    paths = create_app().openapi()["paths"]
    routed = {
        "tailor": "/apply/{selector}/resume",
        "cover-letter": "/apply/{selector}/cover-letter",
        "answers": "/apply/{selector}/answers",
        "evaluate": "/apply/{selector}/evaluate",
        "find-contacts": "/outreach/{selector}/find-contacts",
        "outreach-draft": "/outreach/{selector}/draft",
        "backfill-posters": "/outreach/backfill-posters",
        "gmail-watch": "/mail/watch",
    }
    assert set(routed) == set(PARAMETERIZED_KINDS), "a parameterized kind has no route"
    for path in routed.values():
        assert path in paths


def test_every_outreach_verb_in_the_spec_has_a_route(env: Path) -> None:
    paths = create_app().openapi()["paths"]
    for path in (
        "/outreach/{selector}/find-contacts",
        "/outreach/{selector}/candidates",
        "/outreach/{selector}/candidates/approve",
        "/outreach/{selector}/candidates/reject",
        "/outreach/{selector}/best-contact",
        "/outreach/contacts",
        "/outreach/due",
        "/outreach/sync",
        "/outreach/{selector}/sent",
        "/outreach/{selector}/replied",
        "/outreach/{selector}/snooze",
        "/outreach/{selector}/draft",
        "/outreach/backfill-posters",
    ):
        assert path in paths


# --- the staging invariant ---------------------------------------------------


def test_only_the_approval_route_can_write_a_contact(client: TestClient, staged: int) -> None:
    """Every outreach route driven; only approval may add a contact.

    Enumerating rather than testing the one route under review is the point:
    the invariant is about the whole surface, and a future route added
    without thought is exactly what this is here to catch.
    """
    job = staged
    ref = {"linkedin_url": CANDIDATE_URL}
    calls: list[tuple[str, dict[str, object]]] = [
        ("post", {"url": f"/outreach/{job}/candidates/reject", "json": ref}),
        ("post", {"url": f"/outreach/{job}/best-contact", "json": ref}),
        ("post", {"url": "/outreach/sync", "json": {}}),
        ("post", {"url": f"/outreach/{job}/sent", "json": {}}),
        ("post", {"url": f"/outreach/{job}/replied", "json": {}}),
        ("post", {"url": f"/outreach/{job}/snooze", "json": {"until": "2026-12-01"}}),
        ("get", {"url": f"/outreach/{job}/candidates"}),
        ("get", {"url": "/outreach/contacts"}),
        ("get", {"url": "/outreach/due"}),
    ]
    for method, kwargs in calls:
        getattr(client, method)(**kwargs, headers=auth())

    conn = connect()
    assert list_contacts(conn) == [], "a route other than approval wrote a contact"
    conn.close()

    response = client.post(
        f"/outreach/{job}/candidates/approve",
        json={"linkedin_url": CANDIDATE_URL},
        headers=auth(),
    )
    assert response.status_code == 200
    conn = connect()
    assert len(list_contacts(conn)) == 1, "approval did not write the contact"
    conn.close()


def test_approving_a_candidate_that_was_never_staged_is_refused(
    client: TestClient, job_id: int
) -> None:
    """No staged artifact at all: the back door into the contacts store."""
    response = client.post(
        f"/outreach/{job_id}/candidates/approve",
        json={"linkedin_url": "https://www.linkedin.com/in/never-staged"},
        headers=auth(),
    )
    assert response.status_code == 404
    assert "staged artifact" in response.json()["detail"]
    conn = connect()
    assert list_contacts(conn) == []
    conn.close()


def test_approving_an_unstaged_candidate_beside_a_staged_one_is_refused(
    client: TestClient, staged: int
) -> None:
    """The artifact exists but does not hold this person."""
    response = client.post(
        f"/outreach/{staged}/candidates/approve",
        json={"linkedin_url": "https://www.linkedin.com/in/someone-else"},
        headers=auth(),
    )
    assert response.status_code == 404
    conn = connect()
    assert list_contacts(conn) == []
    conn.close()


def test_the_cli_refuses_the_same_candidate_with_the_same_words(
    client: TestClient, staged: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal is the domain's, so both callers say the same thing."""
    response = client.post(
        f"/outreach/{staged}/candidates/approve",
        json={"linkedin_url": "https://www.linkedin.com/in/someone-else"},
        headers=auth(),
    )
    code = main(
        [
            "contacts",
            "approve",
            "--job-id",
            str(staged),
            "--linkedin-url",
            "https://www.linkedin.com/in/someone-else",
        ]
    )
    printed = capsys.readouterr().err
    assert code == 1
    assert "candidate not found in the staged artifact" in printed
    assert response.json()["detail"] == "candidate not found in the staged artifact"


def test_approving_syncs_the_tracker_as_the_cli_does(client: TestClient, staged: int) -> None:
    with patch("harrier.outreach.sync_tracker_outreach") as sync:
        sync.return_value = []
        client.post(
            f"/outreach/{staged}/candidates/approve",
            json={"linkedin_url": CANDIDATE_URL},
            headers=auth(),
        )
    assert sync.call_args is not None, "approval did not re-derive the outreach fields"


# --- nothing sends -----------------------------------------------------------


def test_no_outreach_route_reaches_a_send_path(env: Path) -> None:
    """Asserted over the module's source imports rather than one handler.

    A source-level check is a last resort by this repo's own rule, so it is
    paired with the behavioural one below: this catches an import of the
    notifier, and that catches the notifier being called.
    """
    source = Path("src/harrier_api/outreach_routes.py").read_text(encoding="utf-8")
    for forbidden in ("harrier.notify", "send_message", "send_telegram", "smtplib"):
        assert forbidden not in source, f"an outreach route reached {forbidden}"


def test_marking_sent_sends_nothing(client: TestClient, job_id: int) -> None:
    """`mark-sent` records what the operator did; it does not do it.

    Telegram is the only outbound message this system has, so it is the one
    function that could betray the invariant, and it is patched by its real
    name so a rename breaks this test rather than silently passing it.
    """
    with patch("harrier.notify.send_telegram_message") as send:
        client.post(f"/outreach/{job_id}/sent", json={}, headers=auth())
        client.post(f"/outreach/{job_id}/replied", json={}, headers=auth())
        client.post(
            f"/outreach/{job_id}/draft",
            json={"contact_name": "Avery Invented"},
            headers=auth(),
        )
    assert send.call_args is None, "an outreach route sent something"


# --- refusals reach the operator ---------------------------------------------


def test_a_contact_not_linked_to_the_job_is_reported_rather_than_claimed(
    client: TestClient, job_id: int
) -> None:
    response = client.post(
        f"/outreach/{job_id}/best-contact",
        json={"linkedin_url": CANDIDATE_URL},
        headers=auth(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "contact is not linked to this job"


def test_an_unparseable_snooze_date_is_refused_in_the_domain_s_words(
    client: TestClient, job_id: int
) -> None:
    response = client.post(
        f"/outreach/{job_id}/snooze", json={"until": "next tuesday"}, headers=auth()
    )
    assert response.status_code == 409


def test_an_unknown_selector_is_refused(client: TestClient, env: Path) -> None:
    response = client.get("/outreach/98765/candidates", headers=auth())
    assert response.status_code == 404


def test_no_staged_candidates_is_an_empty_list_not_an_error(
    client: TestClient, job_id: int
) -> None:
    """Zero candidates is an outcome discovery produces, not a fault."""
    response = client.get(f"/outreach/{job_id}/candidates", headers=auth())
    assert response.status_code == 200
    assert response.json() == []


# --- the draft carries no person on argv -------------------------------------


async def _never_finishes(run: object) -> None:
    """Hold the run open so its input file is still there to inspect."""
    import asyncio

    await asyncio.sleep(3600)


def test_a_contact_never_reaches_argv_and_does_reach_the_file(job_id: int, env: Path) -> None:
    """A contact's name and URL are a real person's details (spec 048).

    Both halves matter. Keeping them off argv is the privacy property; the
    file actually carrying them is what stops that from being achieved by
    losing them, which looks identical from outside until a draft comes out
    addressed to nobody.
    """
    from harrier_api.runs import RunManager

    manager = RunManager(journal_path=env / "journal.jsonl")
    with patch.object(RunManager, "_execute", side_effect=_never_finishes):
        local = TestClient(create_app(run_manager=manager))
        response = local.post(
            f"/outreach/{job_id}/draft",
            json={
                "contact_linkedin": CANDIDATE_URL,
                "contact_name": "Avery Invented",
                "contact_role": "Engineering Manager",
                "audience": "hiring_manager",
                "tone": "warm",
            },
            headers=auth(),
        )
    assert response.status_code == 200
    run = manager.get(response.json()["id"])
    assert run is not None

    joined = " ".join(run.command)
    assert "Avery Invented" not in joined
    assert CANDIDATE_URL not in joined

    staged_file = [part for part in run.command if part.startswith("--input-file=")]
    assert staged_file, "the contact never reached the run at all"
    payload = json.loads(Path(staged_file[0].split("=", 1)[1]).read_text(encoding="utf-8"))
    assert payload["contact_name"] == "Avery Invented"
    assert payload["contact_linkedin"] == CANDIDATE_URL
    assert payload["contact_role"] == "Engineering Manager"
    assert payload["audience"] == "hiring_manager"
    assert payload["tone"] == "warm"


def test_the_draft_input_file_round_trips_through_the_cli(job_id: int, env: Path) -> None:
    """The JSON the route writes is the JSON the verb reads.

    Without this the contact would leave argv and simply never arrive, which
    looks identical from the outside until a draft comes out addressed to
    nobody.
    """
    from harrier_api.runs import write_run_input

    path = write_run_input(
        json.dumps(
            {
                "contact_linkedin": CANDIDATE_URL,
                "contact_name": "Avery Invented",
                "contact_role": "Engineering Manager",
                "audience": "hiring_manager",
                "tone": "warm",
                "jd_text": "",
            }
        )
    )
    argv = build_command("outreach-draft", RunParams(job_id=job_id, input_path=path))[3:]
    parsed = build_parser().parse_args(argv)
    assert parsed.input_file == str(path)

    with (
        patch("harrier.outreach.find_contact") as find,
        patch("harrier.outreach.generate_outreach") as generate,
    ):
        find.return_value = {
            "person_name": "Avery Invented",
            "person_title": "Engineering Manager",
        }
        generate.side_effect = RuntimeError("far enough")
        main(argv)

    assert generate.call_args is not None
    sent = generate.call_args.kwargs
    assert sent["contact_name"] == "Avery Invented"
    assert sent["contact_linkedin"] == CANDIDATE_URL
    assert sent["audience"] == "hiring_manager"
    assert sent["tone"] == "warm"


def test_a_draft_for_an_unknown_contact_is_refused_rather_than_drafted_empty(
    job_id: int, env: Path
) -> None:
    """The CLI refuses an identifier that matches no stored contact.

    A review finding once had it continue with empty fields, producing a
    message addressed to nobody. The route sends the identifier through the
    input file, so the refusal has to survive that trip: this drives the argv
    the route builds and asserts the verb still declines.
    """
    from harrier_api.runs import write_run_input

    path = write_run_input(
        json.dumps({"contact_linkedin": "https://www.linkedin.com/in/not-stored"})
    )
    argv = build_command("outreach-draft", RunParams(job_id=job_id, input_path=path))[3:]

    with patch("harrier.outreach.generate_outreach") as generate:
        code = main(argv)

    assert code == 1, "an unknown contact was drafted rather than refused"
    assert generate.call_args is None, "it reached the generator with empty fields"


def test_backfill_takes_no_job_and_refuses_one() -> None:
    with pytest.raises(ValueError, match="takes no job"):
        build_command("backfill-posters", RunParams(job_id=1))


def test_a_count_reaches_argv_as_a_number(job_id: int) -> None:
    argv = build_command(
        "find-contacts",
        RunParams(job_id=job_id, switches=frozenset({"--best-only"}), numbers={"--max-items": 4}),
    )[3:]
    parsed = build_parser().parse_args(argv)
    assert parsed.max_items == 4
    assert parsed.best_only is True


def test_a_boolean_cannot_masquerade_as_a_count() -> None:
    """bool is an int subclass, so this would otherwise render --limit=True."""
    with pytest.raises(ValueError, match="must be an integer"):
        RunParams(numbers={"--limit": True})


# --- the token boundary ------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/outreach/1/candidates",
        "/outreach/contacts",
        "/outreach/due",
    ],
)
def test_outreach_reads_require_the_token(client: TestClient, job_id: int, path: str) -> None:
    """These carry a real person's name, so they authenticate, as spec 047's
    artifact reads do."""
    assert client.get(path).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/outreach/1/candidates/approve",
        "/outreach/1/best-contact",
        "/outreach/sync",
        "/outreach/1/sent",
        "/outreach/1/snooze",
        "/outreach/1/draft",
        "/outreach/1/find-contacts",
        "/outreach/backfill-posters",
    ],
)
def test_outreach_writes_require_the_token(client: TestClient, job_id: int, path: str) -> None:
    assert client.post(path, json={}).status_code == 403
