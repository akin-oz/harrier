"""`harrier operator erase-test-owners` (spec 143).

The store is a throwaway Postgres database with spec 105's shim and spec
111's erase, and the admin API is `test_operator.FakeSupabase` over a mock
transport, so no test reaches the network. The shim's `auth.users` holds only
an id, so the fixture here adds the columns and the table the rule reads, as
Supabase has them. `test_supabase_project` adds an `email` column itself, so
the shim stays as it is. Every id, address and number is synthetic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pg_support import MIGRATOR_ROLE, fresh_database, new_owner, superuser_url
from test_operator import FakeSupabase, configure, run
from test_owner_policy import fill

from harrier import operator
from harrier.db import data_dir
from harrier.pgstore import migrate_postgres
from harrier.tracker.schema import OWNED_TABLES

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# The columns and the table of Supabase's auth schema that the rule reads. The
# identities table references users with a cascade, as Supabase's does, so an
# auth delete takes a user's identities with it.
AUTH_ADDITIONS: tuple[str, ...] = (
    "ALTER TABLE auth.users ADD COLUMN email text",
    "ALTER TABLE auth.users ADD COLUMN phone text",
    "ALTER TABLE auth.users ADD COLUMN is_anonymous boolean NOT NULL DEFAULT false",
    "ALTER TABLE auth.users ADD COLUMN last_sign_in_at timestamptz",
    """
    CREATE TABLE auth.identities (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE
    )
    """,
    f"GRANT SELECT, INSERT ON auth.identities TO {MIGRATOR_ROLE}",
)


class Store:
    """A migrated, shimmed store whose `auth.users` has Supabase's columns,
    and a fake Supabase over it."""

    def __init__(self, db: PgConnection, fake: FakeSupabase) -> None:
        self.db = db
        self.fake = fake

    def owner(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        anonymous: bool = False,
        signed_in: bool = False,
        identity: bool = False,
    ) -> str:
        """An owner with rows in every owned table. With no argument it is a
        synthetic test owner: what `new_owner` makes."""
        owner = new_owner(self.db)
        self.db.execute(
            "UPDATE auth.users SET email = %s, phone = %s, is_anonymous = %s, "
            "last_sign_in_at = CASE WHEN %s THEN now() END WHERE id = %s",
            (email, phone, anonymous, signed_in, owner),
        )
        if identity:
            self.db.execute("INSERT INTO auth.identities (user_id) VALUES (%s)", (owner,))
        fill(self.db, owner)
        return owner

    def synthetic(self, count: int) -> list[str]:
        """`count` synthetic owners, in the order `auth.users` id gives."""
        return sorted(self.owner() for _ in range(count))

    def rows(self, owner: str) -> int:
        return sum(self.table_rows(table, [owner]) for table in OWNED_TABLES)

    def table_rows(self, table: str, owners: list[str]) -> int:
        found = self.db.execute(
            f"SELECT count(*) FROM {table} WHERE owner_id = ANY(%s::uuid[])".encode(), (owners,)
        ).fetchone()
        assert found is not None
        return int(found[0])

    def users(self) -> set[str]:
        return {str(row[0]) for row in self.db.execute("SELECT id FROM auth.users").fetchall()}

    def snapshot(self) -> dict[str, int]:
        """The rows of every owned table, and the users, as counts."""
        counts: dict[str, int] = {}
        for table in OWNED_TABLES:
            found = self.db.execute(f"SELECT count(*) FROM {table}".encode()).fetchone()
            assert found is not None
            counts[table] = int(found[0])
        counts["auth.users"] = len(self.users())
        return counts

    def asked_about(self) -> list[str]:
        """The user ids the fake's `get` route was called for, in order."""
        return [request.url.path.rsplit("/", 1)[-1] for request in self.fake.of_kind("get")]

    def ledger(self) -> list[dict[str, Any]]:
        path = data_dir() / "hosted" / "erasures.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Store]:
    import psycopg

    with fresh_database() as url:
        migrate_postgres(url)
        with psycopg.connect(superuser_url(url), autocommit=True) as admin:
            for statement in AUTH_ADDITIONS:
                admin.execute(statement.encode())
        with psycopg.connect(url, autocommit=True) as db:
            server = FakeSupabase(db)
            monkeypatch.setattr(operator, "http_client", server.client)
            configure(monkeypatch, tmp_path, url)
            yield Store(db, server)


def real_accounts(store: Store) -> list[str]:
    """One owner for each way a real account fails the rule."""
    return [
        store.owner(email="invited@example.test", identity=True),
        store.owner(phone="+10000000001"),
        store.owner(anonymous=True),
        store.owner(signed_in=True),
        store.owner(identity=True),
        store.owner(email="unconfirmed@example.test"),
    ]


def test_erase_test_owners_matches_only_synthetic_owners(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three synthetic owners and six users that each fail one clause of the
    rule (an email with an identity, a phone alone, an anonymous user, one who
    signed in, one with only an identity, one with only an email). The report
    counts three, and the confirmed run erases those three, their rows and
    their `auth.users` rows, and nothing of the others."""
    real = real_accounts(store)
    test_owners = store.synthetic(3)
    kept = {owner: store.rows(owner) for owner in real}
    held = {table: store.table_rows(table, test_owners) for table in OWNED_TABLES}
    assert all(held.values())

    code, out, err = run(["operator", "erase-test-owners"], capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "synthetic test owners: 3"
    for table in OWNED_TABLES:
        assert f"rows held: {table} {held[table]}" in out.splitlines()

    code, out, err = run(["operator", "erase-test-owners", "--confirm", "3"], capsys)
    assert (code, err) == (0, "")
    assert store.asked_about() == test_owners
    assert store.users() == set(real)
    assert {owner: store.rows(owner) for owner in real} == kept
    assert all(store.rows(owner) == 0 for owner in test_owners)
    last = out.splitlines()[-1]
    assert last.startswith("erased 3, skipped 0; rows deleted: ")
    for table in OWNED_TABLES:
        assert f"{table} {held[table]}" in last
    assert [line["user_id"] for line in store.ledger()] == test_owners


def test_erase_test_owners_reports_without_writing(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without `--confirm` the run calls no admin route, writes no ledger and
    changes no table. Its output is the count, one line per owned table, and
    the instruction: no user id, and no UUID of any kind."""
    real = real_accounts(store)
    test_owners = store.synthetic(3)
    before = store.snapshot()

    code, out, err = run(["operator", "erase-test-owners"], capsys)

    assert (code, err) == (0, "")
    assert store.fake.calls == [] and store.ledger() == []
    assert store.snapshot() == before
    lines = out.splitlines()
    assert lines[0] == "synthetic test owners: 3"
    assert lines[-1] == "nothing erased; repeat with --confirm 3"
    assert [line.split()[2] for line in lines[1:-1]] == list(OWNED_TABLES)
    assert all(re.fullmatch(r"rows held: \w+ \d+", line) for line in lines[1:-1])
    assert UUID_TEXT.search(out) is None
    for owner in [*test_owners, *real]:
        assert owner not in out


def test_erase_test_owners_refuses_a_stale_count(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `--confirm` above or below the count found now exits 1 before any
    admin call, names both numbers, and erases nothing."""
    store.synthetic(3)
    before = store.snapshot()
    for wrong in (2, 4, 0):
        code, out, err = run(["operator", "erase-test-owners", "--confirm", str(wrong)], capsys)
        assert (code, out) == (1, "")
        assert err == (
            f"error: --confirm {wrong} does not match the 3 synthetic owners found now; "
            "nothing was erased\n"
        )
        assert store.fake.calls == [] and store.ledger() == []
        assert store.snapshot() == before


def test_erase_test_owners_rechecks_each_owner(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """An owner who signs in after the count is skipped when its turn comes:
    no admin call is made for it, and its user and rows stay. The others are
    erased in `auth.users` id order."""
    first, second, third = store.synthetic(3)
    kept = store.rows(second)

    def sign_in(user: str) -> None:
        if user == first:
            store.db.execute(
                "UPDATE auth.users SET last_sign_in_at = now() WHERE id = %s", (second,)
            )

    store.fake.before["get"] = sign_in

    code, out, err = run(["operator", "erase-test-owners", "--confirm", "3"], capsys)

    assert (code, err) == (0, "")
    assert store.asked_about() == [first, third]
    assert second in store.users() and store.rows(second) == kept
    assert first not in store.users() and third not in store.users()
    assert out.splitlines()[-1].startswith("erased 2, skipped 1; rows deleted: ")


def test_erase_test_owners_resumes_after_a_failure(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second owner's auth delete is refused. The run exits 1 naming step
    4, the first owner stays erased, and the later owners are untouched. The
    second owner has lost its rows but is still in `auth.users`, so a re-run
    finds it with the rest, and finishes."""
    first, second, third, fourth = store.synthetic(4)
    later = {owner: store.rows(owner) for owner in (third, fourth)}
    store.fake.failures["delete"] = [0, 500]

    code, _, err = run(["operator", "erase-test-owners", "--confirm", "4"], capsys)

    assert code == 1
    assert "error: step 4 (auth): the server answered 500\n" in err
    assert err.endswith("stopped after 1 erased and 0 skipped; run the command again to continue\n")
    assert first not in store.users() and store.rows(first) == 0
    assert second in store.users() and store.rows(second) == 0
    assert {owner: store.rows(owner) for owner in (third, fourth)} == later
    assert [line["complete"] for line in store.ledger()] == [True, False]

    code, out, err = run(["operator", "erase-test-owners"], capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "synthetic test owners: 3"

    code, out, err = run(["operator", "erase-test-owners", "--confirm", "3"], capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1].startswith("erased 3, skipped 0; rows deleted: ")
    assert store.users() == set()
    assert all(store.rows(owner) == 0 for owner in (first, second, third, fourth))
    assert [line["complete"] for line in store.ledger()] == [True, False, True, True, True]


def test_erase_test_owners_prints_one_progress_line_per_ten(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no synthetic owner `--confirm 0` erases nothing. With
    twenty-three, a progress line follows the tenth and the twentieth, then
    the final line; the steps of each erase are not printed."""
    zeros = ", ".join(f"{table} 0" for table in OWNED_TABLES)
    code, out, err = run(["operator", "erase-test-owners", "--confirm", "0"], capsys)
    assert (code, err) == (0, "")
    assert out == f"erased 0, skipped 0; rows deleted: {zeros}\n"

    store.synthetic(23)
    code, out, err = run(["operator", "erase-test-owners", "--confirm", "23"], capsys)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[:2] == ["10 of 23 owners done", "20 of 23 owners done"]
    assert len(lines) == 3 and lines[2].startswith("erased 23, skipped 0; ")
    assert store.users() == set()
