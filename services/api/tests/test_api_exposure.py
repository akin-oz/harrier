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


# --- the capture route no longer writes on GET -------------------------------


def test_a_get_to_capture_changes_nothing(client: TestClient) -> None:
    """The image-tag attack. Any page the operator visited could write to the
    tracker with no interaction, because this route added the row."""
    response = client.get("/capture/add", params={"company": "Acme", "title": "Engineer"})
    assert response.status_code == 200

    conn = connect()
    assert list_jobs(conn) == []
    conn.close()


def test_the_capture_form_requires_the_token(client: TestClient) -> None:
    response = client.post(
        "/capture/add-form", data={"company": "Acme", "title": "Engineer", "token": "wrong"}
    )
    assert response.status_code == 403

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
    set_config(conn, DISCOVERY, {"apify_scheduled_count": 10_000_000})
    assert scheduled_apify_count(conn=conn) == APIFY_MAX_COUNT
    conn.close()


def test_a_reasonable_stored_count_is_untouched(env: Path) -> None:
    from harrier.userconfig import DISCOVERY, set_config

    conn = connect()
    set_config(conn, DISCOVERY, {"apify_scheduled_count": 75})
    assert scheduled_apify_count(conn=conn) == 75
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


def test_the_run_stream_scrubs_what_it_relays(env: Path) -> None:
    """The event stream is unauthenticated by design, so anything a
    subprocess prints reaches a reader who presented nothing."""
    import harrier_api.runs as runs_module

    source = Path(runs_module.__file__).read_text(encoding="utf-8")
    relayed = [line for line in source.splitlines() if '"log_line"' in line]
    assert relayed, "no log_line relay found; this test is looking at the wrong place"
    unscrubbed = [line for line in relayed if "scrub_secrets" not in line]
    assert not unscrubbed, f"a log line reaches the stream unscrubbed: {unscrubbed}"


def test_the_discovery_summary_scrubs_its_errors(env: Path) -> None:
    """The other sink: a bare `except Exception` writes str(exc) into a
    summary file that is also printed."""
    import harrier.discovery as discovery_module

    source = Path(discovery_module.__file__).read_text(encoding="utf-8")
    error_sinks = [line for line in source.splitlines() if '"errors": [' in line]
    assert error_sinks
    unscrubbed = [line for line in error_sinks if "scrub_secrets" not in line]
    assert not unscrubbed, f"an error reaches the summary unscrubbed: {unscrubbed}"


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
