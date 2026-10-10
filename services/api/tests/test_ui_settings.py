"""The Settings page's routes, and the place every command has (spec 096).

Synthetic data only, built under `tmp_path`: no real watchlist entry, search,
hold, archive name or path appears here (ADR-008).
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from conftest import auth
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from harrier.backup import ARCHIVE_PREFIX, ARCHIVE_SUFFIX, create_backup
from harrier.db import connect
from harrier.paths import repo_root
from harrier.userconfig import get_config, list_config
from harrier.userconfig import importer as config_importer
from harrier_api.app import create_app
from harrier_api.runs import KIND_COMMANDS, PARAMETERIZED_KINDS, RunManager
from harrier_api.settings_routes import (
    COMMAND_PLACES,
    HOST_PANEL,
    OnHost,
    Routed,
    TerminalOnly,
    settings_router,
)
from harrier_cli.main import build_parser, main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout of our own: config/ files are read relative to the working
    directory, as the CLI reads them."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARRIER_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv("GMAIL_OAUTH_TOKEN_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    connect().close()
    return tmp_path


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


# --- every command has exactly one place ------------------------------------------


def leaf_commands() -> set[str]:
    """Every subcommand the parser accepts, by its two-word name."""

    def children(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
            if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                return dict(cast("dict[str, argparse.ArgumentParser]", action.choices))  # pyright: ignore[reportUnknownMemberType]
        return {}

    leaves: set[str] = set()
    for name, parser in children(build_parser()).items():
        nested = children(parser)
        if nested:
            leaves.update(f"{name} {child}" for child in nested)
        else:
            leaves.add(name)
    return leaves


# Routes spec 095 builds, which has not landed on this branch yet. Each is
# asserted absent, so the merge that brings one in fails here until it is
# removed from this set, and the set ends empty.
PENDING_ROUTES = {
    "GET /ops/export/jobs.csv",
    "GET /tracker/{selector}/events",
    "POST /ops/events/backfill",
    "GET /apply/{selector}/brief",
    "PUT /apply/{selector}/brief",
    "GET /ops/check",
    "POST /ops/evaluate-prospects",
    "POST /ops/scoring/export",
}


def app_routes() -> set[str]:
    """Every route as `METHOD /path`, read from the OpenAPI document: the
    document is what the web app's client is generated from (ADR-005)."""
    paths = cast("dict[str, dict[str, object]]", create_app().openapi()["paths"])
    return {f"{method.upper()} {path}" for path, methods in paths.items() for method in methods}


def test_every_cli_subcommand_has_exactly_one_place(env: Path) -> None:
    """Routed, run on the host, or terminal only, and never two of them.

    The table is a dictionary, so no command can sit in two places. This
    walks the parser, so a command in none fails here, and so does a place
    kept for a command that is gone.
    """
    leaves = leaf_commands()
    assert set(COMMAND_PLACES) == leaves, (
        f"placed nowhere: {sorted(leaves - set(COMMAND_PLACES))}; "
        f"placed but gone: {sorted(set(COMMAND_PLACES) - leaves)}"
    )

    routes = app_routes()
    for command, place in COMMAND_PLACES.items():
        if not isinstance(place, Routed):
            continue
        if place.route in PENDING_ROUTES:
            assert place.route not in routes, (
                f"{place.route} exists now: take it out of PENDING_ROUTES"
            )
        else:
            assert place.route in routes, f"{command}: no route {place.route}"

    # A browser button that runs a host-only or terminal-only command is what
    # this spec must not introduce: no run kind executes one.
    run_verbs = {kind.verb for kind in PARAMETERIZED_KINDS.values()}
    run_verbs |= {command[3] for command in KIND_COMMANDS.values()}
    unrouted = {
        command
        for command, place in COMMAND_PLACES.items()
        if isinstance(place, OnHost | TerminalOnly)
    }
    assert not run_verbs & unrouted, f"a run executes {sorted(run_verbs & unrouted)}"


def test_the_page_reads_the_list_from_the_api(client: TestClient) -> None:
    body = client.get("/settings/commands", headers=auth()).json()
    places = {
        **{entry["command"]: "routed" for entry in body["routed"]},
        **{entry["command"]: "host" for entry in body["host"]},
        **{entry["command"]: "terminal" for entry in body["terminal"]},
    }
    assert set(places) == set(COMMAND_PLACES)
    assert len(body["routed"]) + len(body["host"]) + len(body["terminal"]) == len(COMMAND_PLACES)
    demo = next(entry for entry in body["routed"] if entry["command"] == "demo-run")
    assert "harness for the run panel" in demo["note"]
    assert [row["fact"] for row in body["panel"]] == [fact for fact, _ in HOST_PANEL]


# --- shown commands -------------------------------------------------------------

SHOWN = re.compile(r"(harrier|just)( (--[a-z][a-z-]*|[a-z][a-z-]*|<[a-z][a-z ]*>))*")


def shown_commands() -> list[str]:
    host = [place.shown for place in COMMAND_PLACES.values() if isinstance(place, OnHost)]
    panel = [line for _, lines in HOST_PANEL for line in lines]
    return host + panel


@pytest.mark.parametrize("line", shown_commands())
def test_shown_commands_carry_placeholders_only(line: str) -> None:
    """Words, flags and placeholders in angle brackets, and nothing else: no
    path, no home directory, no address, no number anyone typed."""
    assert SHOWN.fullmatch(line), line
    assert os.path.expanduser("~") not in line
    program, *rest = line.split(" ")
    if program == "just":
        recipes = (repo_root() / "justfile").read_text(encoding="utf-8")
        assert re.search(rf"^{re.escape(rest[0])}:", recipes, re.MULTILINE), line
        return
    # The exact command: the parser accepts it once each placeholder stands
    # for a value.
    argv = [re.sub(r"^<.*>$", "placeholder", word) for word in rest]
    build_parser().parse_args(argv)


def test_every_host_command_is_on_the_panel_or_listed_with_its_line() -> None:
    for command, place in COMMAND_PLACES.items():
        if isinstance(place, OnHost):
            assert place.shown.startswith(f"harrier {command}"), command


# --- the token ------------------------------------------------------------------


def test_settings_routes_require_the_token(client: TestClient) -> None:
    routes = [route for route in settings_router.routes if isinstance(route, APIRoute)]
    assert routes
    for route in routes:
        path = route.path.replace("{name}", f"{ARCHIVE_PREFIX}2026-01-01-000000{ARCHIVE_SUFFIX}")
        for method in route.methods or ():
            response = client.request(method, path, json={"urls": []})
            assert response.status_code == 403, f"{method} {route.path}"


# --- configuration import -------------------------------------------------------

FEEDS = ["https://boards.greenhouse.io/example", "https://jobs.lever.co/acme"]


def write_config_files(root: Path) -> None:
    config = root / "config"
    config.mkdir(exist_ok=True)
    (config / "feeds.txt").write_text("\n".join(FEEDS) + "\n", encoding="utf-8")
    (config / "linkedin_search_urls.txt").write_text(
        "https://www.linkedin.com/jobs/search/?keywords=example\n", encoding="utf-8"
    )
    (config / "companies-hold.csv").write_text(
        "company,reason,hold_until\nExample Co,,2999-12-31\n", encoding="utf-8"
    )
    (config / "discovery.json").write_text(
        json.dumps({"_comment": "a note", "apify_scheduled_count": 40}), encoding="utf-8"
    )


def stored() -> dict[str, object]:
    conn = connect()
    try:
        return {row["kind"]: get_config(conn, row["kind"]) for row in list_config(conn)}
    finally:
        conn.close()


def clear() -> None:
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM user_config")
    finally:
        conn.close()


def test_config_import_over_http_matches_the_cli(client: TestClient, env: Path) -> None:
    write_config_files(env)
    real = config_importer.import_config_files
    with patch.object(config_importer, "import_config_files", wraps=real) as shared:
        assert main(["config", "import"]) == 0
        from_cli = stored()
        assert shared.call_count == 1

        clear()
        response = client.post("/config/import", headers=auth())
        assert response.status_code == 200, response.text
        assert shared.call_count == 2

    assert stored() == from_cli
    assert set(from_cli) == {"feeds", "linkedin_searches", "company_holds", "discovery"}
    body = response.json()
    assert body["report"][0] == "feeds: 2 entries imported"
    assert body["total"] == 5


def test_config_import_refuses_in_the_stores_words(client: TestClient, env: Path) -> None:
    write_config_files(env)
    (env / "config" / "companies-hold.csv").write_text(
        "company,reason,hold_until\nExample Co,,end of june\n", encoding="utf-8"
    )
    response = client.post("/config/import", headers=auth())
    assert response.status_code == 400
    assert "malformed hold_until" in response.json()["detail"]
    assert stored() == {}

    for name in ("feeds.txt", "linkedin_search_urls.txt", "companies-hold.csv", "discovery.json"):
        (env / "config" / name).unlink()
    nothing = client.post("/config/import", headers=auth())
    assert nothing.status_code == 409
    assert nothing.json()["detail"] == "nothing to import; no configuration files found"


def test_unrouted_watchlist_lines_are_named_in_spec_041s_words(client: TestClient) -> None:
    response = client.post(
        "/settings/feeds/routing",
        json={"urls": [*FEEDS, "https://careers.example.com/jobs", "  "]},
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["unrouted"] == [
        {
            "url": "https://careers.example.com/jobs",
            "message": "no importer handles https://careers.example.com/jobs",
        }
    ]


# --- backups --------------------------------------------------------------------


def wait_for(client: TestClient, run_id: str) -> str:
    deadline = time.monotonic() + 60
    state = ""
    while time.monotonic() < deadline:
        state = client.get(f"/runs/{run_id}").json()["state"]
        if state in ("succeeded", "failed", "cancelled", "interrupted"):
            return state
        time.sleep(0.1)
    return state


def log_lines(manager: RunManager, run_id: str) -> list[str]:
    run = manager.get(run_id)
    assert run is not None
    return [str(event.data.get("line", "")) for event in run.events if event.type == "log_line"]


def test_backup_verify_takes_a_listed_name_never_a_path(env: Path) -> None:
    archive = create_backup(env / "backups").archive
    (env / "backups" / "notes.txt").write_text("not an archive", encoding="utf-8")
    elsewhere = env / "elsewhere"
    elsewhere.mkdir()
    outside = elsewhere / archive.name.replace("2", "3")
    outside.write_bytes(archive.read_bytes())

    manager = RunManager(journal_path=env / "data" / "runs" / "journal.jsonl")
    with TestClient(create_app(run_manager=manager)) as client:
        listed = client.get("/settings/backups", headers=auth()).json()
        assert listed["directory"] == "present"
        assert [item["name"] for item in listed["archives"]] == [archive.name]

        for name in (outside.name, "notes.txt", "..", f"..%2Felsewhere%2F{outside.name}"):
            refused = client.post(f"/settings/backups/{name}/verify", headers=auth())
            assert refused.status_code == 404, name

        started = client.post(f"/settings/backups/{archive.name}/verify", headers=auth())
        assert started.status_code == 200
        run_id = started.json()["id"]
        assert wait_for(client, run_id) == "succeeded"
        lines = log_lines(manager, run_id)
        assert any(line.startswith(f"{archive.name} opens") for line in lines), lines

        marked = client.get("/settings/backups", headers=auth()).json()["archives"]
        assert marked[0]["verification"] == "passed"


def test_a_failed_verification_marks_the_archive(env: Path) -> None:
    broken = env / "backups" / f"{ARCHIVE_PREFIX}2026-01-01-000000{ARCHIVE_SUFFIX}"
    broken.parent.mkdir()
    broken.write_bytes(b"not a tar archive")
    manager = RunManager(journal_path=env / "data" / "runs" / "journal.jsonl")
    with TestClient(create_app(run_manager=manager)) as client:
        run_id = client.post(f"/settings/backups/{broken.name}/verify", headers=auth()).json()["id"]
        assert wait_for(client, run_id) == "failed"
        assert any("not a readable archive" in line for line in log_lines(manager, run_id))
        marked = client.get("/settings/backups", headers=auth()).json()["archives"]
        assert marked[0]["verification"] == "failed"


def test_a_backup_reports_the_archive_by_name_never_its_path(env: Path) -> None:
    manager = RunManager(journal_path=env / "data" / "runs" / "journal.jsonl")
    with TestClient(create_app(run_manager=manager)) as client:
        empty = client.get("/settings/backups", headers=auth()).json()
        assert empty == {"directory": "absent", "archives": []}

        # Spec 050's route, which spec 096 reuses.
        run_id = client.post("/ops/backup", headers=auth()).json()["id"]
        assert wait_for(client, run_id) == "succeeded"
        names = [
            item["name"]
            for item in client.get("/settings/backups", headers=auth()).json()["archives"]
        ]
        assert len(names) == 1
        lines = log_lines(manager, run_id)
        assert any(line.startswith(f"{names[0]} (") for line in lines), lines
        assert not any(str(env) in line for line in lines), lines


# --- what the container can see about the host ----------------------------------

SECRET_CONTENT = "zzqqx-token-contents"
SECRET_NAME = "zzqqx-token-name.json"


def write_host_state(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = env / "secrets"
    secrets.mkdir()
    (secrets / SECRET_NAME).write_text(SECRET_CONTENT, encoding="utf-8")
    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_FILE", str(secrets / SECRET_NAME))


def write_model(env: Path) -> None:
    from harrier.scoring.features import FEATURE_ORDER, NUMERIC_FEATURES
    from harrier.scoring.model import active_model_path, dump_model, model_document

    path = active_model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        dump_model(
            model_document(
                coefficients=[0.0] * len(FEATURE_ORDER),
                intercept=0.0,
                p92={name: 1.0 for name in NUMERIC_FEATURES},
                created_at="2026-01-05",
                training={"zzqqx_training": 4242},
                evaluation={"zzqqx_evaluation": 4343},
            )
        )
    )
    exports = path.parent / "exports"
    exports.mkdir()
    (exports / "features-20260104.jsonl").write_text("{}\n", encoding="utf-8")
    (exports / "features-20260103.jsonl").write_text("{}\n", encoding="utf-8")


def test_host_facts_never_carry_a_secret_or_claim_health(
    client: TestClient, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_host_state(env, monkeypatch)
    write_model(env)
    response = client.get("/settings/host", headers=auth())
    assert response.status_code == 200
    body = response.json()
    text = response.text

    # Never a secret: not its contents, its name, or where it is.
    for leaked in (SECRET_CONTENT, SECRET_NAME, "secrets", str(env), "zzqqx"):
        assert leaked not in text, leaked
    assert body["gmail_token"] == {"state": "present", "age_days": 0}

    # What only the host can know is unknown, never healthy. The schedule's
    # installed state is said by spec 050's `GET /ops/schedule`, pinned in
    # test_ui_operations.py.
    assert body["database_owner"] == "unknown"
    assert body["model"]["state"] == "active"
    assert body["model"]["trained_at"] == "2026-01-05"
    assert body["newest_feature_export"] == "2026-01-04"


def test_absent_host_facts_are_shown_as_absent(
    client: TestClient, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_FILE", str(env / "secrets" / "missing.json"))
    body = client.get("/settings/host", headers=auth()).json()
    assert body["gmail_token"] == {"state": "absent", "age_days": None}
    assert body["model"] == {"state": "missing", "trained_at": None, "version": None}
    assert body["newest_feature_export"] is None

    monkeypatch.delenv("GMAIL_OAUTH_TOKEN_FILE")
    body = client.get("/settings/host", headers=auth()).json()
    assert body["gmail_token"]["state"] == "not_configured"


def test_host_facts_hold_only_the_listed_fields(
    client: TestClient, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_host_state(env, monkeypatch)
    write_model(env)
    body: dict[str, Any] = client.get("/settings/host", headers=auth()).json()
    assert set(body) == {
        "gmail_token",
        "model",
        "newest_feature_export",
        "image_revision",
        "database_owner",
    }
    assert set(body["gmail_token"]) == {"state", "age_days"}
    assert set(body["model"]) == {"state", "trained_at", "version"}
