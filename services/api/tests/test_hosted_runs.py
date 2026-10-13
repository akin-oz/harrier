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
from harrier_api.hostedruns import BEAT_INTERVAL_SECONDS, HEALTH_PATH, HostedRuns, KeepAwake
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
    slept: list[float] = field(default_factory=lambda: [])

    def send(self, url: str) -> int:
        self.sent.append(url)
        if self.fail:
            raise ConnectionError("synthetic failure")
        return 200

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
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
    begun: int = 0

    def begin(
        self, run: Run, needs: Iterable[CredentialKind], extra: Mapping[str, str] | None = None
    ) -> BoundRun:
        self.begun += 1
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
        # Gmail watch answers in spec 109's words, which name the watch.
        named = "Gmail watch" if kind == "gmail-watch" else kind
        assert answer.runs or named in answer.refusal, kind
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
    """Refused before the kind is read and before anything is bound, for a
    kind that runs and a kind the table refuses alike."""
    hosted = hosted_manager(tmp_path, monkeypatch)
    for kind in ("demo", "tailor"):
        with pytest.raises(RunsNotHosted) as refused:
            asyncio.run(hosted.manager.start(kind, owner=""))
        assert str(refused.value) == NO_OWNER_DETAIL
    assert hosted.records.begun == 0
    assert hosted.manager.list_runs() == []
    assert hosted.records.rows == {}


def test_concurrent_starts_of_one_request_make_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hosted start awaits the store between the lock check and the
    registration, so starts take turns: two at once of the same owner,
    kind and target make one run, not two (test integrity review of PR
    #316)."""
    signal = tmp_path / "go-on"
    hosted = hosted_manager(tmp_path, monkeypatch, waits_for(signal))
    owner = new_owner()

    async def go() -> tuple[Run, Run]:
        first, second = await asyncio.gather(
            hosted.manager.start("demo", owner=owner), hosted.manager.start("demo", owner=owner)
        )
        signal.write_text("", encoding="utf-8")
        await hosted.manager.wait(first.id)
        return first, second

    first, second = asyncio.run(go())
    assert first.id == second.id
    assert hosted.records.begun == 1


def test_run_records_hold_the_run_managers_states() -> None:
    """Migration 19's CHECK lists `RunState`'s values, written out in the
    statement so the migration stays what it was; a state added to one and
    not the other fails here (data integrity review of PR #316)."""
    from typing import get_args

    from harrier.tracker.schema import POSTGRES_RUN_RECORDS
    from harrier_api.runs import RunState

    create = POSTGRES_RUN_RECORDS[0]
    listed = create.split("CHECK (state IN (", 1)[1].split("))", 1)[0]
    assert {part.strip().strip("'") for part in listed.split(",")} == set(get_args(RunState))


def test_a_hosted_runs_stream_ends_with_its_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state change event is appended before the record's write is
    awaited, so a stream that reads between them still gets it (data
    integrity review of PR #316)."""
    hosted = hosted_manager(tmp_path, monkeypatch, emit("one line"))
    slow = hosted.records.record

    def record(run: Run) -> None:
        time.sleep(0.3)
        slow(run)

    monkeypatch.setattr(hosted.records, "record", record)

    async def go() -> list[tuple[str, object]]:
        run = await hosted.manager.start("demo", owner=new_owner())
        seen: list[tuple[str, object]] = []
        async for event in hosted.manager.stream(run.id):
            seen.append((event.type, event.data.get("state")))
            await asyncio.sleep(0.1)
        await hosted.manager.wait(run.id)
        return seen

    seen = asyncio.run(go())
    assert seen[-1] == ("state_change", "succeeded"), seen


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


# Spec 106's "Copied from the server's environment when set", written out
# here rather than read from the code, so a name the code adds is a failure.
SPEC_COPIED = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "PLAYWRIGHT_BROWSERS_PATH",
    "HARRIER_REVISION",
    "HARRIER_BUILT_AT",
    "HARRIER_LOG_LEVEL",
    "APIFY_LINKEDIN_ACTOR",
    "APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR",
    "APIFY_ACADEMIC_ACTOR",
    "APIFY_ACADEMIC_PORTALS",
    "ANTHROPIC_MAX_TOKENS",
)


def test_the_child_environment_holds_only_the_allowlist_and_its_owners_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert set(COPIED_VARIABLES) == set(SPEC_COPIED)
    assert set(SPEC_COPIED).isdisjoint(EXCLUDED)
    sentinel = synthetic_secret("sentinel")
    random_name = f"SYNTHETIC_{uuid.uuid4().hex.upper()}"
    hosted = hosted_manager(tmp_path, monkeypatch)
    # Set after the manager, so the run URL and hosted mode stay what the
    # manager needs, and each excluded name holds the sentinel otherwise.
    # Nothing writes an excluded name after this.
    for name in (*EXCLUDED, random_name):
        if name not in ("HARRIER_RUN_DATABASE_URL", "HARRIER_AUTH"):
            monkeypatch.setenv(name, sentinel)
    copied = {name: f"copied-{name}" for name in SPEC_COPIED if name != "PATH"}
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
        "PYTHONSAFEPATH": digest("1"),
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


def test_a_hosted_run_carries_only_its_owners_telegram_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 109's criterion, moved to spec 106: owner A's run holds A's
    Telegram settings, owner B's run with none holds neither, and no run
    holds a `GMAIL_` variable or the operator's Telegram settings, though
    the server's environment holds them."""
    hosted = hosted_manager(tmp_path, monkeypatch)
    operator = synthetic_secret("operator")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GMAIL_TOKEN_FILE", "GMAIL_LABEL"):
        monkeypatch.setenv(name, operator)
    a, b = new_owner(), new_owner()
    bot, chat = synthetic_secret("bot"), str(uuid.uuid4().int)[:12]
    key = credentials_key()
    hosted.records.credentials[a] = [
        stored(key, a, "telegram", {"bot_token": bot, "chat_id": chat})
    ]

    theirs = printed(run_to_end(hosted.manager, a))["env"]
    assert theirs["TELEGRAM_BOT_TOKEN"] == digest(bot)
    assert theirs["TELEGRAM_CHAT_ID"] == digest(chat)
    none = printed(run_to_end(hosted.manager, b))["env"]
    assert "TELEGRAM_BOT_TOKEN" not in none and "TELEGRAM_CHAT_ID" not in none
    for held in (theirs, none):
        assert not [name for name in held if name.startswith("GMAIL_")]
        assert digest(operator) not in held.values()


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
    # One directory exists already, readable by others: the run makes it
    # private as it is, not only when it creates it.
    early = tmp_path / "api-data" / "owners" / owners[0]
    early.mkdir(parents=True)
    early.chmod(0o755)
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


@pytest.mark.dotenv_fallback
def test_the_password_scrub_reads_no_env_file_in_a_hosted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store password scrub falls back to `.env` for the URL whose
    password it masks. In a hosted run it does not, so a `.env` in the run's
    working directory adds nothing to what the run treats as its store."""
    from harrier.logredact import store_password_forms

    password = f"dotenvpw{uuid.uuid4().hex}"
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"HARRIER_DATABASE_URL=postgresql://u:{password}@127.0.0.1:1/db\n", encoding="utf-8"
    )
    monkeypatch.delenv("HARRIER_DATABASE_URL")
    # Locally the file is read.
    assert password in store_password_forms()
    monkeypatch.setenv(RUN_OWNER_VARIABLE, new_owner())
    assert store_password_forms() == ()


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
    # Set and removed through monkeypatch, so the test's end removes what
    # a `.env` read may have written, whatever fails first.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "placeholder")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
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


def test_a_run_that_cannot_open_its_store_says_one_line(
    cli_on_postgres: tuple[list[object], pytest.MonkeyPatch],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run's output reaches its owner, so a store that refuses the run
    is one line naming nothing of the store, not a traceback with its host
    (data integrity review of PR #316)."""
    from harrier.connection import StoreConnectionError

    _opened, monkeypatch = cli_on_postgres
    monkeypatch.setenv(RUN_OWNER_VARIABLE, new_owner())

    def refused(*_args: object, owner: str | None = None) -> object:
        raise StoreConnectionError("cannot connect to the Postgres store at db.internal:5432/x")

    monkeypatch.setattr(cli, "open_store", refused)
    assert cli.main(["reconsider"]) == 1
    assert capsys.readouterr().err.strip() == f"error: {cli.RUN_STORE_REFUSED}"


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
    # The scrub set is dropped once the run's process is reaped.
    assert run.secrets == frozenset()


def test_every_place_an_event_carries_text_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact-value scrub reaches every string a structured event holds,
    at any depth and in its keys and type, a chat id written as a number,
    and the run login's password, which the child holds too (privacy
    review of PR #316)."""
    owner = new_owner()
    secret = synthetic_secret("apify")
    chat = str(uuid.uuid4().int)[:12]
    password = RUN_DATABASE_URL.split(":")[2].split("@")[0]
    events = [
        {"event": f"type {secret}", "message": "m"},
        {"event": "progress", secret: 1, "items": [{"msg": secret}, [secret]]},
        {"event": "summary", "chat": int(chat)},
    ]
    script = emit(*(f"::harrier::{json.dumps(event)}" for event in events), f"bare {password}")
    key = credentials_key()
    credentials = {
        owner: [
            stored(key, owner, "apify", {"token": secret}),
            stored(key, owner, "telegram", {"bot_token": synthetic_secret("b"), "chat_id": chat}),
        ]
    }
    hosted = hosted_manager(tmp_path, monkeypatch, script, credentials)
    run = run_to_end(hosted.manager, owner)
    assert run.state == "succeeded"
    shown = json.dumps([[event.type, event.data] for event in run.events])
    for value in (secret, chat, password):
        assert value not in shown
    assert f"type {SECRET_REDACTION}" in [event.type for event in run.events]
    summary = next(event for event in run.events if event.type == "summary")
    assert summary.data["chat"] == SECRET_REDACTION


def test_a_hosted_run_imports_nothing_from_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's working directory is not on its import path, so a `.py`
    file that lands in the owner's directory never runs in the owner's run
    (privacy review of PR #316)."""
    owner = new_owner()
    directory = tmp_path / "api-data" / "owners" / owner
    directory.mkdir(parents=True)
    (directory / "harrier_planted.py").write_text("PLANTED = True\n", encoding="utf-8")
    script = (
        "import importlib.util, json\n"
        "print(json.dumps({'found': importlib.util.find_spec('harrier_planted') is not None}),"
        " flush=True)\n"
    )
    hosted = hosted_manager(tmp_path, monkeypatch, script)
    run = run_to_end(hosted.manager, owner)
    assert printed(run) == {"found": False}


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

    # The beats wait the interval they were given.
    assert set(beats.slept) == {1.0}
    # The interval the app uses is below the 80 s after which spec 115's F1
    # saw the proxy stop the machine.
    assert BEAT_INTERVAL_SECONDS < 80

    # Locally nothing is sent: a local manager has no beats at all.
    monkeypatch.delenv("HARRIER_AUTH")
    local_manager = RunManager(
        journal_path=tmp_path / "local.jsonl",
        kind_commands={"demo": [sys.executable, "-c", ""]},
    )
    assert local_manager.hosted_runs is None
    local = run_to_end(local_manager, "")
    assert local.state == "succeeded"


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
    # The owner's secret fields and the run login's password.
    password = RUN_DATABASE_URL.split(":")[2].split("@")[0]
    assert apify in bound.secrets and password in bound.secrets
    assert "Europe/Amsterdam" not in bound.secrets
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
