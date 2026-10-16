"""A hosted run's binding to its owner (spec 106).

Hosted, a run's process never inherits the API's environment. It gets the
environment `build_run_env` returns and nothing else: a few configuration
names copied from the API's environment when set, the names the run
manager sets (its owner, its owner's run directory, and the run login's
database URL under the name the CLI reads), and its owner's credentials
under the names the domain already reads (spec 116's kinds table). Its
working directory is its owner's run directory.

`bind_run` is the binding whole: it decrypts an owner's stored
credentials, refuses a run that needs a kind the owner has not set, and
returns the environment, the working directory and the values to scrub
from the run's events. It holds no API state, so a caller other than the
API, the hosted scheduler of spec 108, binds a run the same way: it reads
the owner's ciphertexts as the owner, calls this, and spawns the command
with what it returns. The spawned process is the caller's handle, to wait
on and to terminate.

Locally none of this runs: a local run inherits its environment (spec 035).

This is not a sandbox. Every run and the API run as one operating system
user, so a compromised run can read its siblings' environments (spec 106,
The trust model). The allowlist keeps secrets out of logs, crash output
and child tools; it does not stop an attacker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from harrier.connection import RUN_OWNER_VARIABLE, URL_VARIABLE
from harrier.credentials import (
    KIND_FIELDS,
    CredentialKind,
    CredentialsKey,
    StoredCredential,
    decrypt,
)
from harrier.filestore import (
    PUBLISHABLE_KEY_VARIABLE,
    STORAGE_TOKEN_VARIABLE,
    SUPABASE_URL_VARIABLE,
)
from harrier.logredact import store_password_forms_of

# The run login's URL on the API, passed to the run as its
# `HARRIER_DATABASE_URL` and never under its own name.
from harrier_api.hostedauth import RUN_DATABASE_URL_VARIABLE

# Copied from the API's environment when set: configuration, not secrets
# (spec 106, The child's environment). Chosen from the variable-name
# literals under `services/api/src`.
COPIED_VARIABLES: tuple[str, ...] = (
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
    # Where the run's Storage requests go, and the key they carry in
    # `apikey`: the project's origin and its publishable key, both public
    # (spec 107, Amendment (2026-10-16): PR 3's build).
    SUPABASE_URL_VARIABLE,
    PUBLISHABLE_KEY_VARIABLE,
)

# What a caller may add beyond the allowlist: the owner's timezone, which a
# scheduled run needs so a naive `datetime.now()` is the owner's local time
# (spec 108, "What this spec needs from spec 106's run binding").
EXTRA_VARIABLES: frozenset[str] = frozenset({"TZ"})

# Where owners' run directories sit under the API's data directory.
OWNERS_DIRECTORY = "owners"
RUN_DIRECTORY_MODE = 0o700

# Each kind's variables, as spec 116's kinds table names them. The `ai`
# kind's provider decides which key variable is set.
AI_KEY_VARIABLES: dict[str, str] = {
    "openai-api": "OPENAI_API_KEY",
    "anthropic-api": "ANTHROPIC_API_KEY",
}


class RunCredentialMissing(Exception):
    """A run needs a credential kind its owner has not set. The text is
    spec 106's 409 and names the kind, never a value."""

    def __init__(self, kind: CredentialKind) -> None:
        super().__init__(f"this run needs your {kind} credential; set it under credentials")
        self.kind: CredentialKind = kind


class ExtraVariableRefused(ValueError):
    """A caller asked for a variable beyond the allowlist that is not one
    of `EXTRA_VARIABLES`. Names the variable, never its value."""


def owner_run_dir(root: Path, owner: str) -> Path:
    """`<root>/owners/<owner>/`, created with mode 0700 on first use and set
    to it on every later one. Raises ValueError unless `owner` is a UUID, so
    nothing an owner sends can name another directory."""
    name = str(uuid.UUID(owner))
    directory = root / OWNERS_DIRECTORY / name
    directory.mkdir(parents=True, exist_ok=True, mode=RUN_DIRECTORY_MODE)
    # mkdir's mode is ignored when the directory exists, and the umask
    # applies when it does not.
    directory.chmod(RUN_DIRECTORY_MODE)
    return directory


def credential_variables(kind: CredentialKind, fields: Mapping[str, str]) -> dict[str, str]:
    """The variables `kind`'s decrypted `fields` reach a run as."""
    if kind == "ai":
        provider = fields["provider"]
        variables = {"AI_PROVIDER": provider, AI_KEY_VARIABLES[provider]: fields["key"]}
        if fields.get("model"):
            variables["AI_MODEL"] = fields["model"]
        return variables
    if kind == "apify":
        return {"APIFY_TOKEN": fields["token"]}
    if kind == "hunter":
        return {"HUNTER_API_KEY": fields["key"]}
    return {"TELEGRAM_BOT_TOKEN": fields["bot_token"], "TELEGRAM_CHAT_ID": fields["chat_id"]}


def secret_values(credentials: Mapping[CredentialKind, Mapping[str, str]]) -> frozenset[str]:
    """Every secret field value of `credentials` (spec 116's "Secret
    fields"), for the run's scrub. `provider` and `model` are not secret."""
    values: set[str] = set()
    for kind, fields in credentials.items():
        for name in KIND_FIELDS[kind].secret:
            value = fields.get(name, "")
            if value:
                values.add(value)
    return frozenset(values)


def build_run_env(
    server_env: Mapping[str, str],
    owner: str,
    run_dir: Path,
    credentials: Mapping[CredentialKind, Mapping[str, str]],
    *,
    extra: Mapping[str, str] | None = None,
    storage_token: str | None = None,
) -> dict[str, str]:
    """The whole environment of a hosted run of `owner`.

    `server_env` is the API's environment, of which only `COPIED_VARIABLES`
    and `HARRIER_RUN_DATABASE_URL` are read. Raises KeyError naming
    `HARRIER_RUN_DATABASE_URL` when it is absent, which the hosted start
    refuses before any run (spec 106), and ExtraVariableRefused for an
    `extra` name outside `EXTRA_VARIABLES`.

    `storage_token` is the token the run reaches Storage with (spec 107):
    the session token of the request that started it, or the token the
    scheduler minted for it. It is set as `HARRIER_STORAGE_TOKEN`, which the
    run takes out of its environment when it binds its store; never an
    argument, never the journal.
    """
    env = {name: server_env[name] for name in COPIED_VARIABLES if name in server_env}
    for name, value in (extra or {}).items():
        if name not in EXTRA_VARIABLES:
            raise ExtraVariableRefused(f"a run's environment does not take {name}")
        env[name] = value
    env["HOME"] = str(run_dir)
    env["HARRIER_DATA_DIR"] = str(run_dir)
    # The working directory is the owner's run directory, and `python -m`
    # would put it first on the import path: a `.py` file that landed there
    # would run in the owner's next run (privacy review of PR #316).
    env["PYTHONSAFEPATH"] = "1"
    env[URL_VARIABLE] = server_env[RUN_DATABASE_URL_VARIABLE]
    env[RUN_OWNER_VARIABLE] = str(uuid.UUID(owner))
    if storage_token:
        env[STORAGE_TOKEN_VARIABLE] = storage_token
    for kind, fields in credentials.items():
        env.update(credential_variables(kind, fields))
    return env


@dataclass(frozen=True)
class BoundRun:
    """What a hosted run is spawned with. The environment and the secrets
    are kept out of the repr, so a bound run that reaches a log record
    carries no credential; its working directory names the owner."""

    env: dict[str, str] = field(repr=False)
    cwd: Path
    secrets: frozenset[str] = field(repr=False)


def bind_run(
    *,
    server_env: Mapping[str, str],
    owner: str,
    root: Path,
    stored: Iterable[StoredCredential],
    key: CredentialsKey,
    needs: Iterable[CredentialKind] = (),
    extra: Mapping[str, str] | None = None,
    storage_token: str | None = None,
) -> BoundRun:
    """Bind a run to `owner`: decrypt `stored`, the owner's ciphertexts read
    as the owner, and build the environment and working directory.

    Raises `RunCredentialMissing` for the first kind of `needs` the owner
    has not set, and `harrier.credentials.CredentialUnreadable` for a stored
    credential that does not decrypt, both before a directory exists. The
    caller logs the latter's kind and key id, never a value or the owner.

    `storage_token`, when given, reaches the run as `build_run_env` says,
    and joins the values scrubbed from its events.
    """
    decrypted: dict[CredentialKind, dict[str, str]] = {}
    for credential in stored:
        decrypted[credential.kind] = decrypt(key, owner, credential)
    for kind in needs:
        if kind not in decrypted:
            raise RunCredentialMissing(kind)
    run_dir = owner_run_dir(root, owner)
    env = build_run_env(
        server_env, owner, run_dir, decrypted, extra=extra, storage_token=storage_token
    )
    # The run login's password, in every form a driver or `repr` can print
    # it: the child holds it, and its output reaches the stream (privacy
    # review of PR #316).
    login = store_password_forms_of(server_env[RUN_DATABASE_URL_VARIABLE])
    token = frozenset({storage_token}) if storage_token else frozenset[str]()
    return BoundRun(
        env=env, cwd=run_dir, secrets=secret_values(decrypted) | frozenset(login) | token
    )
