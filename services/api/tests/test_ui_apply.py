"""The UI drives the artifact operations (spec 047, phase 2 of spec 042).

The pairing test here differs from the tracker's and the difference is the
point. A tracker route calls a domain function in-process, so that test
patches the function and drives both callers through it. An apply route
starts a *run*, and a run is the CLI: the route builds argv, and the argv
invokes the same subcommand an operator would type. So the pairing is proved
by taking the argv the route would build and driving it through `main`, which
is one implementation by construction rather than by convention.

The other half of this file is about what must not leak. Operator free text
goes to a file, never to argv, because argv is readable from the process
table by every other process on the machine.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.tracker.store import add_job
from harrier_api.app import create_app
from harrier_api.runs import (
    PARAMETERIZED_KINDS,
    RunManager,
    RunParams,
    build_command,
    run_inputs_dir,
    write_run_input,
)
from harrier_cli.main import build_parser, main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


def _add(company: str, title: str, slug: str) -> int:
    conn = connect()
    identifier = add_job(
        conn,
        {
            "company": company,
            "title": title,
            "url": f"https://boards.example.com/{slug}",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
    )
    conn.close()
    return identifier


@pytest.fixture
def job_id(env: Path) -> int:
    return _add("Northwind Labs", "Senior Frontend Engineer", "northwind/1")


@pytest.fixture
def other_job_id(env: Path) -> int:
    return _add("Cormorant Systems", "Staff Engineer", "cormorant/2")


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


# --- one implementation, two callers -----------------------------------------

# kind -> the domain function the CLI verb reaches, and the argv the route
# builds. If a route ever grew its own copy of an operation, the argv would
# stop reaching the function named here.
PAIRINGS = {
    "tailor": "harrier.resume.tailor.run_tailor",
    "cover-letter": "harrier.apply.generate_cover_letter",
    "answers": "harrier.apply.generate_answer_set",
    "evaluate": "harrier.offers.evaluate_offer",
}


@pytest.mark.parametrize("kind", sorted(PAIRINGS))
def test_the_route_argv_reaches_the_same_function_the_cli_verb_does(kind: str, job_id: int) -> None:
    """The argv a route builds, run as the CLI, arrives at the domain function.

    This is the spec's central claim for phase 2. It is stronger than
    asserting the verb string matches: it executes the argv.
    """
    command = build_command(kind, RunParams(job_id=job_id))
    # Drop the interpreter and module prefix; what remains is what an
    # operator would type after `harrier`.
    argv = command[3:]
    assert argv[0] == PARAMETERIZED_KINDS[kind].verb

    with patch(PAIRINGS[kind]) as domain, suppress(Exception):
        # What happens after the domain function is reached is that verb's
        # own business: a patched return value takes each one somewhere
        # different, and none of that is what this asserts.
        main(argv)

    assert domain.call_args is not None, f"{kind} argv did not reach {PAIRINGS[kind]}"


def test_every_apply_operation_has_a_route_in_the_contract(env: Path) -> None:
    """A kind without a route is an operation the page cannot start.

    Asserted against the OpenAPI document rather than the route table: the
    document is what `packages/contract` generates the client from, so a
    route missing here is a route the web app cannot call even if the server
    would answer it (ADR-005).
    """
    paths = create_app().openapi()["paths"]
    for suffix in ("resume", "cover-letter", "answers", "evaluate"):
        assert f"/apply/{{selector}}/{suffix}" in paths
    assert "/apply/{selector}/artifacts" in paths
    assert "/apply/{selector}/artifacts/{kind}" in paths


def test_this_spec_s_kinds_are_reachable_from_the_page(env: Path) -> None:
    """The four kinds this spec added, each with a route.

    That *every* kind in `PARAMETERIZED_KINDS` has one is asserted once, in
    `test_ui_outreach.py::test_every_parameterized_kind_is_reachable_from_a_page`,
    because the registry grew past this spec and the exhaustive check belongs
    wherever it can cover all of it.
    """
    paths = create_app().openapi()["paths"]
    for kind, path in {
        "tailor": "/apply/{selector}/resume",
        "cover-letter": "/apply/{selector}/cover-letter",
        "answers": "/apply/{selector}/answers",
        "evaluate": "/apply/{selector}/evaluate",
    }.items():
        assert kind in PARAMETERIZED_KINDS
        assert path in paths


# --- argv carries no free text and no injectable value -----------------------


def test_operator_free_text_never_reaches_argv_or_the_run_record(job_id: int, env: Path) -> None:
    """The property, checked in all three places a run is observable.

    The journal and RunOut do not carry `command` today. They are checked
    anyway: the rule exists so that adding `command` to either later is a
    debugging improvement rather than a disclosure.
    """
    secret = "zzqqx-recognisable-answer-text"
    manager = RunManager(journal_path=env / "data" / "runs" / "journal.jsonl")
    local = TestClient(create_app(run_manager=manager))

    response = local.post(
        f"/apply/{job_id}/answers",
        json={"questions": f"Why do you want this job? {secret}"},
        headers=auth(),
    )
    assert response.status_code == 200
    run_id = response.json()["id"]

    run = manager.get(run_id)
    assert run is not None
    assert secret not in " ".join(run.command)

    detail = local.get(f"/runs/{run_id}").json()
    assert secret not in json.dumps(detail)

    journal = env / "data" / "runs" / "journal.jsonl"
    if journal.is_file():
        assert secret not in journal.read_text(encoding="utf-8")

    # It did reach the run, through the file the argv points at.
    staged = [part for part in run.command if part.startswith("--questions-file=")]
    assert staged, "the questions never reached the run at all"


def test_a_path_that_looks_like_a_flag_is_still_a_value(job_id: int) -> None:
    """`--flag=value` form, proved by parsing rather than by reading the code.

    A two-argument form would let a value beginning with a dash be taken as
    the next flag. The parser is the authority on whether it was.
    """
    command = build_command("tailor", RunParams(job_id=job_id, input_path=Path("-rf.txt")))
    args = build_parser().parse_args(command[3:])
    assert args.jd_file == "-rf.txt"
    assert args.job_id == job_id


def test_a_job_selector_must_be_a_positive_integer() -> None:
    """The one selector that reaches argv cannot be made to look like a flag."""
    with pytest.raises(ValueError, match="positive integer"):
        RunParams(job_id=-1)
    with pytest.raises(ValueError, match="positive integer"):
        RunParams(job_id=0)


def test_a_kind_refuses_a_flag_its_verb_does_not_accept(job_id: int) -> None:
    with pytest.raises(ValueError, match="does not accept --no-ai"):
        build_command("answers", RunParams(job_id=job_id, switches=frozenset({"--no-ai"})))


# --- the lock is per target --------------------------------------------------


def _manager(env: Path) -> RunManager:
    """A manager whose runs block until released, so 'active' is observable."""
    return RunManager(
        journal_path=env / "journal.jsonl",
        kind_commands={"discovery": ["sleep", "5"], "demo": ["sleep", "5"]},
    )


def test_two_jobs_tailor_concurrently_while_one_job_twice_joins_the_run(env: Path) -> None:
    async def scenario() -> tuple[str, str, str]:
        manager = _manager(env)
        with patch.object(manager, "_execute", side_effect=_never_finishes):
            first = await manager.start("tailor", RunParams(job_id=1))
            second = await manager.start("tailor", RunParams(job_id=2))
            again = await manager.start("tailor", RunParams(job_id=1))
        return first.id, second.id, again.id

    first, second, again = asyncio.run(scenario())
    assert first != second, "two different jobs must not share one run"
    assert again == first, "the same job twice must join the run already in flight"


async def _never_finishes(run: object) -> None:
    """A run that stays active, so the lock is what the test observes."""
    await asyncio.sleep(3600)


def test_a_parameterless_kind_still_allows_only_one_at_a_time(env: Path) -> None:
    """The existing behaviour for discovery and demo is unchanged."""

    async def scenario() -> tuple[str, str]:
        manager = _manager(env)
        with patch.object(manager, "_execute", side_effect=_never_finishes):
            first = await manager.start("discovery")
            second = await manager.start("discovery")
        return first.id, second.id

    first, second = asyncio.run(scenario())
    assert first == second


# --- the input file does not outlive the run ---------------------------------


def test_the_input_file_is_removed_when_the_run_succeeds(env: Path) -> None:
    path = write_run_input("questions the operator typed")
    assert path.is_file()

    async def scenario() -> None:
        manager = RunManager(
            journal_path=env / "journal.jsonl",
            kind_commands={"demo": ["true"]},
        )
        run = await manager.start("tailor", RunParams(job_id=1, input_path=path))
        await manager.wait(run.id)

    with patch(
        "harrier_api.runs.build_command",
        return_value=["true"],
    ):
        asyncio.run(scenario())
    assert not path.exists(), "the operator's words outlived the run that consumed them"


def test_the_input_file_is_removed_when_the_run_is_cancelled(env: Path) -> None:
    """Cancelling is the path most likely to leak, because it skips the
    ordinary end of the run."""
    path = write_run_input("questions the operator typed")

    async def scenario() -> str:
        manager = RunManager(journal_path=env / "journal.jsonl", grace_seconds=0.2)
        run = await manager.start("tailor", RunParams(job_id=1, input_path=path))
        await manager.cancel(run.id)
        await manager.wait(run.id)
        finished = manager.get(run.id)
        return finished.state if finished is not None else ""

    with patch("harrier_api.runs.build_command", return_value=["sleep", "30"]):
        state = asyncio.run(scenario())
    assert state == "cancelled"
    assert not path.exists(), "a cancelled run left the operator's words on disk"


def test_the_input_file_is_removed_when_the_run_fails_to_spawn(env: Path) -> None:
    path = write_run_input("questions the operator typed")

    async def scenario() -> None:
        manager = RunManager(journal_path=env / "journal.jsonl")
        run = await manager.start("tailor", RunParams(job_id=1, input_path=path))
        await manager.wait(run.id)

    with patch(
        "harrier_api.runs.build_command",
        return_value=["/nonexistent/harrier-should-not-exist"],
    ):
        asyncio.run(scenario())
    assert not path.exists()


def test_an_attempt_that_joins_an_active_run_leaves_no_file_behind(env: Path) -> None:
    """The leak this catches happens on a double click, not on an error."""
    first_path = write_run_input("first")
    second_path = write_run_input("second")

    async def scenario() -> None:
        manager = _manager(env)
        with patch.object(manager, "_execute", side_effect=_never_finishes):
            await manager.start("tailor", RunParams(job_id=1, input_path=first_path))
            await manager.start("tailor", RunParams(job_id=1, input_path=second_path))

    asyncio.run(scenario())
    assert first_path.is_file(), "the run that is actually in flight lost its input"
    assert not second_path.exists(), "the joined attempt left its file behind"


def test_a_staged_input_file_is_owner_readable_only(env: Path) -> None:
    path = write_run_input("notes about why this job")
    assert path.stat().st_mode & 0o077 == 0, "another local user can read the operator's words"
    assert path.parent == run_inputs_dir()


def test_the_staged_file_is_private_from_the_moment_it_exists(env: Path) -> None:
    """The mode is part of creation, not applied afterwards.

    The test above passed while the file was created at the umask's mode and
    only then chmodded, which on a common umask of 022 left the operator's
    words world-readable for the width of that window. Checking the mode
    after the fact cannot see a window, so this asserts the mode was
    requested at open time (review finding on PR #51).
    """
    seen: list[int] = []
    real_open = os.open

    def recording_open(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        seen.append(mode)
        return real_open(path, flags, mode, **kwargs)  # pyright: ignore[reportArgumentType]

    with patch("harrier_api.runs.os.open", side_effect=recording_open):
        write_run_input("notes about why this job")

    assert seen, "the file was not created through os.open, so no mode was requested"
    assert all(mode & 0o077 == 0 for mode in seen), f"created with {[oct(m) for m in seen]}"


def test_the_staged_directory_is_not_traversable_by_other_users(env: Path) -> None:
    """Belt to the file mode's braces: no path to the file either."""
    write_run_input("notes")
    assert run_inputs_dir().stat().st_mode & 0o077 == 0


def test_a_pre_existing_directory_is_tightened_rather_than_trusted(env: Path) -> None:
    """`mkdir(mode=...)` is ignored when the directory already exists, and an
    install that predates this fix has a 0755 one sitting there."""
    directory = run_inputs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o755)

    write_run_input("notes")
    assert directory.stat().st_mode & 0o077 == 0


# --- artifacts ---------------------------------------------------------------


def test_an_unknown_artifact_kind_is_refused(client: TestClient, job_id: int) -> None:
    response = client.get(f"/apply/{job_id}/artifacts/not-a-kind", headers=auth())
    assert response.status_code == 404
    assert "unknown artifact kind" in response.json()["detail"]


@pytest.mark.parametrize(
    "kind",
    ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "resume-pdf/../../secrets"],
)
def test_a_path_shaped_kind_is_refused_as_a_kind_rather_than_read(
    client: TestClient, job_id: int, kind: str
) -> None:
    """There is no traversal defence to get wrong, because there is no path.

    The kind is a closed set and the check runs before anything builds a
    path, so a traversal attempt is simply an unknown kind.
    """
    response = client.get(f"/apply/{job_id}/artifacts/{kind}", headers=auth())
    assert response.status_code == 404
    assert "passwd" not in response.text


def test_a_missing_artifact_names_the_operation_that_produces_it(
    client: TestClient, job_id: int
) -> None:
    response = client.get(f"/apply/{job_id}/artifacts/cover-letter-pdf", headers=auth())
    assert response.status_code == 404
    assert "run cover-letter" in response.json()["detail"]


def test_the_artifact_index_lists_absent_kinds_rather_than_omitting_them(
    client: TestClient, job_id: int
) -> None:
    response = client.get(f"/apply/{job_id}/artifacts", headers=auth())
    assert response.status_code == 200
    kinds = {item["kind"]: item for item in response.json()}
    assert "cover-letter-pdf" in kinds
    assert kinds["cover-letter-pdf"]["exists"] is False
    assert kinds["cover-letter-pdf"]["produced_by"] == "cover-letter"


def test_an_artifact_is_served_with_its_own_media_type(client: TestClient, job_id: int) -> None:
    from harrier.apply.letters import cover_letter_paths_for

    paths = cover_letter_paths_for("Northwind Labs", "Senior Frontend Engineer")
    paths["markdown"].parent.mkdir(parents=True, exist_ok=True)
    paths["markdown"].write_text("# a letter\n", encoding="utf-8")

    response = client.get(f"/apply/{job_id}/artifacts/cover-letter-markdown", headers=auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "a letter" in response.text


def test_the_reader_finds_what_the_writer_wrote(client: TestClient, job_id: int) -> None:
    """The writer and the reader share one naming helper (spec 047).

    A second copy of the slug would pass every test that writes through the
    reader's own helper, which is why this one writes through the writer's.
    """
    from harrier.apply.answers import write_output

    written = write_output("Northwind Labs", "Senior Frontend Engineer", "# answers\n")
    response = client.get(f"/apply/{job_id}/artifacts/answers", headers=auth())
    assert response.status_code == 200
    assert "answers" in response.text
    assert written.is_file()


# --- the token boundary ------------------------------------------------------


def test_an_artifact_read_without_the_token_is_refused(client: TestClient, job_id: int) -> None:
    """This read requires the token although tracker reads do not (spec 047)."""
    assert client.get(f"/apply/{job_id}/artifacts").status_code == 403
    assert client.get(f"/apply/{job_id}/artifacts/answers").status_code == 403


def test_a_tracker_read_still_does_not_need_the_token(client: TestClient, job_id: int) -> None:
    """The asymmetry is deliberate, so it is pinned rather than assumed."""
    assert client.get("/tracker/queue").status_code == 200
    assert client.get("/jobs").status_code == 200


@pytest.mark.parametrize("suffix", ["resume", "cover-letter", "answers", "evaluate"])
def test_starting_an_operation_without_the_token_is_refused(
    client: TestClient, job_id: int, suffix: str
) -> None:
    assert client.post(f"/apply/{job_id}/{suffix}", json={}).status_code == 403


def test_an_unknown_selector_is_refused_before_anything_is_staged(
    client: TestClient, env: Path
) -> None:
    """A file staged for a job that does not exist would never be collected."""
    response = client.post("/apply/98765/answers", json={"questions": "anything"}, headers=auth())
    assert response.status_code == 404
    staged = run_inputs_dir()
    assert not staged.exists() or not list(staged.iterdir())
