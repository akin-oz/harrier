"""The operator writes one hosted account from the command line (spec 144).

`harrier --owner <user id> <command>` runs a short list of commands against
one hosted account instead of the local store. The hosted store is the URL
in `HARRIER_ACCOUNT_DATABASE_URL`, exported in the shell, and its login must
pass the run login's checks (spec 134), as spec 141's import's must. Every
transaction runs as the owner under the owner's row policy (spec 106), and
each command calls the same domain function it calls locally: no SQL here
writes a row.

Nothing here prints. A refusal is an `AccountRefused` whose text names the
check, never the owner id, the URL or the host.
"""

from __future__ import annotations

from collections.abc import Mapping

from harrier.connection import (
    URL_VARIABLE,
    AccountRefused,
    Connection,
    StoreUrlError,
    open_store,
    owner_claims,
    store_target,
)

ACCOUNT_URL_VARIABLE = "HARRIER_ACCOUNT_DATABASE_URL"

# The commands `--owner` runs, by `subcommand_name`. Any other is refused
# before anything opens (spec 144, "What runs").
ACCOUNT_COMMANDS: frozenset[str] = frozenset(
    {
        "tracks add",
        "tracks list",
        "profile put",
        "profile list",
        "profile check",
        "move",
    }
)

NO_ACCOUNT = "that owner sees no track: the account does not exist or has not been invited"


def account_refusal(command: str) -> str:
    """A command outside `ACCOUNT_COMMANDS` given `--owner`."""
    return f"harrier {command} does not run against a hosted account (spec 144)"


def account_url(environ: Mapping[str, str]) -> str:
    """The URL `HARRIER_ACCOUNT_DATABASE_URL` names, or a refusal that names
    the variable and never its value."""
    raw = environ.get(ACCOUNT_URL_VARIABLE, "")
    if not raw.strip():
        raise AccountRefused(
            f"{ACCOUNT_URL_VARIABLE} is not set; export the run login's URL in the shell "
            "for this command"
        )
    try:
        target = store_target({URL_VARIABLE: raw})
    except StoreUrlError as error:
        raise AccountRefused(str(error).replace(URL_VARIABLE, ACCOUNT_URL_VARIABLE)) from None
    if not target.is_postgres:
        raise AccountRefused(f"{ACCOUNT_URL_VARIABLE} names no Postgres store")
    return target.url


def open_account(owner: str, environ: Mapping[str, str]) -> Connection:
    """The hosted store as `owner`, after the URL, the login role, the
    store's version and the account itself have passed.

    Raises AccountRefused for a check, and StoreError (a
    StoreConnectionError for an unreachable store) otherwise, with nothing
    written either way.
    """
    # Imported here: the import module reads the local SQLite file's tables,
    # which nothing that only opens an account needs at import time.
    from harrier.tracker.importlocal import login_problems

    claims = owner_claims(owner)
    url = account_url(environ)
    problems = login_problems(url)
    if problems:
        raise AccountRefused(
            f"{ACCOUNT_URL_VARIABLE} names a login role this command refuses: "
            + "; ".join(problems)
        )
    conn = open_store({URL_VARIABLE: url}, owner=str(claims["sub"]))
    try:
        with conn.transaction():
            row = conn.execute("SELECT EXISTS (SELECT 1 FROM tracks)").fetchone()
        if row is None or not row[0]:
            raise AccountRefused(NO_ACCOUNT)
    except BaseException:
        conn.close()
        raise
    return conn
