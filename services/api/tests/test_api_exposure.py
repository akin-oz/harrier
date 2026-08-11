"""What an unauthenticated caller reaches, and how easily (spec 035).

The API binds locally and has no accounts, which is reasonable for a
single-user tool. What made it a finding is that two properties turned it
from local-process-only into browser-reachable: a state-changing route that
answered GET, so any page could fire it with an image tag, and no
trusted-host check, so a page could rebind its own hostname to 127.0.0.1 and
speak to it as same-origin.

These tests are the two closures and the smaller leaks alongside them.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import TEST_TOKEN, auth
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.discovery import APIFY_MAX_COUNT, scheduled_apify_count
from harrier.screening.normalized import NormalizedJob
from harrier.sources import scrub_secrets
from harrier.tracker.store import list_jobs
from harrier_api.app import create_app
from harrier_api.localauth import TOKEN_HEADER, token_matches


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


# --- the rebinding closure ---------------------------------------------------


def test_a_request_with_a_foreign_host_is_refused(client: TestClient) -> None:
    """DNS rebinding is the attack this closes, and it defeats every other
    browser protection at once: the page resolves its own hostname to
    127.0.0.1 and then it *is* same-origin, so nothing about cookies, CORS or
    a token saves you. The Host header still carries the attacker's name.
    """
    response = client.get("/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:8000"])
def test_a_local_host_is_allowed(client: TestClient, host: str) -> None:
    assert client.get("/health", headers={"Host": host}).status_code == 200


def test_the_rebinding_check_applies_to_writes_too(client: TestClient) -> None:
    response = client.post(
        "/runs", json={"kind": "demo"}, headers={**auth(), "Host": "evil.example.com"}
    )
    assert response.status_code == 400


# --- the token on state-changing requests ------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/runs", {"kind": "demo"}),
        ("post", "/runs/does-not-exist/cancel", None),
        ("put", "/config/feeds", {"value": []}),
        ("delete", "/config/feeds", None),
        ("post", "/capture/add", {"company": "A", "title": "T"}),
    ],
)
def test_a_state_changing_request_without_the_token_is_refused(
    client: TestClient, method: str, path: str, payload: dict[str, object] | None
) -> None:
    call = getattr(client, method)
    response = call(path, json=payload) if payload is not None else call(path)
    assert response.status_code == 403, f"{method.upper()} {path} accepted an untokened request"


def test_a_wrong_token_is_refused(client: TestClient) -> None:
    response = client.post("/runs", json={"kind": "demo"}, headers={TOKEN_HEADER: "not-the-token"})
    assert response.status_code == 403


def test_reads_do_not_require_the_token(client: TestClient) -> None:
    """The token exists to stop a foreign page from *changing* things. A read
    that required it would send it to every request for no gain."""
    assert client.get("/health").status_code == 200
    assert client.get("/jobs").status_code == 200


def test_the_app_can_read_its_own_token(client: TestClient) -> None:
    """A cross-origin page may issue this request but cannot read the
    response: no CORS headers are sent, so the browser withholds the body."""
    body = client.get("/session").json()
    assert body["token"] == TEST_TOKEN
    assert token_matches(body["token"])


def test_the_token_file_is_created_readable_only_by_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Created with the mode rather than chmodded afterwards.

    Writing first and narrowing after leaves a window in which the file
    exists at the process default mode, and on a machine with other local
    users that window is enough (review finding on PR #39). No test covered
    this, which is why the first mutation of it passed.
    """
    import stat

    from harrier_api.localauth import load_or_create_token, token_path

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_API_TOKEN", raising=False)

    token = load_or_create_token()
    assert token

    mode = stat.S_IMODE(token_path().stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, expected 0o600"


def test_a_second_call_returns_the_token_already_in_circulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first requests must not each write a different token: the second
    would invalidate one already handed to the app."""
    from harrier_api.localauth import load_or_create_token

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_API_TOKEN", raising=False)

    assert load_or_create_token() == load_or_create_token()


# --- the capture route no longer writes on GET -------------------------------


def test_a_get_to_capture_changes_nothing(client: TestClient) -> None:
    """The image-tag attack. Any page the operator visited could write to the
    tracker with no interaction, because this route added the row."""
    response = client.get("/capture/add", params={"company": "Acme", "title": "Engineer"})
    assert response.status_code == 200

    conn = connect()
    assert list_jobs(conn) == []
    conn.close()


@pytest.mark.parametrize(
    ("data", "case"),
    [
        ({"company": "Acme", "title": "Engineer", "token": "wrong"}, "wrong token"),
        ({"company": "Acme", "title": "Engineer"}, "no token field at all"),
    ],
)
def test_the_capture_form_requires_the_token(
    client: TestClient, data: dict[str, str], case: str
) -> None:
    """Both shapes: a forged token and none. A cross-origin form post carries
    neither, so the absent case is the realistic one."""
    response = client.post("/capture/add-form", data=data)
    assert response.status_code == 403, case

    conn = connect()
    assert list_jobs(conn) == []
    conn.close()


# --- paid spend is bounded where it is read ----------------------------------


def test_a_stored_count_above_the_bound_is_clamped(env: Path) -> None:
    """Written directly to the store, which is the path that matters: the
    value outlives the request that set it and the next scheduled run
    executes it, so validating the write alone leaves this open."""
    from harrier.userconfig import DISCOVERY, set_config

    conn = connect()
    set_config(conn, DISCOVERY, {"apify_scheduled_count": APIFY_MAX_COUNT + 1})
    assert scheduled_apify_count(conn=conn) == APIFY_MAX_COUNT
    conn.close()


def test_a_reasonable_stored_count_is_untouched(env: Path) -> None:
    from harrier.userconfig import DISCOVERY, set_config

    conn = connect()
    inside = APIFY_MAX_COUNT - 1
    set_config(conn, DISCOVERY, {"apify_scheduled_count": inside})
    assert scheduled_apify_count(conn=conn) == inside
    conn.close()


def test_a_zero_or_negative_count_is_lifted_to_one(env: Path) -> None:
    from harrier.userconfig import DISCOVERY, set_config

    conn = connect()
    set_config(conn, DISCOVERY, {"apify_scheduled_count": -5})
    assert scheduled_apify_count(conn=conn) == 1
    conn.close()


# --- credentials do not cross the boundary -----------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("https://api.apify.com/v2/acts/x/runs?token=SECRETVALUE&count=5", "SECRETVALUE"),
        ("https://api.telegram.org/bot99:AAHsecretpart/sendMessage", "AAHsecretpart"),
        ("https://api.hunter.io/v2/x?api_key=hunterkey123&domain=e.com", "hunterkey123"),
        ("failed: https://x/y?access_token=abc.def&z=1", "abc.def"),
        ("https://x/y?password=hunter2", "hunter2"),
    ],
)
def test_a_credential_is_scrubbed_from_text(text: str, secret: str) -> None:
    """The trigger is a malformed environment value: `http.client.InvalidURL`,
    raised by a token pasted with a stray character, is not caught by a retry
    loop expecting TimeoutError, URLError and HTTPError, and its message
    embeds the request path. That escapes to a summary file, to stdout, and
    to the event stream."""
    scrubbed = scrub_secrets(text)
    assert secret not in scrubbed
    assert "REDACTED" in scrubbed


def test_scrubbing_keeps_the_useful_part() -> None:
    """The host and path are what make an error worth reading. Only the
    credential goes."""
    scrubbed = scrub_secrets("failed for https://api.apify.com/v2/acts/actor/runs?token=SECRET")
    assert "api.apify.com" in scrubbed
    assert "/v2/acts/actor/runs" in scrubbed


def test_scrubbing_leaves_ordinary_text_alone() -> None:
    message = "greenhouse board exampleco returned 404"
    assert scrub_secrets(message) == message


def test_a_structured_event_is_scrubbed(env: Path) -> None:
    """The branch my first pass missed. A subprocess emitting a well-formed
    protocol object whose message or URL holds a token put it straight onto
    the unauthenticated stream, because only the log-line branches were
    scrubbed (review finding on PR #39)."""
    from harrier_api.runs import scrub_event_data

    scrubbed = scrub_event_data(
        {
            "event": "progress",
            "message": "fetching https://api.apify.com/v2/acts/x/runs?token=SECRETVALUE",
            "nested": {"url": "https://x/y?api_key=INNERSECRET"},
            "items": ["https://x/y?token=LISTSECRET", 7],
            "count": 3,
        }
    )
    flat = repr(scrubbed)
    assert "SECRETVALUE" not in flat
    assert "INNERSECRET" not in flat
    assert "LISTSECRET" not in flat
    assert scrubbed["count"] == 3, "scrubbing must not alter non-string values"


async def _collect(manager: object, run: object, event_type: str, data: dict[str, object]):
    await manager._append(run, event_type, data)  # pyright: ignore[reportAttributeAccessIssue]


def test_every_event_is_scrubbed_at_the_choke_point(env: Path) -> None:
    """Behavioural, not a source scan.

    My first version of this test read the module text and matched literal
    `"log_line"` call sites, which is exactly the set I had just fixed, so it
    could not have caught the structured-event branch. Scrubbing now happens
    in `_append`, the single place an event reaches the stream, and this
    calls it.
    """
    import asyncio

    from harrier_api.runs import Run, RunManager

    manager = RunManager()
    run = Run(id="r1", kind="demo", command=["true"])
    asyncio.run(
        _collect(
            manager,
            run,
            "progress",
            {"message": "https://api.apify.com/v2/x?token=SECRETVALUE", "step": 2},
        )
    )
    stored = repr(run.events[-1].data)
    assert "SECRETVALUE" not in stored
    assert "REDACTED" in stored
    assert run.events[-1].data["step"] == 2


def test_every_exception_sink_in_discovery_is_scrubbed(env: Path) -> None:
    """Also broadened. The first version matched only lines containing
    `"errors": [`, so it passed while two logger.warning calls handed the raw
    exception straight to the log."""
    import harrier.discovery as discovery_module

    source = Path(discovery_module.__file__).read_text(encoding="utf-8")
    sinks = [
        line.strip()
        for line in source.splitlines()
        if ("exc" in line and ("logger." in line or '"errors"' in line))
    ]
    assert sinks, "this test is looking at the wrong place"
    unscrubbed = [line for line in sinks if "scrub_secrets" not in line]
    assert not unscrubbed, f"an exception reaches a sink unscrubbed: {unscrubbed}"


def test_a_board_error_carries_no_credential(env: Path) -> None:
    """redact_url covers the board URL. The exception text is a separate
    channel and can carry a provider URL of its own, which is how a token
    reached the per-source summary (review finding on PR #39)."""
    from harrier.sources import fetch_many

    def explode(board_url: str) -> list[NormalizedJob]:
        raise RuntimeError("upstream said no: https://api.apify.com/v2/x?token=SECRETVALUE")

    _jobs, errors = fetch_many(["https://boards.greenhouse.io/exampleco"], explode, "greenhouse")
    assert errors
    assert "SECRETVALUE" not in " ".join(errors)
    assert "REDACTED" in " ".join(errors)


# --- the daily path still works ----------------------------------------------


def test_the_ui_can_still_start_a_run(client: TestClient) -> None:
    """Every protection here is worthless if it breaks the thing it guards."""
    assert client.post("/runs", json={"kind": "demo"}, headers=auth()).status_code in (200, 201)


def test_the_bookmarklet_path_still_reaches_the_tracker(client: TestClient) -> None:
    """Two steps now: the bookmarklet navigates, the operator confirms."""
    page = client.get("/capture/add", params={"company": "Acme", "title": "Engineer"})
    assert TEST_TOKEN in page.text

    added = client.post(
        "/capture/add-form",
        data={"company": "Acme", "title": "Engineer", "token": TEST_TOKEN},
    )
    assert added.status_code == 200

    conn: sqlite3.Connection = connect()
    assert len(list_jobs(conn)) == 1
    conn.close()
