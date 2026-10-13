"""Hosted runs are bound to their owner (spec 106), without a database server.

The run manager here serves hosted with a stand-in for its store half: the
binding is the real `runenv.bind_run`, over credentials encrypted here with
a key made at run time; only the `run_records` writes are kept in memory.
`tests/test_hosted_runs_on_postgres.py` runs the store half on Postgres.
The children are real processes that print what they hold, by name and by
SHA-256 of each value, so no value is printed by a test. Every credential,
key and owner here is synthetic, and nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from test_hosted_auth import credentials_key

from harrier.connection import POSTGRES_NOT_YET, RUN_OWNER_VARIABLE
from harrier.credentials import (
    CredentialKind,
    CredentialsKey,
    StoredCredential,
    encrypt,
)
from harrier.llm import generate_text
from harrier.llm.config import (
    CLI_PROVIDERS_LOCAL_ONLY,
    NO_HOSTED_AI_CREDENTIAL,
    LLMClientError,
    load_config,
    resolve_auto_providers,
)
from harrier.logredact import SECRET_REDACTION
from harrier_api.hostedruns import HEALTH_PATH, HostedRuns, KeepAwake
from harrier_api.runenv import (
    COPIED_VARIABLES,
    BoundRun,
    ExtraVariableRefused,
    RunCredentialMissing,
    bind_run,
)
from harrier_api.runs import (
    HOSTED_RUN_KINDS,
    KIND_COMMANDS,
    NO_OWNER_DETAIL,
    PARAMETERIZED_KINDS,
    TERMINAL_STATES,
    Run,
    RunCredentialRefused,
    RunManager,
    RunParams,
    RunsNotHosted,
)
from harrier_cli import main as cli

RUN_DATABASE_URL = f"postgresql://harrier_run:{uuid.uuid4().hex}@127.0.0.1:1/postgres"
API_DATABASE_URL = f"postgresql://harrier_api:{uuid.uuid4().hex}@127.0.0.1:1/postgres"

# A child that prints every variable it holds, each by name and by the
# SHA-256 of its value, with its working directory, as one JSON line.
PRINT_ENVIRONMENT = (
    "import hashlib, json, os\n"
    "print(json.dumps({'cwd': os.getcwd(), 'env': {name: "
    "hashlib.sha256(value.encode()).hexdigest() for name, value in os.environ.items()}}), "
    "flush=True)\n"
)


def waits_for(signal: Path) -> str:
    """A child that waits for `signal` to exist, at most 20 seconds, so a
    failed test leaves no process behind."""
    return (
        "import os, time\n"
        "deadline = time.monotonic() + 20\n"
        f"while not os.path.exists({str(signal)!r}) and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_owner() -> str:
    return str(uuid.uuid4())


def synthetic_secret(label: str) -> str:
    # No shape a pattern scrub knows (no `sk-`, no `token=`), so only the
    # exact-value scrub can catch it.
    return f"synthetic{label}{uuid.uuid4().hex}"


def stored(
    key: CredentialsKey, owner: str, kind: CredentialKind, fields: Mapping[str, str]
) -> StoredCredential:
    nonce, ciphertext = encrypt(key, owner, kind, fields)
    return StoredCredential(kind, key.key_id, nonce, ciphertext, "2026-10-13T00:00:00Z")


# --- the run manager, hosted, with its store half in memory ---


@dataclass
class Beats:
    """The keep-awake beats' network and clock: what was sent, and a sleep
    the test releases."""

    sent: list[str] = field(default_factory=lambda: [])
    fail: bool = False
    slept: int = 0

    def send(self, url: str) -> int:
        self.sent.append(url)
        if self.fail:
            raise ConnectionError("synthetic failure")
        return 200

    async def sleep(self, _seconds: float) -> None:
        self.slept += 1
        await asyncio.sleep(0.02)


@dataclass
class InMemoryRecords:
    """`HostedRuns` with its store in memory: the owner's ciphertexts and
    the rows. `begin` binds through the real `bind_run`."""

    key: CredentialsKey
    root: Path
    credentials: dict[str, list[StoredCredential]]
    keep_awake: KeepAwake
    rows: dict[str, dict[str, object]] = field(default_factory=lambda: {})
    bound: dict[str, BoundRun] = field(default_factory=lambda: {})

    def begin(
        self, run: Run, needs: Iterable[CredentialKind], extra: Mapping[str, str] | None = None
    ) -> BoundRun:
        bound = bind_run(
            server_env=os.environ,
            owner=run.owner,
            root=self.root,
            stored=self.credentials.get(run.owner, []),
            key=self.key,
            needs=needs,
            extra=extra,
        )
        self.rows[run.id] = {"owner": run.owner, "state": run.state}
        self.bound[run.id] = bound
        return bound

    def record(self, run: Run) -> None:
        self.rows[run.id] = {"owner": run.owner, "state": run.state}


@dataclass
class Hosted:
    manager: RunManager
    records: InMemoryRecords
    beats: Beats


def hosted_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str = PRINT_ENVIRONMENT,
    credentials: dict[str, list[StoredCredential]] | None = None,
) -> Hosted:
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    monkeypatch.setenv("HARRIER_RUN_DATABASE_URL", RUN_DATABASE_URL)
    beats = Beats()
    keep_awake = KeepAwake("api.example.test", send=beats.send, sleep=beats.sleep, interval=1.0)
    records = InMemoryRecords(
        key=credentials_key(),
        root=tmp_path / "api-data",
        credentials=credentials or {},
        keep_awake=keep_awake,
    )
    manager = RunManager(
        journal_path=tmp_path / "journal.jsonl",
        kind_commands={kind: [sys.executable, "-c", script] for kind in ("demo", "discovery")},
        grace_seconds=0.5,
    )
    manager.serve_hosted(cast(HostedRuns, records))
    return Hosted(manager, records, beats)


def run_to_end(manager: RunManager, owner: str, kind: str = "demo", **kwargs: Any) -> Run:
    async def go() -> Run:
        run = await manager.start(kind, kwargs.get("params"), owner=owner)
        return await manager.wait(run.id)

    return asyncio.run(go())


def printed(run: Run) -> dict[str, Any]:
    for event in run.events:
        line = event.data.get("line")
        if isinstance(line, str) and line.startswith("{"):
            return cast(dict[str, Any], json.loads(line))
    raise AssertionError(f"the child printed no JSON line: {run.events}")


# --- which kinds run hosted ---


def test_every_run_kind_declares_its_hosted_answer() -> None:
    """Every kind the run manager knows has one answer, and a refused one
    names why. The CLI's run class is exactly the verbs of the kinds that
    run, so a kind offered later moves its verb in, or this fails."""
    kinds = set(KIND_COMMANDS) | set(PARAMETERIZED_KINDS)
    assert set(HOSTED_RUN_KINDS) == kinds
    for kind, answer in HOSTED_RUN_KINDS.items():
        assert answer.runs or kind in answer.refusal, kind
        assert answer.runs != bool(answer.refusal), kind
    verbs = {
        kind: (PARAMETERIZED_KINDS[kind].verb if kind in PARAMETERIZED_KINDS else "")
        for kind in kinds
    }
    verbs["demo"] = KIND_COMMANDS["demo"][3]
    assert {verbs[kind] for kind, answer in HOSTED_RUN_KINDS.items() if answer.runs} == set(
        cli.RUN_CLASS
    )
    # Spec 106's table, as written.
    assert HOSTED_RUN_KINDS["digest"].needed(frozenset()) == ("telegram",)
    assert HOSTED_RUN_KINDS["digest"].needed(frozenset({"--dry-run"})) == ()
    assert HOSTED_RUN_KINDS["evaluate-prospects"].needed(frozenset()) == ("ai",)
    assert "until spec 107" in HOSTED_RUN_KINDS["tailor"].refusal
    assert "(spec 109)" in HOSTED_RUN_KINDS["gmail-watch"].refusal
    assert HOSTED_RUN_KINDS["backup"].refusal == "backup runs are local only"


def test_a_hosted_run_needs_an_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hosted = hosted_manager(tmp_path, monkeypatch)
    with pytest.raises(RunsNotHosted) as refused:
        asyncio.run(hosted.manager.start("demo", owner=""))
    assert str(refused.value) == NO_OWNER_DETAIL
    assert hosted.manager.list_runs() == []
    assert hosted.records.rows == {}


# --- the lock ---


def test_two_owners_never_join_each_others_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal = tmp_path / "go-on"
    script = waits_for(signal)
    hosted = hosted_manager(tmp_path, monkeypatch, script)
    a, b = new_owner(), new_owner()

    async def go() -> tuple[Run, Run, Run]:
        first = await hosted.manager.start("demo", owner=a)
        theirs = await hosted.manager.start("demo", owner=b)
        again = await hosted.manager.start("demo", owner=a)
        signal.write_text("", encoding="utf-8")
        for run in (first, theirs):
            await hosted.manager.wait(run.id)
        return first, theirs, again

    first, theirs, again = asyncio.run(go())
    assert theirs.id != first.id
    assert theirs.owner == b
    assert again.id == first.id
    assert {run.state for run in (first, theirs)} == {"succeeded"}


# --- credentials ---


def test_a_run_that_needs_a_missing_credential_is_refused_before_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[object] = []
    real = asyncio.create_subprocess_exec

    async def spawn(*argv: Any, **kwargs: Any) -> Any:
        spawned.append(argv)
        return await real(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    owner = new_owner()
    hosted = hosted_manager(tmp_path, monkeypatch)
    # A digest that sends needs Telegram; the owner has none.
    params = RunParams(target="2026-10-13")
    with pytest.raises(RunCredentialRefused) as refused:
        asyncio.run(hosted.manager.start("digest", params, owner=owner))
    assert str(refused.value) == "this run needs your telegram credential; set it under credentials"
    assert refused.value.missing and refused.value.kind == "telegram"
    assert spawned == []
    assert hosted.manager.list_runs() == []
    assert hosted.records.rows == {}
    assert not (tmp_path / "api-data" / "owners").exists()

    # A dry digest needs none, and runs.
    dry = RunParams(switches=frozenset({"--dry-run"}), target="2026-10-13:dry")

    def quiet_command(_kind: str, _params: RunParams) -> list[str]:
        return [sys.executable, "-c", ""]

    monkeypatch.setattr("harrier_api.runs.build_command", quiet_command)
    run = run_to_end(hosted.manager, owner, "digest", params=dry)
    assert run.state == "succeeded"
    assert len(spawned) == 1


def test_an_unreadable_credential_is_refused_with_its_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    owner, other = new_owner(), new_owner()
    key = credentials_key()
    # Owner B's ciphertext in owner A's rows does not decrypt for A.
    moved = stored(key, other, "ai", {"provider": "openai-api", "key": synthetic_secret("k")})
    hosted = hosted_manager(tmp_path, monkeypatch, credentials={owner: [moved]})
    with caplog.at_level(logging.WARNING), pytest.raises(RunCredentialRefused) as refused:
        asyncio.run(hosted.manager.start("evaluate-prospects", RunParams(), owner=owner))
    assert str(refused.value) == "your ai credential cannot be read; set it again"
    assert not refused.value.missing
    assert hosted.manager.list_runs() == []
    logged = caplog.text
    assert key.key_id in logged and "ai" in logged
    assert owner not in logged and other not in logged


# --- the child's environment ---

# Every name spec 106's Behavior section excludes by name, and one of each
# excluded prefix.
EXCLUDED = (
    "HARRIER_DATABASE_URL",
    "HARRIER_RUN_DATABASE_URL",
    "HARRIER_CREDENTIALS_KEY",
    "HARRIER_AUTH",
    "HARRIER_SUPABASE_URL",
    "HARRIER_ALLOWED_HOSTS",
    "HARRIER_CORS_ORIGINS",
    "FLY_MACHINE_ID",
    "SUPABASE_SERVICE_ROLE_KEY",
    "AI_DEBUG",
    "OPENAI_DEBUG",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CLI_PATH",
    "CODEX_CLI_PATH",
    "GMAIL_TOKEN_FILE",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "APIFY_TOKEN",
    "HUNTER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "AI_PROVIDER",
    "VIRTUAL_ENV",
)


def test_the_child_environment_holds_only_the_allowlist_and_its_owners_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = synthetic_secret("sentinel")
    random_name = f"SYNTHETIC_{uuid.uuid4().hex.upper()}"
    hosted = hosted_manager(tmp_path, monkeypatch)
    # Set after the manager, so the run URL and hosted mode stay what the
    # manager needs, and each excluded name holds the sentinel otherwise.
    for name in (*EXCLUDED, random_name):
        if name not in ("HARRIER_RUN_DATABASE_URL", "HARRIER_AUTH"):
            monkeypatch.setenv(name, sentinel)
    copied = {name: f"copied-{name}" for name in COPIED_VARIABLES if name != "PATH"}
    for name, value in copied.items():
        monkeypatch.setenv(name, value)
    owner = new_owner()
    key = credentials_key()
    ai_key, apify, hunter = (synthetic_secret(label) for label in ("ai", "apify", "hunter"))
    bot, chat = synthetic_secret("bot"), str(uuid.uuid4().int)[:12]
    hosted.records.credentials[owner] = [
        stored(key, owner, "ai", {"provider": "anthropic-api", "key": ai_key, "model": "m-1"}),
        stored(key, owner, "apify", {"token": apify}),
        stored(key, owner, "hunter", {"key": hunter}),
        stored(key, owner, "telegram", {"bot_token": bot, "chat_id": chat}),
    ]

    run = run_to_end(hosted.manager, owner)
    assert run.state == "succeeded", run.events
    child = printed(run)
    run_dir = str((tmp_path / "api-data" / "owners" / owner).resolve())
    expected = {
        **{name: digest(value) for name, value in copied.items()},
        "PATH": digest(os.environ["PATH"]),
        "HOME": digest(str(tmp_path / "api-data" / "owners" / owner)),
        "HARRIER_DATA_DIR": digest(str(tmp_path / "api-data" / "owners" / owner)),
        "HARRIER_DATABASE_URL": digest(RUN_DATABASE_URL),
        RUN_OWNER_VARIABLE: digest(owner),
        "AI_PROVIDER": digest("anthropic-api"),
        "ANTHROPIC_API_KEY": digest(ai_key),
        "AI_MODEL": digest("m-1"),
        "APIFY_TOKEN": digest(apify),
        "HUNTER_API_KEY": digest(hunter),
        "TELEGRAM_BOT_TOKEN": digest(bot),
        "TELEGRAM_CHAT_ID": digest(chat),
    }
    # The platform adds these to every process it starts; they hold
    # nothing of the server's.
    platform = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
    got = {name: value for name, value in child["env"].items() if name not in platform}
    assert got == expected
    assert digest(sentinel) not in child["env"].values()
    assert os.path.realpath(child["cwd"]) == run_dir


def test_the_credentials_key_never_reaches_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = hosted_manager(tmp_path, monkeypatch)
    key_text = f"test-key:{uuid.uuid4().hex}"
    monkeypatch.setenv("HARRIER_CREDENTIALS_KEY", key_text)
    monkeypatch.setenv("HARRIER_DATABASE_URL", API_DATABASE_URL)
    a, b = new_owner(), new_owner()
    key = credentials_key()
    theirs = synthetic_secret("theirs")
    hosted.records.credentials[b] = [stored(key, b, "apify", {"token": theirs})]

    run = run_to_end(hosted.manager, a)
    held = set(printed(run)["env"].values())
    for value in (key_text, API_DATABASE_URL, theirs):
        assert digest(value) not in held
    assert digest(RUN_DATABASE_URL) in held


def test_a_hosted_run_works_in_its_owners_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = (
        "import json, os\n"
        "from harrier.screening.seen import SeenDecision, save_seen, _state_path\n"
        "save_seen('synthetic', {'k': SeenDecision('kept', 'r', 'p', '2026-10-13T00:00:00Z')}, 1)\n"
        "print(json.dumps({'cwd': os.getcwd(), 'home': os.environ['HOME'], "
        "'data': os.environ['HARRIER_DATA_DIR'], "
        "'seen': str(_state_path('synthetic', 1))}), flush=True)\n"
    )
    hosted = hosted_manager(tmp_path, monkeypatch, script)
    owners = new_owner(), new_owner()
    seen: list[str] = []
    for owner in owners:
        run = run_to_end(hosted.manager, owner)
        assert run.state == "succeeded", run.events
        child = printed(run)
        directory = tmp_path / "api-data" / "owners" / owner
        for where in (child["cwd"], child["home"], child["data"]):
            assert os.path.realpath(where) == str(directory.resolve())
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert Path(child["seen"]).is_file()
        assert directory.resolve() in Path(child["seen"]).resolve().parents
        seen.append(child["seen"])
    # Track 1 of each owner: two files.
    assert seen[0] != seen[1]


def test_a_hosted_run_reads_no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `.env` in the run's working directory adds nothing: not the
    Telegram settings a spawned run once took from it (spec 109's privacy
    review), not a key, and not the store URL."""
    sentinel = synthetic_secret("dotenv")
    script = (
        "import json, os\n"
        "from harrier_cli.main import main\n"
        "code = main(['demo-run', '--steps', '1', '--delay', '0'])\n"
        "print(json.dumps({'code': code, 'names': sorted(os.environ)}), flush=True)\n"
    )
    hosted = hosted_manager(tmp_path, monkeypatch, script)
    owner = new_owner()
    directory = tmp_path / "api-data" / "owners" / owner
    directory.mkdir(parents=True)
    names = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "AI_PROVIDER")
    (directory / ".env").write_text(
        "".join(f"{name}={sentinel}\n" for name in names), encoding="utf-8"
    )
    run = run_to_end(hosted.manager, owner)
    child = printed(run)
    assert child["code"] == 0, run.events
    assert not set(names) & set(child["names"])
    assert all(sentinel not in json.dumps(event.data) for event in run.events)


@pytest.mark.dotenv_fallback
def test_a_hosted_run_reads_no_store_url_from_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store_target` falls back to `.env` for the store's URL when the
    environment has none. In a hosted run it does not, so a `.env` cannot
    name the store a run opens."""
    (tmp_path / ".env").write_text(
        f"HARRIER_DATABASE_URL={RUN_DATABASE_URL}\nTELEGRAM_BOT_TOKEN=synthetic\n",
        encoding="utf-8",
    )
    env = {"PATH": os.environ["PATH"], RUN_OWNER_VARIABLE: new_owner()}
    started = subprocess.run(
        [sys.executable, "-m", "harrier_cli.main", "demo-run", "--steps", "1", "--delay", "0"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert started.returncode == 1
    assert started.stderr.strip() == f"error: {cli.OWNER_WITHOUT_POSTGRES}"


# --- the run class on a Postgres store ---

RUN_VERBS: dict[str, list[str]] = {
    "discover": ["discover"],
    "demo-run": ["demo-run", "--steps", "1", "--delay", "0"],
    "digest": ["digest", "--dry-run"],
    "config check-feeds": ["config", "check-feeds"],
    "reconsider": ["reconsider"],
    "events backfill": ["events", "backfill", "--dry-run"],
    "evaluate-prospects": ["evaluate-prospects"],
}


class Opened(Exception):
    """Raised by the stand-in `open_store`, so a command stops there."""


@pytest.fixture
def cli_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[object], pytest.MonkeyPatch]:
    """The CLI in this process with a Postgres URL, its working directory a
    tmp_path holding a `.env`, and every open recorded and refused."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=synthetic-from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HARRIER_DATABASE_URL", RUN_DATABASE_URL)
    opened: list[object] = []

    def open_store(*_args: object, owner: str | None = None) -> object:
        opened.append(owner)
        raise Opened

    def connect(*_args: object, **_kwargs: object) -> object:
        opened.append("sqlite")
        raise Opened

    monkeypatch.setattr(cli, "open_store", open_store)
    monkeypatch.setattr(cli, "connect", connect)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    return opened, monkeypatch


def test_a_run_command_needs_an_owner_on_postgres(
    cli_on_postgres: tuple[list[object], pytest.MonkeyPatch],
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened, monkeypatch = cli_on_postgres
    assert set(RUN_VERBS) == set(cli.RUN_CLASS)

    # Without an owner: the run class text, exit 1, nothing opened.
    for name, argv in RUN_VERBS.items():
        assert cli.main(argv) == 1, name
        assert capsys.readouterr().err.strip() == f"error: {cli.run_class_refusal(name)}"
    assert opened == []
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    # With one: each verb runs, and opens its store as that owner.
    owner = new_owner()
    monkeypatch.setenv(RUN_OWNER_VARIABLE, owner)
    for name, argv in RUN_VERBS.items():
        opened.clear()
        try:
            code = cli.main(argv)
        except Opened:
            code = None
        capsys.readouterr()
        if name == "demo-run":
            assert code == 0 and opened == []
        else:
            assert opened == [owner], name
        assert "TELEGRAM_BOT_TOKEN" not in os.environ, name

    # A command outside the run class, with an owner: spec 112's text until
    # spec 127, nothing opened, and no `.env` read.
    opened.clear()
    for argv in (["next"], ["store", "status"]):
        assert cli.main(argv) == 1
        assert capsys.readouterr().err.strip() == f"error: {POSTGRES_NOT_YET}"
    assert opened == []
    assert "TELEGRAM_BOT_TOKEN" not in os.environ

    # An owner that is not a UUID, and one beside SQLite.
    monkeypatch.setenv(RUN_OWNER_VARIABLE, "not-an-owner")
    assert cli.main(RUN_VERBS["demo-run"]) == 1
    assert capsys.readouterr().err.strip() == f"error: {cli.NOT_AN_OWNER}"
    monkeypatch.setenv(RUN_OWNER_VARIABLE, owner)
    monkeypatch.setenv("HARRIER_DATABASE_URL", "")
    assert cli.main(RUN_VERBS["demo-run"]) == 1
    assert capsys.readouterr().err.strip() == f"error: {cli.OWNER_WITHOUT_POSTGRES}"
    assert opened == []
    assert "TELEGRAM_BOT_TOKEN" not in os.environ


# --- providers ---


def fake_binary(directory: Path, name: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_hosted_runs_offer_only_the_api_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("codex", "claude"):
        fake_binary(tmp_path, name)
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in ("AI_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # Locally, auto finds the binaries first.
    assert resolve_auto_providers()[0] == "codex-cli"

    monkeypatch.setenv(RUN_OWNER_VARIABLE, new_owner())
    for provider in ("codex-cli", "claude-cli", "codex", "claude"):
        with pytest.raises(LLMClientError) as refused:
            load_config(provider=provider)
        assert str(refused.value) == CLI_PROVIDERS_LOCAL_ONLY
    with pytest.raises(LLMClientError) as none_set:
        load_config()
    assert str(none_set.value) == NO_HOSTED_AI_CREDENTIAL
    with pytest.raises(LLMClientError) as none_to_try:
        generate_text("system", "input")
    assert str(none_to_try.value) == NO_HOSTED_AI_CREDENTIAL

    monkeypatch.setenv("ANTHROPIC_API_KEY", synthetic_secret("anthropic"))
    assert resolve_auto_providers() == ["anthropic-api"]
    assert load_config().provider == "anthropic-api"
    monkeypatch.setenv("OPENAI_API_KEY", synthetic_secret("openai"))
    assert resolve_auto_providers() == ["openai-api", "anthropic-api"]
    assert load_config().provider == "openai-api"


# --- the owner's values never reach the stream ---


def emit(*lines: str) -> str:
    return "".join(f"print({line!r}, flush=True)\n" for line in lines)


def event_text(run: Run) -> str:
    return json.dumps([event.data for event in run.events])


def test_a_credential_value_never_reaches_the_event_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = new_owner()
    secret = synthetic_secret("apikey")
    structured = json.dumps({"event": "progress", "message": f"provider said {secret}"})
    script = emit(f"error from the provider: {secret}", f"::harrier::{structured}")
    key = credentials_key()
    credentials = {
        owner: [stored(key, owner, "ai", {"provider": "openai-api", "key": secret, "model": "m"})]
    }
    hosted = hosted_manager(tmp_path, monkeypatch, script, credentials)
    run = run_to_end(hosted.manager, owner)
    assert run.state == "succeeded"
    text = event_text(run)
    assert secret not in text
    assert text.count(SECRET_REDACTION) == 2
    progress = [event for event in run.events if event.type == "progress"]
    assert progress[0].data["message"] == f"provider said {SECRET_REDACTION}"
    # Neither the provider nor the model is secret.
    assert run.secrets == frozenset()


def test_a_telegram_chat_id_never_reaches_the_event_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = new_owner()
    chat = str(uuid.uuid4().int)[:12]
    bot = synthetic_secret("bot")
    summary = {"sent": True, "chat": chat, "nested": {"to": [chat]}}
    script = emit(
        f"telegram: sent to chat {chat}",
        "::harrier::" + json.dumps({"event": "progress", "message": f"chat {chat}"}),
        "::harrier::" + json.dumps({"event": "summary", **summary}),
        json.dumps(summary),
    )
    key = credentials_key()
    credentials = {owner: [stored(key, owner, "telegram", {"bot_token": bot, "chat_id": chat})]}
    hosted = hosted_manager(tmp_path, monkeypatch, script, credentials)
    run = run_to_end(hosted.manager, owner)
    assert run.state == "succeeded"
    text = event_text(run)
    assert chat not in text
    summaries = [event for event in run.events if event.type == "summary"]
    assert summaries[0].data["chat"] == SECRET_REDACTION
    assert summaries[0].data["nested"] == {"to": [SECRET_REDACTION]}


# --- keeping the machine awake ---


def test_an_active_run_keeps_the_machine_awake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    signal = tmp_path / "go-on"
    script = waits_for(signal)
    hosted = hosted_manager(tmp_path, monkeypatch, script)
    beats = hosted.beats
    url = f"https://api.example.test{HEALTH_PATH}"

    async def until(condition: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() > deadline:
                signal.write_text("", encoding="utf-8")
                raise AssertionError(f"no beat came: {beats.sent}")
            await asyncio.sleep(0.01)

    async def go() -> tuple[int, int, Run]:
        run = await hosted.manager.start("demo", owner=new_owner())
        await until(lambda: len(beats.sent) >= 3)
        # Beats fail from now on; the run goes on.
        beats.fail = True
        await until(lambda: len(beats.sent) >= 5)
        signal.write_text("", encoding="utf-8")
        ended = await hosted.manager.wait(run.id)
        # One interval for the loop to see no active run.
        await asyncio.sleep(0.2)
        after_end = len(beats.sent)
        await asyncio.sleep(0.2)
        return after_end, len(beats.sent), ended

    with caplog.at_level(logging.WARNING):
        after_end, later, run = asyncio.run(go())
    assert set(beats.sent) == {url}
    assert run.state == "succeeded"
    assert after_end == later
    assert "a keep-awake beat failed: ConnectionError" in caplog.text
    assert "api.example.test" not in caplog.text

    # Locally nothing is sent.
    monkeypatch.delenv("HARRIER_AUTH")
    beats.sent.clear()
    local = run_to_end(hosted.manager, "")
    signal.unlink()
    assert local.state == "succeeded" or local.state in TERMINAL_STATES
    assert beats.sent == []


# --- binding a run from another caller (spec 108) ---


def test_a_run_binds_without_the_api(tmp_path: Path) -> None:
    """The binding takes no API state: a caller with an owner, the owner's
    ciphertexts and the key binds a run, adds only `TZ`, and is told which
    credential is missing."""
    owner = new_owner()
    key = credentials_key()
    apify = synthetic_secret("apify")
    server_env = {"PATH": "/bin", "HARRIER_RUN_DATABASE_URL": RUN_DATABASE_URL, "TZ": "UTC"}
    bound = bind_run(
        server_env=server_env,
        owner=owner,
        root=tmp_path,
        stored=[stored(key, owner, "apify", {"token": apify})],
        key=key,
        extra={"TZ": "Europe/Amsterdam"},
    )
    assert bound.env["TZ"] == "Europe/Amsterdam"
    assert bound.env["APIFY_TOKEN"] == apify
    assert bound.secrets == frozenset({apify})
    assert apify not in repr(bound)
    with pytest.raises(ExtraVariableRefused):
        bind_run(
            server_env=server_env,
            owner=owner,
            root=tmp_path,
            stored=[],
            key=key,
            extra={"PATH": "/elsewhere"},
        )
    with pytest.raises(RunCredentialMissing) as missing:
        bind_run(
            server_env=server_env, owner=owner, root=tmp_path, stored=[], key=key, needs=("ai",)
        )
    assert missing.value.kind == "ai"
