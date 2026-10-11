---
spec: 111
title: Sign-up, export and deletion
status: accepted
approved: yes
milestone: M10
depends: [105, 107, 110, 112]
---

# Spec 111: Sign-up, export and deletion

Akin approved the stub in chat on 2026-10-10; the frontmatter flip reached main inside PR #222, whose message does not say so, and this line records it. Refined 2026-10-10 after approval of the stub. This text is an amendment
to the approved stub and goes to Akin before implementation.

## Problem

A hosted service holds other people's personal data. They need a controlled
way in and a way to take their data out or remove it. Akin chose invite-only
sign-up until export and deletion ship (spec 102, open decision 5).
ADR-013's Consequences say "a tenant's data can be exported and deleted on
request; spec 111 owns it".

Line numbers below are from `main` at `44ac0d7` on 2026-10-10. The stub cited
older ones (`schema.py:441-445`, `:463`, `:545`, `:549`, `:776`, `:780`); the
same statements now sit at the lines given here, found with `grep -n` for
`harrier_refuse`, `CREATE TRIGGER` and `TRUNCATE` in
`services/api/src/harrier/tracker/schema.py`.

**Deletion collides with the store's own refusals.** The post-merge review
of PRs #194 and #207 (architecture 5, 2026-10-10) found it. On Postgres,
`job_events` is append-only and `tracks` are never deleted, enforced by one
function, `harrier_refuse()` (`schema.py:482-486`), which raises its trigger
argument unconditionally. It sits behind row triggers
`tracks_are_never_deleted` (`:503-504`), `job_events_append_only_update`
(`:585-586`) and `job_events_append_only_delete` (`:589-590`) and, since
spec 105, statement triggers on TRUNCATE (`:817-818`, `:821-822`), which
fire even for a superuser
(`services/api/tests/test_owner_policy.py::test_truncate_cannot_empty_events_or_tracks`).
An owner's events and tracks cannot be deleted at all. Spec 079 made
append-only a database rule, not a convention
(`specs/079-decisions-are-recorded-as-events.md:149-150`), and nothing
written since says when, if ever, it yields.

The one way through today is the one migration 10 used for the seeded
track: `ALTER TABLE tracks DISABLE TRIGGER` (`schema.py:746-748`). That
statement takes a `SHARE ROW EXCLUSIVE` lock (PostgreSQL, ALTER TABLE),
which conflicts with the `ROW EXCLUSIVE` lock every insert, update and
delete takes. In a migration that is fine. To delete one owner while others
use the store, it would make every other owner's writes to that table wait,
and while the trigger is off it is off for every session.

**No auth user can be deleted.** Every owned table carries `owner_id uuid
NOT NULL DEFAULT auth.uid() REFERENCES auth.users (id)` with no `ON DELETE`
action (`schema.py:749-755`, spec 105). Deleting a user who owns rows is
refused. The post-merge review of PR #216 (2026-10-10) added that the
sign-up trigger, `harrier_new_owner()` (`schema.py:829-841`), gives every
user a track 1 the moment the user row is inserted. So every auth user owns
a row, including one who was invited and never signed in, and the Supabase
dashboard's delete fails the same way: Supabase's own troubleshooting guide
names a foreign key from a public table to `auth.users` as the usual cause
of a failed delete. Today the only way to remove a user is to drop a
constraint or a trigger by hand.

**Sign-up is open by default.** A Supabase project lets anyone sign up
until "Allow new users to sign up" is turned off. With it off, "only
existing users can sign in" (Supabase, General configuration). Then a user
exists only if the operator creates one. Supabase's built-in email service
"will refuse to deliver messages to addresses that are not part of the
project's team" and is not meant for production (Supabase, Send emails with
custom SMTP), so an invitation email to a stranger does not arrive without
a custom SMTP provider. A custom SMTP provider is a vendor the amended
Local-first invariant does not name: the hosted deployment adds Supabase,
Fly.io and Vercel, nothing else (spec 102, Behavior).

**Erasure is an expectation, not a feature.** The target users are in the
EU (spec 102, open decision 2). The GDPR asks a controller to erase personal
data "without undue delay" when a ground applies (Article 17(1)), to answer
a request within one month, extendable by two (Article 12(3)), and to give
data under Article 20 in "a structured, commonly used and machine-readable
format". Supabase's database backups keep seven days on the Pro plan and do
not include Storage objects (Supabase, Database backups). None of this is
written down for harrier.

**Export and deletion are two reviews.** Deletion is a database change that
lets one function past the store's refusals, plus operator commands holding
a secret key. Export reads only what the owner may already read, through
the API as the owner, and changes the contract. Their risks and reviewers
differ, and export also waits on the browser (spec 114) and Storage (spec
107). `change-boundary` says to split the spec, not the branch. This spec
keeps sign-up and deletion, and moves export to spec 117 (Scope item 9,
Open decision 1).

## Scope

1. **A new Postgres-only migration (its version is the next free one when
   implemented), called "the erasure migration" below.**
   `POSTGRES_MIGRATIONS` in `services/api/src/harrier/tracker/schema.py`
   gains it, with the statements under Behavior. `MIGRATIONS` gains the
   same version with no statements, and `SINGLE_DIALECT_MIGRATIONS`
   declares it Postgres only, with the reason `owner erasure (spec 111);
   the local store has one owner, who deletes the file`.
2. **`harrier_refuse()` stands aside for one owner's erasure only.**
   Replaced by `CREATE OR REPLACE` in the erasure migration, which keeps its owner
   and its revoked grants (PostgreSQL, CREATE FUNCTION). No trigger is
   dropped, disabled or recreated.
3. **`harrier_erase_owner(uuid)`,** a `SECURITY DEFINER` function that
   deletes one owner's rows from every owned table, executable by
   `service_role` and by no other grantee.
4. **The catalog test learns the function.**
   `services/api/tests/test_owner_policy.py`: the definer allowlist in
   `catalog_problems` gains `harrier_erase_owner`, and the catalog fails
   when any harrier function is executable by `harrier_tenant`, or when
   `harrier_erase_owner` is executable by any grantee other than
   `service_role`. `service_role` still holds no table or sequence
   privilege (`NO_PRIVILEGE_ROLES` unchanged).
5. **An operator command group, `harrier operator`,** in
   `services/api/src/harrier_cli/main.py`, backed by a new module
   `services/api/src/harrier/operator.py`: `harrier operator invite` and
   `harrier operator erase`. It is the second command that runs on a
   Postgres URL, beside `harrier store`. It is one command group with spec
   108's `scheduler` and `schedule-report`, and each subcommand declares
   the database role it runs as (decision D2, 2026-10-11): `erase` runs
   as `service_role`; `invite` declares that it opens no database
   connection. The CLI gate
   (`harrier_cli/main.py:2913-2916` today, restructured by spec 112) gains
   the class "operator: each subcommand runs as its declared role",
   written into spec 112 as its Amendment (2026-10-11) with its planned
   test_the_postgres_gate_has_operator_and_run_classes.
6. **A Supabase admin client** in `harrier/operator.py`, over `httpx`
   (already a dependency, `services/api/pyproject.toml:40`): create, read,
   ban and delete a user. Storage listing and deletion
   go through spec 107's seam (item 8).
7. **The project's sign-up settings** are stated under Behavior and checked
   by spec 110's deploy (item 8). This spec changes no Supabase setting
   itself; it has no Management API credential.
8. **Findings that belong to other specs** (specs 104, 105, 107, 110 and
   112) are listed under "Changes to approved specs". Each is written into
   its spec once Akin approves it with this one.
9. **Export is spec 117,** `specs/117-an-owner-exports-everything-they-own.md`,
   a stub created with this refinement, `approved: no`, carrying the
   Problem and headline under "Export" in Behavior. `specs/README.md`'s M10
   line lists it.
10. **ADR-013 Consequences** gain an "Amended (spec 111)" note: export is
    spec 117's, deletion is this spec's, and `job_events` and `tracks`
    yield to one owner's erasure and to nothing else.
11. **`docs/architecture.md`'s hosted section** names invite-only sign-up,
    the two operator commands, the erasure order, the erasure ledger, and
    the backup limitation, each with its test.

Classification: `harrier/operator.py` and the new tests sit under
`services/api/src/` and `services/api/tests/`, public by
`config/data-classification.json`. The erasure ledger sits under `data/`,
which the table classifies never-in-git (`data/**`). No other new path.

## Behavior

### Sign-up is by invitation only

The hosted Supabase project has these Auth settings (provisioned and
checked by spec 110):

| Setting | Value | Effect |
|---|---|---|
| Allow new users to sign up | off | "only existing users can sign in" (General configuration); the public signup endpoint answers `Signups not allowed for this instance` |
| Allow anonymous sign-ins | off | no anonymous users; spec 104 also refuses an anonymous token with 403 |
| OAuth providers | none enabled | no second door to user creation |

A user exists only when the operator creates one:

```
harrier operator invite
```

- Reads one email address from standard input, never from the command
  line, so the address stays out of shell history and the process list.
- Calls the auth server's admin user creation, `POST
  /auth/v1/admin/users`, with the address and `email_confirm` true, and
  no password. Supabase's `createUser` reference shows this form as
  "Auto-confirm the user's email"; it does not say whether an email is
  sent, which spec 110's staging check records. The admin routes sit
  under `requireAdminCredentials` (`internal/api/api.go`), apart from the
  public `Signup` handler that holds the sign-up guard
  (`internal/api/signup.go`), so creation works with sign-up off. This is
  read from the auth server's source, not run; spec 110's staging check
  confirms it.
- The user is created confirmed because the invitee signs in with spec
  114's emailed code (decision D6, 2026-10-11), and the auth server's
  magic link handler treats a request for an unconfirmed user as a new
  sign-up (`isNewUser = !user.IsConfirmed()`, then `a.Signup(...)`,
  `internal/api/magic_link.go`, read 2026-10-11), which sign-up off would
  refuse.
- Creating the user fires `harrier_new_owner()`, so the invitee owns track
  1 from that moment.
- Prints the user's id once to standard output, and nothing to any log.
  There is no link and no invitation page. The operator tells the invitee,
  personally, to open the hosted SPA and sign in with their address; the
  SPA's sign-in page (spec 114) then emails them a one-time code. Harrier
  sends nothing itself, which is the "Nothing auto-sends" invariant
  applied to the operator's own mail.
- An address that already has a user is refused by the auth server; the
  command prints `error: that address already has an account` and exits 1.
  It never prints the address.

The code reaches the invitee only through the email sender every sign-in
uses (spec 114, Honest limitations; configured by spec 110). Supabase's
built-in sender does not reach addresses outside the project's team
(Problem), so invitations work only once that sender is settled. If it
needs a custom SMTP provider, that is a vendor the amended Local-first
invariant does not list, and adding it is a spec of its own.

### Deletion: what is erased, in what order

```
harrier operator erase <user id> --confirm <user id>
```

The user id is the auth user's UUID. An email address is never accepted.
`--confirm` must repeat the id exactly, so a pasted id cannot be run by
accident. The command needs, from the process environment:

| Variable | Use |
|---|---|
| `HARRIER_DATABASE_URL` | a Postgres login that can `SET ROLE service_role` (the operator login, spec 110) |
| `HARRIER_SUPABASE_URL` | the project, as in spec 104 |
| `HARRIER_SUPABASE_SECRET_KEY` | the auth and Storage admin calls |

It refuses to run when `HARRIER_SUPABASE_SECRET_KEY` is defined in the
working directory's `.env`. `load_project_env` copies `.env` into the
environment (`harrier_cli/main.py:51-58`), and `docker-compose.yml` hands
`.env` to the local container (spec 110), so a secret key written
there would sit in the local container's environment. The text names the
variable, never its value.

The steps, each reported on its own line:

1. **Ban.** `PUT /auth/v1/admin/users/{user_id}` with a ban of 876000
   hours, the duration Supabase's reference uses as its example. A banned
   user signs in no more. An access token already issued stays valid until
   its `exp` (spec 104, Failure modes, and its open decision 10). A user
   that does not exist skips steps 1, 3 and 4, so a re-run after a partial
   erase finishes the rest.
2. **Storage.** Every object under the owner's prefix in the private
   bucket (ADR-013 decision 6) is deleted through spec 107's seam, which
   calls the Storage API. Supabase warns never to delete rows from
   `storage.objects` with SQL, because the files stay behind (Supabase,
   Storage troubleshooting).
3. **Rows.** One transaction, as `service_role`:
   `SET LOCAL ROLE service_role`, a read-back that `current_user` is
   `service_role` (refusing otherwise, as spec 104's request transaction
   does), `SET LOCAL lock_timeout` to `BUSY_TIMEOUT_MS`
   (`services/api/src/harrier/db.py:32`), then `SELECT * FROM
   harrier_erase_owner(<id>)`, then commit.
4. **Auth user.** `DELETE /auth/v1/admin/users/{user_id}`, a hard delete
   (`should_soft_delete` false). Supabase documents that a user who owns
   Storage objects cannot be deleted (Managing user data), which is why
   step 2 runs first. With no row left, the `owner_id` foreign keys no
   longer refuse.
5. **Stragglers.** The owner's prefix is listed again and anything found is
   deleted, for objects written between step 2 and step 4 with a token
   issued before the ban.
6. **Ledger.** One line appended to `data/hosted/erasures.jsonl`: the user
   id, the time in UTC, and which steps completed. No email, no row
   content. See "Backups" below for why it exists and why it is not in the
   database.

The output names each table and how many of its rows were deleted, the
number of objects deleted, and whether the auth user was deleted. Counts
are not personal data; nothing else about the owner is printed. Exit 0 only
when every step completed. Any failure exits 1 naming the step, and a re-run
with the same id resumes: every step is a no-op on what is already gone.

### How `harrier_refuse()` stands aside

After the erasure migration:

```sql
CREATE OR REPLACE FUNCTION harrier_refuse() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND TG_LEVEL = 'ROW'
       AND (pg_catalog.to_jsonb(OLD) ->> 'owner_id')
           = pg_catalog.current_setting('harrier.erase_owner', true)
       AND current_user = (
           SELECT pg_catalog.pg_get_userbyid(relowner)
           FROM pg_catalog.pg_class WHERE oid = TG_RELID
       )
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '%', TG_ARGV[0];
END
$$
```

It lets a row through only when all four hold:

- the operation is a row-level `DELETE`. Updates of events, a changed track
  id, and every `TRUNCATE` (a statement trigger, where `OLD` is null) are
  refused as today;
- the transaction-local setting `harrier.erase_owner` names the row's own
  owner, so an erase of owner A cannot remove a row of owner B, even inside
  the same transaction;
- the current role is the table's owner, which is the role a `SECURITY
  DEFINER` function owned by the migrating role runs as;
- implicitly, the session holds `DELETE` on the table, which no grantee
  does: `harrier_tenant` holds `SELECT, INSERT` on `job_events` and
  `SELECT, INSERT, UPDATE` on `tracks` (`schema.py:883-884`), and
  `service_role` holds nothing. The privilege check refuses before any
  trigger fires (spec 105, Amendment).

`to_jsonb(OLD) ->> 'owner_id'` is null for a table without the column, and
null never equals the setting, so the function refuses on any table it may
later guard that has no owner. An unset setting reads as null; a reset one
reads as the empty string. Neither matches a UUID.

Every other session is refused exactly as before: `harrier_tenant`,
`service_role`, `authenticated` and `anon` by the missing privilege and, if
a grant were ever added by mistake, by the role check; the migrating role by
the setting, unless it sets it. That last case is not a new power: the role
that owns the tables can already disable or drop the triggers (Honest
limitations).

### `harrier_erase_owner(target uuid)`

```sql
CREATE FUNCTION harrier_erase_owner(target uuid)
RETURNS TABLE (table_name text, erased bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$ ... $$
```

In order:

1. A null `target` raises `harrier_erase_owner needs an owner`.
2. If the role it runs as is neither superuser nor `BYPASSRLS`, it raises
   `harrier_erase_owner runs as a role that does not bypass row security;
   it would erase nothing`. Every owned table forces row security
   (`schema.py:850-858`) with a policy only for `harrier_tenant`, so a
   definer subject to it would see no rows, delete none, and report
   success.
3. `pg_catalog.set_config('harrier.erase_owner', target::text, true)`.
4. Deletes `WHERE owner_id = target` from each owned table in
   `OWNED_TABLES` order (`schema.py:606-614`): `job_events`, `jobs`,
   `contacts`, `profile_documents`, `user_config`, `job_runs`, `tracks`.
   That order deletes a referencing row before the row it references
   (`job_events` to `jobs`, `jobs` and `profile_documents` to `tracks`).
   One result row per table, with the count `GET DIAGNOSTICS ... ROW_COUNT`
   reports.
5. `pg_catalog.set_config('harrier.erase_owner', '', true)`, so a statement
   later in the caller's transaction is refused again.

The table list is written literally into the erasure migration, not
generated at run time, because a migration's text is history. A later
owned table (spec 116's `credentials`, or spec 108's `schedule_state`)
replaces the function in its own migration; the test under Acceptance
criteria that fills every table in `OWNED_TABLES` and erases fails until
it does.

Grants: `REVOKE ALL ON FUNCTION harrier_erase_owner(uuid) FROM PUBLIC,
anon, authenticated, service_role, harrier_tenant`, then `GRANT EXECUTE ...
TO service_role`, in the same migration transaction, as PostgreSQL advises
for definer functions. This is the one privilege `service_role` gains in
harrier's schema, and it is a door, not a table: it can erase an owner and
do nothing else.

A rollback anywhere in the caller's transaction undoes the deletes and the
setting together, since `set_config(..., true)` lasts only until the
transaction ends (PostgreSQL, `set_config`).

### No table lock, no trigger off

Each `DELETE` takes `ROW EXCLUSIVE` on its table, the same lock any
tenant's write takes, and these do not conflict with each other. Row locks
fall only on the erased owner's rows. Another owner's insert, update or
read during an erase neither waits nor fails. No statement in the erasure migration
or in the erase disables a trigger.

### Concurrent writes by the owner being erased

A token issued before the ban keeps working until it expires (one hour by
default, spec 104). In that window:

- A write that commits before the erase's transaction reaches its table is
  deleted with the rest.
- A write that references a row the erase has already locked waits, then
  fails its foreign key when the erase commits.
- An event inserted against a job after the erase deleted that owner's
  events, but before it deleted the job, makes the job's delete fail its
  foreign key. The erase transaction rolls back whole, the command exits 1
  at step 3, and a re-run erases both.
- A row written after step 3 commits (a contact, say, which references no
  track) makes step 4 fail on the `owner_id` foreign key. The command exits
  1 at step 4 and prints `rows were written after the erase; run it again
  after the user's last token expires`.

Nothing is ever left half-deleted inside one table set: step 3 is one
transaction.

### What the operator can and cannot see

Can:

- Every row and object. The `postgres` role has `BYPASSRLS` (spec 105,
  Honest limitations), and the Supabase dashboard's SQL editor and Storage
  browser run with it. Row-level policy protects owners from each other,
  not from the operator.
- Every auth user's email address, in the dashboard and in `auth.users`.
- Fly's logs, under the redaction spec 110 states.

Cannot, through anything harrier builds:

- No HTTP route acts as `service_role` (ADR-013 decision 3). The API holds
  no secret key and refuses to start with one in its environment (spec
  104, The login role).
- `harrier operator` prints ids, counts and step results, never row content
  or an address.
- `service_role`'s only privilege in harrier's schema is to erase. It
  cannot read a row.

Specs 106 and 116 decide whether the operator can decrypt stored
credentials; this spec claims nothing about them.

### Backups

Erasure removes the live copy. Supabase's daily database backups keep the
erased rows until they age out: seven days on Pro, fourteen on Team, up to
thirty on Enterprise, and point-in-time recovery up to its chosen window
(Supabase, Database backups). Database backups do not include Storage
objects, so a deleted object is not restorable from them.

The UK regulator's guidance on the same erasure text accepts that backup
copies remain "until it is overwritten" if the data is put "beyond use"
(ICO, Right to erasure). Here that means: a restore of a backup taken
before an erasure re-runs `harrier operator erase` for every id in the
ledger erased after the backup's time, before the API serves. That is why
the ledger exists and why it is outside the database: a restore would take
a database ledger back to the backup's time, with the very entries it needs
missing. `docs/architecture.md` states this procedure. The ledger holds
UUIDs, which identify a person to anyone who can join them to
`auth.users`; once the user is deleted, nothing harrier holds can.

### Export (moved to spec 117)

The stub this spec creates says, as its headline: an owner downloads
everything they own from the hosted API, running as `harrier_tenant` under
the row policy, with no service-role privilege. The file is a zip holding a
`manifest.json` (format version, export time, store version, the list of
tables and objects), one JSON Lines file per table in `OWNED_TABLES` with
every column but `owner_id`, and every Storage object under the owner's
prefix at its own path. The format is documented in `docs/` and versioned,
which is what Article 20 asks for. Stored credentials are listed by kind,
never exported by value. The SPA's control for it is spec 114's or later.
`harrier export` stays local (spec 112, Out of scope).

## Failure modes

- **Sign-up left open in a project.** Anyone can create a user, and
  `harrier_new_owner()` gives them track 1. This spec cannot detect it from
  inside harrier; spec 110's deploy check refuses to deploy a project with
  sign-up or anonymous sign-ins on (Scope item 8).
- **`invite` without the secret key or URL.** Refused before any call,
  naming the missing variable, exit 1.
- **`invite` with an address that has a user.** Exit 1, `error: that
  address already has an account`. No user id is printed.
- **An invitee whose code never arrives.** The user exists and cannot sign
  in until the email sender reaches them (Behavior). Nothing in this spec
  detects it; the invitee tells the operator.
- **`invite` with an empty or malformed address.** Refused before any
  call; the address is not echoed.
- **An invitation never accepted.** The user exists with track 1 and no
  password. `harrier operator erase` removes it like any other.
- **`erase` with a URL whose login cannot `SET ROLE service_role`.** Exit 1,
  `error: harrier operator needs a login that can act as service_role`,
  nothing deleted (Storage is step 2, so the role check runs before it,
  as a preflight).
- **`erase` with a definer that does not bypass row security.** The
  function raises step 2's message; the transaction rolls back; exit 1.
  Without this check the erase would report zero rows everywhere and step
  4 would then fail on the foreign key, a worse place to find out.
- **`erase` with a mistyped or mismatched `--confirm`.** Exit 1, nothing
  called.
- **`erase` of an id with no auth user.** Steps 1, 3 and 4 are skipped,
  Storage under the prefix is still cleared, the ledger records it. Rows
  cannot exist for a missing user, because the foreign key forbids it.
- **A tenant session sets `harrier.erase_owner` itself.** Its delete on
  `job_events` or `tracks` is refused by the missing privilege. With a
  privilege granted by mistake, the trigger still refuses, because
  `current_user` is `harrier_tenant`, not the table's owner.
- **Erasing owner A with the setting naming owner B.** Not reachable
  through the function. In a hand-written session as the table owner, A's
  rows are refused, because the setting names B.
- **`TRUNCATE job_events` or `TRUNCATE tracks CASCADE` during an erase.**
  Refused as today; the statement trigger never stands aside.
- **Updating an event or a track id during an erase.** Refused as today.
- **A concurrent write by the erased owner.** See Behavior; at worst exit 1
  at step 3 or 4, and a re-run completes.
- **Lock wait.** The erase waits at most `BUSY_TIMEOUT_MS` for a row lock,
  then fails with exit 1 at step 3; a re-run resumes.
- **Storage call fails.** Exit 1 at step 2 or 5. Rows are untouched at step
  2, so a re-run starts over cleanly.
- **Auth delete refused because of Storage objects.** Step 5 has not run
  yet when step 4 fails; the command reports step 4, and a re-run's step 2
  clears the objects first.
- **The Supabase dashboard's delete.** Still fails on the foreign key for
  any user with rows, which is every user. `docs/architecture.md` says the
  dashboard delete is not the path and names the command.
- **A later owned table not added to the function.** Its rows survive the
  erase and step 4 fails on that table's foreign key. The planned test that
  fills every table in `OWNED_TABLES` fails first, in CI.
- **A restore from a backup older than an erasure.** The erased person's
  rows come back. The documented restore procedure re-runs the ledger's
  erasures before the API serves. Nothing enforces that the operator
  follows it (Honest limitations).
- **The secret key in `.env`.** `erase` and `invite` refuse, naming the
  variable.

## Acceptance criteria

Migration and database:

- [ ] `POSTGRES_MIGRATIONS` has the erasure migration with the
      statements under Behavior; `MIGRATIONS` has the same version with
      no statements; `SINGLE_DIALECT_MIGRATIONS` declares it Postgres only
      with its reason;
      `services/api/tests/test_postgres_store.py::test_every_new_migration_declares_both_dialects`
      passes.
- [ ] `harrier store migrate` on a shimmed store at the version before
      the erasure migration prints `postgres <previous> -> <new>`; a fresh
      store reaches `target_version()`; a SQLite store records the new
      version and changes nothing
      (planned test_the_erasure_migration_is_postgres_only).
- [ ] No statement in the erasure migration contains `DISABLE TRIGGER`,
      `DROP TRIGGER` or `ALTER TABLE`, and every trigger present before it
      is present and enabled after it
      (planned test_the_erasure_migration_leaves_every_trigger_in_place).
- [ ] As the test superuser, without the setting, deleting a `job_events`
      row and a `tracks` row is still refused with the existing messages,
      and `TRUNCATE job_events` and `TRUNCATE tracks CASCADE` still are
      (`services/api/tests/test_owner_policy.py::test_truncate_cannot_empty_events_or_tracks`
      unchanged, plus planned test_refusals_hold_without_the_erase_setting).
- [ ] With `harrier.erase_owner` set to owner A, the table owner's delete
      of owner B's event and track is refused, an update of A's event is
      refused, and `TRUNCATE` is refused
      (planned test_the_erase_setting_lets_through_only_its_owners_deletes).
- [ ] As `harrier_tenant` with the setting set to its own id, a delete on
      `job_events` and on `tracks` is refused; with `DELETE` granted to it
      inside the test, still refused by the trigger
      (planned test_the_erase_setting_alone_lets_no_tenant_delete).
- [ ] With two owners whose rows fill every table in `OWNED_TABLES`,
      `harrier_erase_owner(A)` as `service_role` leaves no row of A in any
      of them, leaves every row of B unchanged, returns one count per
      table, and leaves `harrier.erase_owner` empty afterwards
      (planned test_erase_removes_one_owner_from_every_owned_table).
- [ ] After that erase, deleting A from the shim's `auth.users` succeeds;
      before it, the same delete is refused by the foreign key
      (planned test_an_erased_owner_can_be_deleted).
- [ ] While one transaction holds an uncommitted erase of A, owner B
      inserts a job, an event and a track with `lock_timeout` of 100 ms,
      and each succeeds (planned test_erasing_one_owner_never_waits_for_another).
- [ ] `harrier_erase_owner` executed as `harrier_tenant`, `authenticated`
      and `anon` is refused with `permission denied`
      (planned test_only_service_role_can_erase).
- [ ] A definer without `BYPASSRLS` raises the "would erase nothing"
      message and deletes nothing
      (planned test_erase_refuses_a_definer_bound_by_row_security).
- [ ] An erase that fails part way (an event inserted for one of A's jobs
      between its two deletes, simulated with a trigger in the test)
      leaves every one of A's rows in place
      (planned test_a_failed_erase_deletes_nothing).
- [ ] The catalog test passes, and fails when `harrier_erase_owner` is
      granted to `harrier_tenant`, and when a second definer function
      appears
      (`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`
      and `services/api/tests/test_owner_policy.py::test_the_catalog_names_each_breach`,
      both extended).

Operator commands (admin and Storage calls through an injected `httpx`
transport; no test reaches the network):

- [ ] `harrier operator invite` reads the address from standard input,
      posts the admin user creation with `email_confirm` true and no
      password, prints the user id and no link, and the address appears
      in no argument, log record or error text (decision D6; planned
      test_invite_creates_a_confirmed_user_and_never_prints_the_address).
- [ ] No `generate_link` call and no `redirect_to` appears in
      `harrier/operator.py`, and no hosted SPA route is an invitation page
      (decision D6).
- [ ] An address with an account exits 1 with the fixed text and prints no
      user id (planned test_invite_refuses_an_existing_address).
- [ ] `harrier operator erase` runs ban, Storage, rows, auth delete and
      stragglers in that order, appends one ledger line with no email, and
      exits 0 (planned test_erase_runs_its_steps_in_order).
- [ ] A failure at each step exits 1 naming it, and a re-run with the same
      id completes the remaining steps without repeating a failed delete
      (planned test_erase_resumes_after_a_failed_step).
- [ ] A row written after step 3 makes step 4 exit 1 with the "run it again"
      text (planned test_erase_reports_rows_written_after_it).
- [ ] Missing variables, a mismatched `--confirm`, a login that cannot act
      as `service_role`, and the secret key defined in `.env` each exit 1
      before any call, and no output contains the key, the URL's password
      or an address (planned test_operator_refusals_name_no_secret).
- [ ] `harrier operator` runs on a Postgres URL; `erase` declares
      `service_role` and `invite` declares that it opens no database
      connection; every command outside the classes of spec 112's
      Amendment (2026-10-11) still refuses (decision D2; spec 112's
      planned test_the_postgres_gate_has_operator_and_run_classes).

Documents and other specs:

- [ ] Once approved, the specs named under "Changes to approved specs"
      carry those lines, and nothing else in them changes.
- [ ] ADR-013's Consequences carry the "Amended (spec 111)" note.
- [ ] `docs/architecture.md`'s hosted section states the sign-up settings,
      the two commands, the erasure order, the ledger and the restore
      procedure, naming the tests above.

Gates:

- [ ] `uv run ruff check` and `uv run pyright` pass on the service.
- [ ] CI's `check-python` job runs the new Postgres tests, none skipped.
- [ ] `just check` passes.

## Honest limitations

- **The operator sees everything.** Row-level policy separates owners from
  each other. It does not separate them from whoever holds the `postgres`
  role or the dashboard. This spec narrows what harrier's own commands
  print; it cannot narrow what the platform shows its project owner.
- **The table owner can still bypass the refusals.** The role that owns the
  tables can set `harrier.erase_owner` by hand, or disable or drop the
  triggers, as migration 10 did (`schema.py:746-748`). The refusals guard
  against application code and every other role, not against the schema
  owner.
- **Erasure is not instant everywhere.** Backups keep erased rows until
  they age out, and the restore procedure depends on the operator following
  it. Fly's logs are erased only by their own retention; spec 110's
  redaction is what keeps identity out of them.
- **Data outside harrier is not reached.** Content a run sent to the
  owner's own AI provider, Apify, Hunter, Telegram or Gmail accounts sits
  under those accounts. The owner holds them; harrier cannot erase there.
- **A token outlives the ban.** Up to its `exp`, one hour by default
  (spec 104). The erase handles the race by failing and being re-run, not
  by preventing it.
- **No self-service deletion.** An owner asks the operator, out of band.
  The operator's answer time is a human's, measured against Article
  12(3)'s month by nothing in harrier.
- **This is not legal advice.** The GDPR references say what the text asks
  for. Whether harrier's operator is a controller, which lawful basis
  applies, a privacy notice, and processor agreements with Supabase, Fly
  and Vercel are outside any spec.
- **The shim is not Supabase.** The database tests run on plain Postgres
  with spec 105's shim, where `auth.users` has one column and no auth
  server cascades. The admin calls are tested against an injected
  transport. Every vendor behavior under Proof / origin marked as an
  assumption is unverified until spec 110's staging check.
- **The ledger is one file on one machine.** If the operator loses it, the
  restore procedure has no list. It is never-in-git by classification, so
  git is not its backup either.
- **Invitations wait on the sign-in sender.** An invitee signs in with spec
  114's emailed code (decision D6), so an invitation works only where that
  sender reaches the invitee.
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).

## Migration

None for the local product. SQLite records the new version and changes
nothing.
`harrier operator` refuses without a Postgres URL, with spec 112's text for
a local-only store: there is one owner locally, and deleting the data
directory deletes everything.

A hosted store reaches the new version through `harrier store migrate`. Existing
users keep their rows; nothing is erased by the migration. Before any
invitation is sent, spec 110 turns sign-up and anonymous sign-ins off.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 104:** its Out of scope gives "Sign-up, invitations, export and
  deletion" to spec 111. Export now reads spec 117.
- **Spec 105:** its Grants say `service_role` gets nothing; it now holds
  `EXECUTE` on `harrier_erase_owner(uuid)` and nothing else. Its catalog,
  which fails on `EXECUTE` for `service_role` on any function in `public`
  (Post-merge review, item 5), allows that one grant, declares
  `harrier_erase_owner` a definer function, and fails when any harrier
  function is executable by `harrier_tenant` (Scope item 4).
  `harrier_refuse()` lets one owner's erasure through (Behavior, "How
  `harrier_refuse()` stands aside"). Its Out of scope gives "Sign-up,
  export and deletion" to spec 111; export now reads spec 117.
- **Spec 107:** the seam can list and delete every object under one
  owner's prefix for an operator command holding a secret key, outside
  any request (Behavior, "Deletion", steps 2 and 5). Spec 107 as refined
  sends no secret key and binds a store only to a request's token, so this
  is a new way to bind it.
- **Spec 112:** the CLI's Postgres gate takes a class for `harrier
  operator` (Scope item 5), and its planned
  test_every_command_has_a_postgres_refusal_class covers it. Its Out of
  scope ("A tenant export: spec 111") now reads spec 117. Decision D2
  (2026-10-11) reworded the class: "operator: each subcommand runs as its
  declared role", one group with spec 108's subcommands. Written into
  spec 112 as its Amendment (2026-10-11); the Out of scope line is not.
- **Spec 110:** the project's Auth settings under Behavior ("Sign-up is
  by invitation only") are provisioned and checked at deploy, and a
  project with sign-up or anonymous sign-ins on is refused; the operator
  login that can `SET ROLE service_role` is created per environment; and
  the assumptions under Proof / origin are staging checks. Written into
  spec 110's Amendment (2026-10-11) (decision D4).

## Options weighed

- **Disable the triggers for the erase.** What migration 10 did. It takes
  a `SHARE ROW EXCLUSIVE` lock that makes every owner's writes to the table
  wait, and while off the trigger is off for every session. Not chosen.
- **`session_replication_role = replica`.** Skips ordinary triggers for one
  session without a table lock. It skips every trigger, including those
  the erase should still meet, and setting it needs superuser rights, which
  the operator login does not have. Not chosen.
- **A flag setting that is simply "on", as the stub proposed.** Any role
  that can delete would then remove any owner's events while it is on. A
  setting naming one owner, checked per row against `owner_id`, plus a role
  check, says the same thing more narrowly. Chosen: the narrower form.
- **`ON DELETE CASCADE` on `owner_id`,** as Supabase's guide suggests.
  Deleting the auth user would then delete the rows. The cascade's deletes
  fire the row triggers, so `job_events` and `tracks` would still refuse,
  and the cascade would run as the auth server's role, which Supabase's
  troubleshooting guide says has limited privileges outside `auth`. It
  would also make the dashboard's one click an irreversible erase with no
  Storage step and no ledger. Not chosen; spec 105's refusal stays.
- **Delete `auth.users` inside the function.** One transaction for rows and
  user, with no window between them. It skips the auth server's own
  delete, and whether the migrating role may delete from `auth.users` on
  Supabase is unverified. Not chosen; open decision 5.
- **Self-service deletion.** A definer function executable by
  `harrier_tenant` that erases `auth.uid()`'s own rows, with the auth user
  removed later by an operator sweep. It gives owners a button. It puts a
  bulk delete one request away from any stolen token, still needs the
  operator for the auth user, and is reachable from HTTP, which ADR-013
  decision 3 keeps the service-role path away from. Not chosen for now;
  open decision 2.
- **`generate_link` with type `invite`, delivered by hand.** This text's
  first recommendation. The link would land on an invitation page in the
  SPA, and the link is a credential until used. Decision D6 (2026-10-11)
  has the invitee sign in with spec 114's code instead, with no
  invitation page, so the link has nowhere to land. Not chosen.
- **`inviteUserByEmail` with custom SMTP.** Supabase sends the invitation.
  It needs an SMTP vendor the Local-first invariant does not list, so an
  invariant amendment by spec, and makes harrier send mail on the
  operator's behalf. Not chosen; open decision 4.
- **A harrier-owned operator role,** like `harrier_tenant` for requests.
  `service_role` is reachable through the Data API with the secret key, but
  only the operator holds that key, and ADR-013 decisions 3 and 4 name the
  service role for operator commands. Not chosen; open decision 8.
- **Soft delete.** Supabase can keep a hashed user id; its reference says a
  soft delete "is not reversible". It keeps a link to a person harrier no
  longer has a reason to hold. Not chosen; open decision 6.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").
The text above says where each changed it.

- **D2, one operator group.** `harrier operator` is one command group, and
  each subcommand declares the database role it runs as: `erase` runs as
  `service_role`, and `invite` opens no database connection. Spec 108's
  `scheduler` and `schedule-report` are subcommands of the same group.
  Spec 112's gate gains exactly two classes beyond `harrier store`:
  operator, and spec 106's run class (Scope item 5, Changes to approved
  specs).
- **D4, spec 110 takes this spec's asks.** The spec 110 line under
  "Changes to approved specs", with the assumptions under Proof / origin
  as this text now states them, is written into spec 110's Amendment
  (2026-10-11).
- **D6, invitations.** An invited user signs in with the same emailed
  one-time code as spec 114; there is no separate invitation page. The
  invite creates a confirmed user and prints no link (Behavior, "Sign-up
  is by invitation only"). This takes Open decision 4 in a form its
  recommendation did not have.
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

## Open decisions for Akin

1. **Split export into spec 117.** Recommended: this spec keeps sign-up and
   deletion; export, which needs the API as the owner, Storage reads and a
   contract change, becomes spec 117. Invite-only then holds until both
   ship. Alternative: all three here, one larger change with three review
   lenses.
2. **Who starts an erase.** Recommended: the operator, on the owner's
   request, through `harrier operator erase`. Alternative: self-service
   row erasure through a definer function `harrier_tenant` may execute,
   with an operator sweep for auth users.
3. **How the refusals stand aside.** Recommended: a transaction-local
   setting naming one owner, honored only for row deletes of that owner's
   rows by the table's owner role. Alternative: the stub's plain "on" flag,
   simpler and broader.
4. **How an invitation travels.** Recommended: `generate_link`, delivered
   by the operator by hand; no SMTP vendor. Alternative: Supabase sends it
   through a custom SMTP provider, which amends the Local-first invariant
   by spec.
   Taken 2026-10-11 (D6): no link and no invitation page. The operator
   creates the user, confirmed, and the invitee signs in with the same
   emailed one-time code as spec 114 (Behavior, "Sign-up is by invitation
   only").
5. **Where the auth user is deleted.** Recommended: the auth admin API
   after the rows are gone, with a re-run for the race. Alternative:
   `DELETE FROM auth.users` inside `harrier_erase_owner`, atomic but
   unverified on Supabase and past the auth server's own cleanup.
6. **Hard or soft delete of the auth user.** Recommended: hard.
   Alternative: soft, keeping a hashed id.
7. **The erasure ledger.** Recommended: a local JSON Lines file under
   `data/hosted/`, never-in-git. Alternatives: none, leaving the restore
   procedure to memory; or a database table, which a restore rolls back.
8. **The role that may erase.** Recommended: `service_role`, as ADR-013
   names it. Alternative: a harrier-owned `harrier_operator` role with no
   login, which Supabase's Data API can never switch to.

## Proof / origin

Repository:

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`: decisions 1 (as
  amended by spec 105), 3, 4 and 6, Consequences.
- `specs/102-hosted-multi-tenant-deployment.md`: open decisions 2 and 5,
  and the amended Local-first bullet under Behavior.
- `specs/105-every-personal-table-is-owned-and-policed.md`: the foreign key
  with no `ON DELETE`, the sign-up trigger, the grants, the catalog test,
  and its Amendment (`harrier_tenant`, TRUNCATE triggers, missing grants
  refuse first).
- `specs/104-the-hosted-api-authenticates-every-request.md`: the token
  window after sign-out (open decision 10) and the refusal of a secret key
  in the API's environment.
- `specs/112-the-tracker-domain-runs-on-either-store.md`: the CLI gate's
  refusal classes and Out of scope ("A tenant export: spec 111").
- `specs/079-decisions-are-recorded-as-events.md:149-150`: append-only is a
  database rule.
- `services/api/src/harrier/tracker/schema.py`: `harrier_refuse()` at
  `:482-486`; the refusal triggers at `:503-504`, `:585-586`, `:589-590`,
  `:817-818`, `:821-822`; `OWNED_TABLES` at `:606-614`; the disabled
  trigger at `:746-748`; the `owner_id` foreign key at `:749-755`;
  `harrier_new_owner()` at `:829-841`; the grants at `:870-890`;
  `POSTGRES_MIGRATIONS` at `:919-923`, whose latest version
  `target_version()` returns (`services/api/src/harrier/pgstore.py:84-88`).
- `services/api/tests/test_owner_policy.py`: `catalog_problems`, its
  definer allowlist and `NO_PRIVILEGE_ROLES`.
- `services/api/src/harrier_cli/main.py`: `load_project_env` at `:51-58`,
  the Postgres gate at `:2913-2916`.
- The post-merge reviews of PRs #194 and #207 (architecture 5) and of PR
  #216, 2026-10-10, as routed into the stub.

Vendors and law, each read 2026-10-10:

- Supabase, General configuration: with sign-up disabled "only existing
  users can sign in"; anonymous sign-ins setting.
  https://supabase.com/docs/guides/auth/general-configuration
- Supabase, Send emails with custom SMTP: the default service delivers only
  to the project team's addresses, two messages an hour, not for
  production. https://supabase.com/docs/guides/auth/auth-smtp
- Supabase, Managing user data: `auth.admin.deleteUser()`; "You cannot
  delete a user if they are the owner of any objects in Supabase Storage";
  the suggestion of `on delete cascade`; a failing trigger blocks sign-ups.
  https://supabase.com/docs/guides/auth/managing-user-data
- Supabase, deleteUser reference: needs the service role key, server only,
  soft delete "is not reversible".
  https://supabase.com/docs/reference/javascript/auth-admin-deleteuser
- Supabase, updateUserById reference: banning by `ban_duration`, example
  `876000h`. https://supabase.com/docs/reference/javascript/auth-admin-updateuserbyid
- Supabase, generateLink reference: link types including `invite`, for
  delivery by your own means.
  https://supabase.com/docs/reference/javascript/auth-admin-generatelink
- Supabase, Errors when creating, updating or deleting users: a foreign key
  from a public table, or a trigger, blocks a dashboard delete; the auth
  admin role's limited privileges.
  https://supabase.com/docs/guides/troubleshooting/dashboard-errors-when-managing-users-N1ls4A
- Supabase, Storage troubleshooting: never delete from `storage.objects`
  with SQL; use the Storage API.
  https://supabase.com/docs/guides/troubleshooting/storage-unexpectedly-high-usage-or-exceed_storage_size_quota-errors-ae21a5
- Supabase, Database backups: retention by plan, point-in-time recovery
  windows, Storage objects not included.
  https://supabase.com/docs/guides/platform/backups
- Supabase, createUser reference: server only; the example titled
  "Auto-confirm the user's email" sets `email_confirm: true` with no
  password; the page does not say whether an email is sent. Read
  2026-10-11.
  https://supabase.com/docs/reference/javascript/auth-admin-createuser
- Supabase auth server, `internal/api/magic_link.go`: an unconfirmed user
  is treated as a new user and handed to `Signup`. Read 2026-10-11.
  https://github.com/supabase/auth/blob/master/internal/api/magic_link.go
- Supabase auth server source: admin routes under `requireAdminCredentials`
  (`internal/api/api.go`); `adminGenerateLink` creates an invited user and
  sends no email (`internal/api/mail.go`); `DisableSignup` is checked in the
  `Signup` handler only (`internal/api/signup.go`); `adminUserDelete` and
  `should_soft_delete` (`internal/api/admin.go`).
  https://github.com/supabase/auth/tree/master/internal/api
- PostgreSQL, ALTER TABLE: `DISABLE TRIGGER` takes `SHARE ROW EXCLUSIVE`.
  https://www.postgresql.org/docs/current/sql-altertable.html
- PostgreSQL, CREATE FUNCTION: `OR REPLACE` keeps ownership and
  permissions; definer functions set a safe `search_path` and revoke
  `PUBLIC` in the creating transaction.
  https://www.postgresql.org/docs/current/sql-createfunction.html
- PostgreSQL, trigger functions in PL/pgSQL: `TG_OP`, `TG_LEVEL`,
  `TG_RELID`, `OLD` null in statement triggers, `RETURN OLD` lets a delete
  proceed. https://www.postgresql.org/docs/current/plpgsql-trigger.html
- GDPR Article 17 (erasure, "without undue delay", the exceptions in
  17(3)): https://gdpr-info.eu/art-17-gdpr/
- GDPR Article 12(3) (one month, extendable by two):
  https://gdpr-info.eu/art-12-gdpr/
- GDPR Article 20(1) ("structured, commonly used and machine-readable"):
  https://gdpr-info.eu/art-20-gdpr/
- ICO, Right to erasure: backup data put "beyond use" until overwritten.
  UK guidance on the same text, cited as guidance, not as EU authority.
  https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/individual-rights/right-to-erasure/

Assumptions for spec 110's staging check, each unconfirmed:

- The admin user creation creates a confirmed user while sign-up is off,
  and sends that user no email (read from source and the reference, not
  run). Added by decision D6, 2026-10-11, in place of the same assumption
  about `generate_link`.
- A user created that way can request and verify spec 114's emailed code
  while sign-up is off (decision D6).
- The operator login can `SET ROLE service_role`, and a definer function
  owned by the migrating role runs with `BYPASSRLS` (spec 105 already lists
  the second).
- A banned user cannot refresh a session; an access token already issued
  stays valid to `exp`.
- The admin and Storage endpoints accept a new-style secret key in the
  form the client sends (header placement is not stated in the pages read).
- The auth admin delete succeeds once no public row references the user
  and no Storage object is owned by them, and removes the user's
  identities and sessions with it.
- The admin user creation refuses an address that already has a user,
  rather than returning it (not established from the source read). It
  replaces, with decision D6, the two assumptions this list held about
  `generate_link`'s links.

## Out of scope

- Export: spec 117 (Open decision 1).
- Open sign-up, waitlists and self-service account creation. Spec 102's
  invite-only holds until 111 and 117 ship, and opening it is its own
  spec.
- The SPA's sign-in page: spec 114. There is no invitation page and no
  password (decision D6, 2026-10-11).
- Changing Supabase settings from code, and the deploy check of them:
  spec 110.
- The Storage seam itself: spec 107. This spec only states what it needs
  from it.
- Self-service deletion (Open decision 2).
- Billing, plans, a privacy notice, terms, and processor agreements.
- Erasing data held in an owner's own third-party accounts.
- Local deletion: the local product has one owner, who deletes the data
  directory.
