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

Line numbers below were refreshed on 2026-10-15, by the change that builds
this spec (decision D9), to the files as that change leaves them. The stub
cited older ones (`schema.py:441-445`, `:463`, `:545`, `:549`, `:776`,
`:780`), and each refresh since moved them again; the same statements are
found with `grep -n` for `harrier_refuse`, `CREATE TRIGGER` and `TRUNCATE`
in `services/api/src/harrier/tracker/schema.py`.

**Deletion collides with the store's own refusals.** The post-merge review
of PRs #194 and #207 (architecture 5, 2026-10-10) found it. On Postgres,
`job_events` is append-only and `tracks` are never deleted, enforced by one
function, `harrier_refuse()` (`schema.py:682-686`), which raises its trigger
argument unconditionally. It sits behind row triggers
`tracks_are_never_deleted` (`:703-704`), `job_events_append_only_update`
(`:785-786`) and `job_events_append_only_delete` (`:789-790`) and, since
spec 105, statement triggers on TRUNCATE (`:1142-1143`, `:1146-1147`), which
fire even for a superuser
(`services/api/tests/test_owner_policy.py::test_truncate_cannot_empty_events_or_tracks`).
An owner's events and tracks cannot be deleted at all. Spec 079 made
append-only a database rule, not a convention
(`specs/079-decisions-are-recorded-as-events.md:149-150`), and nothing
written since says when, if ever, it yields.

The one way through today is the one migration 10 used for the seeded
track: `ALTER TABLE tracks DISABLE TRIGGER` (`schema.py:1071-1073`). That
statement takes a `SHARE ROW EXCLUSIVE` lock (PostgreSQL, ALTER TABLE),
which conflicts with the `ROW EXCLUSIVE` lock every insert, update and
delete takes. In a migration that is fine. To delete one owner while others
use the store, it would make every other owner's writes to that table wait,
and while the trigger is off it is off for every session.

**No auth user can be deleted.** Every owned table carries `owner_id uuid
NOT NULL DEFAULT auth.uid() REFERENCES auth.users (id)` with no `ON DELETE`
action (`schema.py:1076-1080`, and `:1454` for spec 122's `job_eligibility`;
spec 105). Deleting a user who owns rows is
refused. The post-merge review of PR #216 (2026-10-10) added that the
sign-up trigger, `harrier_new_owner()` (`schema.py:1154-1166`), gives every
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

1. **A new Postgres-only migration (version 22, the next free one when
   it was built), called "the erasure migration" below.**
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
   (`harrier_cli/main.py:3838-3842` today, restructured by spec 112) gains
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
    spec 117's, deletion is this spec's, and `job_events`, `job_eligibility`,
    `job_verdicts` and `tracks` yield to one owner's erasure and to nothing
    else.
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
environment (`harrier_cli/main.py:79-99`), and `docker-compose.yml` hands
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
   Storage troubleshooting). Until spec 107's seam exists this step lists
   the prefix and refuses when anything is there: Amendment (2026-10-15):
   the erase before spec 107 is built.
3. **Rows.** One transaction, as `service_role`:
   `SET LOCAL ROLE service_role`, a read-back that `current_user` is
   `service_role` (refusing otherwise, as spec 104's request transaction
   does), `SET LOCAL lock_timeout` to `BUSY_TIMEOUT_MS`
   (`services/api/src/harrier/db.py:33`), then `SELECT * FROM
   harrier_erase_owner(<id>)`, then commit.
4. **Auth user.** `DELETE /auth/v1/admin/users/{user_id}`, a hard delete
   (`should_soft_delete` false). Supabase documents that a user who owns
   Storage objects cannot be deleted (Managing user data), which is why
   step 2 runs first. With no row left, the `owner_id` foreign keys no
   longer refuse.
5. **Stragglers.** The owner's prefix is listed again and anything found is
   deleted, for objects written between step 2 and step 4 with a token
   issued before the ban. Until spec 107's seam exists, anything found
   refuses the step (Amendment (2026-10-15), as for step 2).
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
  `SELECT, INSERT, UPDATE` on `tracks` (`schema.py:1211-1212`), and
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
   (`schema.py:1175-1184`, `:1472-1475`) with a policy only for `harrier_tenant`, so a
   definer subject to it would see no rows, delete none, and report
   success.
3. `pg_catalog.set_config('harrier.erase_owner', target::text, true)`.
4. Deletes `WHERE owner_id = target` from each owned table in
   `OWNED_TABLES` order: `job_eligibility`, `job_verdicts`, `job_events`,
   `jobs`, `contacts`, `profile_documents`, `user_config`, `job_runs`,
   `tracks`, `schedule_state`, `credentials`, `run_records`. (The stub's
   text named migration 10's seven. Spec 122 put `job_eligibility` first,
   and specs 139, 108, 116 and 106 added the other four before the
   erasure migration was written: Amendment (2026-10-15): what building it
   settled, item 1.) That order deletes a referencing row before the row it
   references (`job_eligibility`, `job_verdicts` and `job_events` to `jobs`;
   `jobs`, `job_verdicts` and `profile_documents` to `tracks`). One result
   row per table, with the count `GET DIAGNOSTICS ... ROW_COUNT` reports.
5. `pg_catalog.set_config('harrier.erase_owner', '', true)`, so a statement
   later in the caller's transaction is refused again.

The table list is written literally into the erasure migration, not
generated at run time, because a migration's text is history. A later
owned table after migration 22 replaces the function in its own
migration; the test under Acceptance
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
  erase and step 4 fails on that table's foreign key. The test that fills
  every table in `OWNED_TABLES` fails first, in CI
  (`services/api/tests/test_owner_erasure.py::test_erase_removes_one_owner_from_every_owned_table`).
- **A restore from a backup older than an erasure.** The erased person's
  rows come back. The documented restore procedure re-runs the ledger's
  erasures before the API serves. Nothing enforces that the operator
  follows it (Honest limitations).
- **The secret key in `.env`.** `erase` and `invite` refuse, naming the
  variable.

## Acceptance criteria

Migration and database:

- [x] `POSTGRES_MIGRATIONS` has the erasure migration with the
      statements under Behavior; `MIGRATIONS` has the same version with
      no statements; `SINGLE_DIALECT_MIGRATIONS` declares it Postgres only
      with its reason;
      `services/api/tests/test_postgres_store.py::test_every_new_migration_declares_both_dialects`
      passes.
- [x] `harrier store migrate` on a shimmed store at the version before
      the erasure migration prints `postgres <previous> -> <new>`; a fresh
      store reaches `target_version()`; a SQLite store records the new
      version and changes nothing
      (`services/api/tests/test_owner_erasure.py::test_the_erasure_migration_is_postgres_only`).
- [x] No statement in the erasure migration contains `DISABLE TRIGGER`,
      `DROP TRIGGER` or `ALTER TABLE`, and every trigger present before it
      is present and enabled after it
      (`services/api/tests/test_owner_erasure.py::test_the_erasure_migration_leaves_every_trigger_in_place`).
- [x] As the test superuser, without the setting, deleting a `job_events`
      row and a `tracks` row is still refused with the existing messages,
      and `TRUNCATE job_events` and `TRUNCATE tracks CASCADE` still are
      (`services/api/tests/test_owner_policy.py::test_truncate_cannot_empty_events_or_tracks`
      unchanged, plus `services/api/tests/test_owner_erasure.py::test_refusals_hold_without_the_erase_setting`).
- [x] With `harrier.erase_owner` set to owner A, the table owner's delete
      of owner B's event and track is refused, an update of A's event is
      refused, and `TRUNCATE` is refused
      (`services/api/tests/test_owner_erasure.py::test_the_erase_setting_lets_through_only_its_owners_deletes`).
- [x] As `harrier_tenant` with the setting set to its own id, a delete on
      `job_events` and on `tracks` is refused; with `DELETE` granted to it
      inside the test, still refused by the trigger
      (`services/api/tests/test_owner_erasure.py::test_the_erase_setting_alone_lets_no_tenant_delete`).
- [x] With two owners whose rows fill every table in `OWNED_TABLES`,
      `harrier_erase_owner(A)` as `service_role` leaves no row of A in any
      of them, leaves every row of B unchanged, returns one count per
      table, and leaves `harrier.erase_owner` empty afterwards
      (`services/api/tests/test_owner_erasure.py::test_erase_removes_one_owner_from_every_owned_table`).
- [x] After that erase, deleting A from the shim's `auth.users` succeeds;
      before it, the same delete is refused by the foreign key
      (`services/api/tests/test_owner_erasure.py::test_an_erased_owner_can_be_deleted`).
- [x] While one transaction holds an uncommitted erase of A, owner B
      inserts a job, an event and a track with `lock_timeout` of 100 ms,
      and each succeeds (`services/api/tests/test_owner_erasure.py::test_erasing_one_owner_never_waits_for_another`).
- [x] `harrier_erase_owner` executed as `harrier_tenant`, `authenticated`
      and `anon` is refused with `permission denied`
      (`services/api/tests/test_owner_erasure.py::test_only_service_role_can_erase`).
- [x] A definer without `BYPASSRLS` raises the "would erase nothing"
      message and deletes nothing
      (`services/api/tests/test_owner_erasure.py::test_erase_refuses_a_definer_bound_by_row_security`).
- [x] An erase that fails part way (an event inserted for one of A's jobs
      between its two deletes, simulated with a trigger in the test)
      leaves every one of A's rows in place
      (`services/api/tests/test_owner_erasure.py::test_a_failed_erase_deletes_nothing`).
- [x] The catalog test passes, and fails when `harrier_erase_owner` is
      granted to `harrier_tenant`, and when a second definer function
      appears
      (`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`
      and `services/api/tests/test_owner_policy.py::test_the_catalog_names_each_breach`,
      both extended).

Operator commands (admin and Storage calls through an injected `httpx`
transport; no test reaches the network):

- [x] `harrier operator invite` reads the address from standard input,
      posts the admin user creation with `email_confirm` true and no
      password, prints the user id and no link, and the address appears
      in no argument, log record or error text (decision D6; `services/api/tests/test_operator.py::test_invite_creates_a_confirmed_user_and_never_prints_the_address`).
- [x] No `generate_link` call and no `redirect_to` appears in
      `harrier/operator.py`, and no hosted SPA route is an invitation page
      (decision D6;
      `services/api/tests/test_operator.py::test_no_link_and_no_invitation_page_exist`).
- [x] An address with an account exits 1 with the fixed text and prints no
      user id (`services/api/tests/test_operator.py::test_invite_refuses_an_existing_address`).
- [x] `harrier operator erase` runs ban, Storage, rows, auth delete and
      stragglers in that order, appends one ledger line with no email, and
      exits 0 (`services/api/tests/test_operator.py::test_erase_runs_its_steps_in_order`).
- [x] A failure at each step exits 1 naming it, and a re-run with the same
      id completes the remaining steps without repeating a failed delete
      (`services/api/tests/test_operator.py::test_erase_resumes_after_a_failed_step`).
- [x] A row written after step 3 makes step 4 exit 1 with the "run it again"
      text (`services/api/tests/test_operator.py::test_erase_reports_rows_written_after_it`).
- [x] Missing variables, a mismatched `--confirm`, a login that cannot act
      as `service_role`, and the secret key defined in `.env` each exit 1
      before any call, and no output contains the key, the URL's password
      or an address (`services/api/tests/test_operator.py::test_operator_refusals_name_no_secret`).
- [x] `harrier operator` runs on a Postgres URL; `erase` declares
      `service_role` and `invite` declares that it opens no database
      connection; both are refused beside SQLite before anything is read or
      called; every command outside the classes still refuses (decision D2;
      `services/api/tests/test_operator_class.py::test_every_operator_subcommand_declares_its_role`,
      `services/api/tests/test_operator_class.py::test_the_operator_class_runs_only_on_postgres`,
      `services/api/tests/test_operator.py::test_the_operator_group_runs_on_postgres_only`).

Documents and other specs:

- [x] Once approved, the specs named under "Changes to approved specs"
      carry those lines, and nothing else in them changes.
- [x] ADR-013's Consequences carry the "Amended (spec 111)" note.
- [x] `docs/architecture.md`'s hosted section states the sign-up settings,
      the two commands, the erasure order, the ledger and the restore
      procedure, naming the tests above.

Gates:

- [x] `uv run ruff check` and `uv run pyright` pass on the service.
- [x] CI's `check-python` job runs the new Postgres tests, none skipped.
- [x] `just check` passes.

## Honest limitations

- **The operator sees everything.** Row-level policy separates owners from
  each other. It does not separate them from whoever holds the `postgres`
  role or the dashboard. This spec narrows what harrier's own commands
  print; it cannot narrow what the platform shows its project owner.
- **The table owner can still bypass the refusals.** The role that owns the
  tables can set `harrier.erase_owner` by hand, or disable or drop the
  triggers, as migration 10 did (`schema.py:1071-1073`). The refusals guard
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
  For Storage the race does not fail: a token can write an object under the
  owner's prefix after step 5 has listed it, and no later step finds it.
  Spec 107's amendment of 2026-10-15 closes it in the policy, which this
  build does not create (CodeRabbit on PR #348).
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
- The line numbers cited here were refreshed by the change that implements
  this spec (decision D9, 2026-10-11; done 2026-10-15). They move with
  every later edit to those files.

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

Each line below is a change this spec asks of an approved spec. None was
written into that spec when this text was approved. The change that builds
this spec writes them (2026-10-15), where they were not written already:
spec 110 carried its lines from its Amendment (2026-10-11); specs 104, 105,
107 and 112, spec 122's note and ADR-013 gain theirs; spec 117 existed.

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
  `:682-686`; the refusal triggers at `:703-704`, `:785-786`, `:789-790`,
  `:1142-1143`, `:1146-1147`; `MIGRATION_10_OWNED_TABLES` at `:810-818` and
  `OWNED_TABLES` at `:825-832` (one list, `OWNED_TABLES` at `:606-614`, until
  spec 122 split it; noted 2026-10-11); the disabled trigger at
  `:1071-1073`; the `owner_id` foreign key at `:1076-1080`;
  `harrier_new_owner()` at `:1154-1166`; the grants at `:1196-1218`;
  `POSTGRES_MIGRATIONS` at `:1903-1946`, whose latest version
  `target_version()` returns (`services/api/src/harrier/pgstore.py:156-160`).
- `services/api/tests/test_owner_policy.py`: `catalog_problems`, its
  definer allowlist and `NO_PRIVILEGE_ROLES`.
- `services/api/src/harrier_cli/main.py`: `load_project_env` at `:79-99`,
  the Postgres gate at `:3838-3842`.
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

## Amendment (2026-10-11): the erase covers `job_eligibility`

Akin approved this section in chat on 2026-10-11 ("approve the spec 111
amendment"); this line records it. Nothing below is built yet. Line
numbers are from `main` at `5c3d083`, after migration 16 (they were taken
at `f26abe0`).

### Problem

Spec 122 added an owned table after this spec was approved:
`job_eligibility`, migration 15 (`POSTGRES_JOB_ELIGIBILITY`,
`services/api/src/harrier/tracker/schema.py:1447-1482`). It holds one
owner's eligibility assessments of their jobs, with posting excerpts as
evidence. Three facts about it meet this spec's erase:

- It is in `OWNED_TABLES`, first: `("job_eligibility",
  *MIGRATION_10_OWNED_TABLES)` (`schema.py:825-832`). Its `owner_id`
  references `auth.users (id)` with no `ON DELETE` action (`:1454`), like
  every other owned table.
- Its rows reference a job of the same owner: `FOREIGN KEY (owner_id,
  job_id) REFERENCES jobs (owner_id, id)` (`:1455`).
- It is append-only, as `job_events` is. Row triggers refuse `UPDATE` and
  `DELETE` and a statement trigger refuses `TRUNCATE`, each through
  `harrier_refuse('job_eligibility is append-only')` (`:1461-1471`).

Behavior, "`harrier_erase_owner(target uuid)`", step 4, names the seven
tables migration 10 owned and deletes in that order. Built as it
stands, the function never deletes an assessment. Discovery and capture
write one with every kept industry row (spec 122, Scope), so any owner
with such a row since spec 122 has some. For that owner the function's
`DELETE FROM jobs` fails on `job_eligibility`'s foreign key, the
transaction rolls back, and the command exits 1 at its step 3 on every
run. Nothing is erased, and a re-run cannot help.

`services/api/tests/test_owner_erasure.py::test_erase_removes_one_owner_from_every_owned_table`
already fills every table in `OWNED_TABLES`, and `fill` in
`services/api/tests/test_owner_policy.py` writes a `job_eligibility` row
for each owner, so that test would fail. This amendment makes the text
say what the test already demands.

Export is not affected. Spec 117, and this spec's "Export (moved to spec
117)", both say "one JSON Lines file per table in `OWNED_TABLES`", which
now includes `job_eligibility`.

### Behavior after the change

**The function deletes eight tables, in `OWNED_TABLES` order.**
`harrier_erase_owner`'s step 4 deletes `WHERE owner_id = target` from
`job_eligibility`, `job_events`, `jobs`, `contacts`, `profile_documents`,
`user_config`, `job_runs`, `tracks`, in that order. `job_eligibility`
goes first because it references `jobs`. The erasure migration writes
the list literally, as before. It returns one row per table, so eight
rows, the first for `job_eligibility`.

**`job_eligibility` stands aside exactly as `job_events` does.** The
replaced `harrier_refuse()` (Behavior, "How `harrier_refuse()` stands
aside") is unchanged. It decides by the operation, the row's own
`owner_id`, the setting and the role, not by the table. So a row-level
`DELETE` of a `job_eligibility` row passes only when
`harrier.erase_owner` names that row's owner and the current role owns
the table. An `UPDATE` of an assessment, and `TRUNCATE job_eligibility`,
are refused as today, with `job_eligibility is append-only`.

**No session but the erase holds `DELETE` on it.** `harrier_tenant`
holds `SELECT`, and `INSERT` on every column but `id`
(`schema.py:1480-1481`). `service_role` holds nothing. As for
`job_events`, the privilege check refuses a tenant's delete before the
trigger fires.

**The table owner is the definer.** The role check in `harrier_refuse()`
compares `current_user` with the table's owner. `job_eligibility` is the
first owned table created after migration 10. A store migrated by one
role gives it the same owner as the other seven, and the erase passes. A
store whose migration 15 ran as a different role than the erasure
migration would refuse the erase on `job_eligibility` with `job_eligibility
is append-only`, and the whole erase rolls back. The erasure migration
checks this before it creates the function (Failure modes).

**The output names the new table.** `harrier operator erase` prints a
`job_eligibility` line with its count, before the `job_events` line.
Counts are not personal data, as before.

**Concurrent writes.** Behavior, "Concurrent writes by the owner being
erased", holds for assessments as for events. An assessment inserted for
one of the owner's jobs after the erase deleted their assessments, but
before it deleted that job, makes the job's delete fail its foreign key.
The erase transaction rolls back whole, the command exits 1 at step 3,
and a re-run erases both.

### Failure modes

- **An assessment written during the erase.** Exit 1 at step 3, nothing
  deleted, a re-run completes (Behavior after the change).
- **The erasure migration on a store whose `job_eligibility` has another
  owner.** The migration refuses, before creating the function, with
  `the erasure migration found owned tables with different owners; every
  owned table must have one owner`, naming the tables and not the roles.
  Without this check, the first erase would fail on `job_eligibility`
  and report it as append-only, which points at the wrong cause.
- **An update of an assessment during an erase.** Refused as today.
- **`TRUNCATE job_eligibility` during an erase.** Refused as today; the
  statement trigger never stands aside.
- **A later owned table.** Unchanged from Failure modes: it replaces the
  function in its own migration, and the test that fills every table in
  `OWNED_TABLES` fails until it does.

### Acceptance criteria

These add to the criteria under "Migration and database". Where one
names an existing test, that test gains the case.

- [x] The erasure migration's `harrier_erase_owner` lists the eight
      tables of `OWNED_TABLES` in its order, `job_eligibility` first; a
      test compares the list in the migration's text with `OWNED_TABLES`
      (`services/api/tests/test_owner_erasure.py::test_the_erase_lists_every_owned_table_in_order`).
- [x] With two owners whose rows fill every table in `OWNED_TABLES`,
      `harrier_erase_owner(A)` returns eight rows, the first
      `job_eligibility` with a count of at least one, and leaves no
      `job_eligibility` row of A and every one of B
      (`services/api/tests/test_owner_erasure.py::test_erase_removes_one_owner_from_every_owned_table`).
- [x] As the table owner with `harrier.erase_owner` set to A: deleting
      A's assessment passes; deleting B's is refused with
      `job_eligibility is append-only`; updating A's is refused; and
      `TRUNCATE job_eligibility` is refused
      (`services/api/tests/test_owner_erasure.py::test_the_erase_setting_lets_through_only_its_owners_deletes`).
- [x] Without the setting, the table owner's delete of a `job_eligibility`
      row is refused with `job_eligibility is append-only`
      (`services/api/tests/test_owner_erasure.py::test_refusals_hold_without_the_erase_setting`), and
      `services/api/tests/test_owner_policy.py::test_truncate_cannot_empty_events_or_tracks`
      and `services/api/tests/test_eligibility.py::test_eligibility_history_is_append_only`
      pass unchanged.
- [x] As `harrier_tenant` with the setting set to its own id, a delete on
      `job_eligibility` is refused; with `DELETE` granted to it inside the
      test, still refused by the trigger
      (`services/api/tests/test_owner_erasure.py::test_the_erase_setting_alone_lets_no_tenant_delete`).
- [x] An assessment inserted for one of A's jobs between the erase's
      `job_eligibility` delete and its `jobs` delete (simulated with a
      trigger in the test) leaves every one of A's rows in place
      (`services/api/tests/test_owner_erasure.py::test_a_failed_erase_deletes_nothing`).
- [x] The erasure migration on a store where `job_eligibility` has a
      different owner from the other owned tables refuses with the text
      under Failure modes and creates no function
      (`services/api/tests/test_owner_erasure.py::test_the_erasure_migration_refuses_mixed_table_owners`).
- [x] `harrier operator erase` prints a `job_eligibility` line before the
      `job_events` line (`services/api/tests/test_operator.py::test_erase_runs_its_steps_in_order`).

### Changes to approved specs

Written into each spec once Akin approves this amendment, as the list
under "Changes to approved specs" above.

- **Spec 122:** its Storage section says `job_eligibility` is
  "Append-only by trigger on both dialects, as `job_events` is". It gains:
  on Postgres, one owner's erasure removes that owner's rows, as it does
  for `job_events` (spec 111). An update is never allowed, and
  `TRUNCATE` is refused.
- **ADR-013:** Scope item 10's "Amended (spec 111)" note names
  `job_eligibility` with `job_events` and `tracks` as the tables that
  yield to one owner's erasure and to nothing else.
- **Spec 117:** no change. Its "one JSON Lines file per table in
  `OWNED_TABLES`" already covers `job_eligibility`, and its refinement
  keeps that wording.

### Out of scope

- Export: spec 117, unchanged (Problem).
- SQLite. `job_eligibility` stays append-only on the local store, and
  local deletion stays deleting the data directory.
- The erase's mechanism. `harrier_refuse()`, the setting, the role check,
  the grants and the command's steps are as Behavior states them.
- Spec 116's `credentials` and spec 108's `schedule_state`. Each replaces
  the function in its own migration, as Behavior already says.
- Folding this section into Behavior step 4 and Scope item 10. The
  implementing change does that. Step 4 already carries PR #253's dated
  note that `OWNED_TABLES` now puts `job_eligibility` first.

### Migration

None beyond the erasure migration. The table list it writes now has
eight entries. A hosted store reaches it through `harrier store migrate`,
as before.

### Limitations

- **The owner check is new.** It is the only behavior here that the
  stated design did not already imply. It assumes every migration of a
  store runs as one role. A store migrated by two roles cannot gain the
  erase until its tables are reassigned to one owner by hand.
- **Spec 079's rule now yields twice.** `job_events` and
  `job_eligibility` are both append-only by spec 079's reasoning, and
  both yield to the erase. Nothing else changes about either.

### Open decisions for Akin

1. **The owner check in the erasure migration.** Recommended: refuse the
   migration when the owned tables have more than one owner. Alternative:
   no check, and a mixed-owner store fails at its first erase with the
   append-only message.
   Taken 2026-10-11: Akin chose to keep the check, in chat ("keep the
   owner check"). The rest of this amendment was approved later the same
   day.

### Proof / origin

- `specs/122-each-posting-says-whether-it-can-hire-from-turkiye.md`:
  Scope (migration 15, `OWNED_TABLES`), Storage ("Append-only by trigger
  on both dialects"), and Amendment item 2 (the frozen lists).
- `services/api/src/harrier/tracker/schema.py`: `OWNED_TABLES` at `:825-832`,
  `POSTGRES_JOB_ELIGIBILITY` at `:1447-1482`, the `owner_id` and job
  references at `:1454-1455`, the refusal triggers at `:1461-1471`, the
  tenant's grants at `:1480-1481`.
- `services/api/tests/test_owner_policy.py`: `REQUIRED`, `fill` and
  `JOB_HISTORY_TABLES`, which already write and treat `job_eligibility`
  as an owned history table; `test_truncate_cannot_empty_events_or_tracks`.
- `specs/117-an-owner-exports-everything-they-own.md`: Acceptance
  criteria ("one JSON Lines file per table in `OWNED_TABLES`").
- PR #253's out-of-scope note, which found the gap.

## Amendment (2026-10-15): the erase before spec 107 is built

Akin decided this in chat on 2026-10-15: amend this spec instead of
building spec 107 first. This line records it. Nothing here changes an
accepted decision of the stub, and the amendment below it (what building
this spec settled) is separate.

### Problem

Spec 107 (Storage) is approved and not built. No code writes to the
`harrier-files` bucket, and there is no Storage seam. Behavior, "Deletion",
steps 2 (Storage) and 5 (stragglers) assume that seam: each deletes every
object under the owner's prefix "through spec 107's seam". With no seam
and no object ever written by harrier, the steps would have nothing to call.
This spec does not build the seam.

### Behavior until spec 107's seam exists

Steps 2 and 5 do this and nothing more:

- **List, read only.** The command lists the owner's prefix, `<owner>/`, in
  the bucket `harrier-files` through the Storage API, with
  `HARRIER_SUPABASE_SECRET_KEY`. The call is `POST
  {HARRIER_SUPABASE_URL}/storage/v1/object/list/harrier-files`, the header
  `apikey` carrying the key, and the JSON body `{"prefix": "<owner>",
  "limit": 1, "offset": 0}`, where `<owner>` is the user id. The body holds
  the id without a trailing slash, the way the reference's own example,
  `folder/subfolder`, writes a prefix, and the list is of what is inside
  that folder. A limit of one is enough: the answer is only empty or not.
- **Any entry refuses the step.** If the listing holds anything, the erase
  stops at that step with exit 1 and `error: step 2 (storage): objects
  exist under the owner's prefix that this build cannot delete (spec 111,
  Amendment (2026-10-15)); nothing was deleted here` (step 5 for the second
  listing). Nothing is deleted at that step. Storage shows a folder entry
  only when an object lies under it, so a folder entry counts.
- **An empty listing passes the step.**
- **Nothing here deletes a Storage object.** No call to a Storage delete
  route is made, and nothing deletes a row from `storage.objects` with SQL,
  which Supabase warns leaves the files behind (step 2 above).
- **What it costs.** An owner who has objects cannot be erased by this
  build, and the command says so at step 2, before any row is deleted. At
  step 2 nothing but the ban has happened, and the ban is idempotent. At
  step 5 the rows and the auth user are already gone. Supabase refuses to
  delete a user who owns objects (Managing user data), so in practice step
  4 fails first and a re-run reaches step 2 again.
- **Replaced when spec 107's seam lands.** Its build replaces this check
  with the delete that "Deletion", steps 2 and 5, describe, and updates the
  test that pins this behavior,
  `services/api/tests/test_operator.py::test_this_build_deletes_no_storage_object`.
  Spec 107 carries a line saying so (Changes to approved specs, below).

### Where the request shape comes from

Read 2026-10-15, from Supabase's own pages:

- Storage Server Reference, "Search for objects under a prefix": `POST
  /object/list/{bucketName}`, body `prefix` (required), `limit`, `offset`,
  `sortBy` and `search`, answering an array of entries.
  https://supabase.com/docs/reference/self-hosting-storage/v1/search-for-objects-under-a-prefix
- Storage server source, `src/http/routes/object/listObjects.ts`: the route
  `POST /list/:bucketName`, with the body schema (`prefix` required, `limit`
  an integer of at least 1, `offset` an integer of at least 0). The reference
  above is for the self-hosted server. This spec assumes the hosted API
  serves the same routes under `/storage/v1`, and spec 110's staging check
  confirms it.
  https://github.com/supabase/storage/blob/master/src/http/routes/object/listObjects.ts
- JavaScript reference, `list`: "Lists all the files and folders within a
  path of the bucket"; a folder entry has `id` and `metadata` null.
  https://supabase.com/docs/reference/javascript/storage-from-list
- API keys guide: a secret key goes in the `apikey` header, not in
  `Authorization: Bearer`, because it is not a JWT.
  https://supabase.com/docs/guides/api/api-keys

### Failure modes

- **Objects under the prefix.** Exit 1 at step 2 or 5 with the text above.
  Nothing is deleted at that step.
- **The Storage call fails.** Exit 1 at the step, naming the HTTP status and
  nothing the server sent.
- **The answer is not a list.** Exit 1 at the step.
- **The prefix form is wrong for the hosted server.** An unrecognized form
  could list nothing for an owner who has objects. The erase would then
  reach step 4, where Supabase refuses to delete a user who owns objects,
  and exit 1 there. Spec 110's staging check confirms the form (assumption
  at the end of this amendment).

### Acceptance criteria

- [x] With an object listed at step 2, the erase exits 1 at step 2 with the
      text above, deletes nothing at that step, and makes no auth delete or
      row call
      (`services/api/tests/test_operator.py::test_erase_resumes_after_a_failed_step`).
- [x] With an empty listing the step passes and the run goes on
      (`services/api/tests/test_operator.py::test_erase_runs_its_steps_in_order`).
- [x] A full erase and a refused one make no Storage call but the list, the
      list's request has the shape above, and the operator module names no
      `storage.objects` and no Storage delete route
      (`services/api/tests/test_operator.py::test_this_build_deletes_no_storage_object`).

### Out of scope

- The Storage seam, and deleting an object: spec 107.
- A copy of the owner's objects before an erase: spec 117 (export).

### Assumption for spec 110's staging check, unconfirmed

- The hosted Storage server lists an owner's objects for `POST
  /storage/v1/object/list/harrier-files` with the prefix written as the bare
  user id, and answers an empty array when none exist.

## Amendment (2026-10-15): what building it settled

Written by the change that builds this spec, which found these gaps. Each
amends the text above where the two differ. Nothing here widens what the
commands may do.

1. **The erase covers twelve tables, not eight.** The amendment of
   2026-10-11 counts eight (`job_eligibility` and migration 10's seven).
   Four owned tables landed before the erasure migration: `job_verdicts`
   (spec 139, migration 21), `schedule_state` (spec 108, migration 17),
   `credentials` (spec 116, migration 18) and `run_records` (spec 106,
   migration 19). Behavior says a later owned table replaces the function in
   its own migration. These came earlier, so the first version of the
   function lists them. It deletes from `job_eligibility`, `job_verdicts`,
   `job_events`, `jobs`, `contacts`, `profile_documents`, `user_config`,
   `job_runs`, `tracks`, `schedule_state`, `credentials` and `run_records`,
   in `OWNED_TABLES` order: a referencing row before the row it references
   (`job_verdicts` to `jobs` and `tracks`). It returns twelve rows, and
   `harrier operator erase` prints twelve table lines. `job_verdicts` is
   append-only by trigger, as `job_eligibility` is, and stands aside the
   same way: `harrier_refuse()` decides by the operation, the row's owner,
   the setting and the role, not by the table. The tenant role holds no
   `DELETE` on `job_verdicts`, `schedule_state` or `run_records` (specs 139,
   108 and 106 left deletion to this spec).
2. **The owner check compares with the migrating role.** The check in the
   amendment of 2026-10-11 compared tables with each other. The function is
   owned by the role that runs the migration, and the refusal compares the
   role it runs as with each table's owner, so the check names every owned
   table whose owner is not that role. A store whose tables all had one
   other owner would otherwise pass and fail at its first erase. The text
   is as stated, followed by the table names in parentheses, and no role
   name.
3. **The parameter keeps its name.** `run_records` has a column named
   `target`, which collides with the parameter in `WHERE owner_id = target`.
   The function body opens with `#variable_conflict use_variable`, so the
   name Behavior gives is kept.
4. **The seam takes `service_role` for a transaction.** Step 3 needs `SET
   LOCAL ROLE service_role` and a read-back, and `Connection.execute`
   refuses a `SET` (spec 112, amendment of 2026-10-14: the session).
   `transaction(service_role=True)` in
   `services/api/src/harrier/connection.py` sends the `SET LOCAL ROLE` after
   the lock timeout and reads `current_user` back before the commit, as
   `schedule_reader` does. It is refused with another role, nested, on
   SQLite, and on a connection with an owner's claims. The command also
   reads the role back before the function runs, so a transaction that did
   not take the role raises before anything is deleted (`erase_rows` in
   `services/api/src/harrier/operator.py`).
5. **A preflight comes before the first call.** After the refusals that need
   no connection (a mismatched `--confirm`, an id that is not a UUID, a
   missing or malformed variable, the secret key in `.env`), the command
   opens the connection and checks that the login may `SET ROLE
   service_role` and that `harrier_erase_owner(uuid)` exists, reading only
   the catalog. Either failing exits 1 before any auth or Storage call:
   `error: harrier operator needs a login that can act as service_role`, or
   `error: the store has no harrier_erase_owner(uuid); run harrier store
   migrate first`. The text above ran the role check "before Storage". The
   ban is step 1, so it now runs before the ban. The store's version is not
   read, because `service_role` may not read `schema_version`, and the
   operator's login need not either.
6. **A user that exists is found by a read.** Step 1 reads the user
   (`GET /auth/v1/admin/users/{id}`) before it bans, and a 404 means the user
   is gone: steps 1, 3 and 4 are skipped, each said on its own line.
7. **A refused auth delete is read.** When step 4's delete is refused, the
   command runs the erase function once more in a transaction of its own. If
   it deletes any row, rows were written after step 3, and the command says
   so: `error: step 4 (auth): rows were written after the erase; run it
   again after the user's last token expires`. The late rows are already
   erased, so a re-run completes. If it deletes none, the refusal is the
   server's, reported as `error: step 4 (auth): the server answered
   <status>`.
8. **The ledger records a run that failed after step 3.** Rows erased from
   the live copy are what a restore must erase again, whether or not the run
   finished. So a run that fails after step 3 committed also appends a line,
   with `"complete": false` and the steps that completed. A run that
   finishes appends one with `"complete": true`. Each line holds `user_id`,
   `at`, `completed` and `complete`, and nothing about the owner. A re-run
   appends its own line, and a restore that re-runs the erase for every id
   in the ledger finds duplicates harmless. A ledger that cannot be written
   exits 1 naming step 6.
9. **The `.env` check reads the file.** `load_project_env` copies `.env` into
   the environment before the command runs, so the environment cannot tell a
   key the operator exported from one the file put there. The check reads the
   file in the working directory for the variable's presence, whatever the
   environment holds, and refuses on a key defined there, even with an empty
   value.
10. **`invite` needs a Postgres URL and does not use it.** The operator group
    runs only on a Postgres store (spec 108, Amendment (2026-10-14)), so
    `HARRIER_DATABASE_URL` must name one for `invite` to run at all. The
    command opens no connection to it.
11. **Nothing is logged.** Neither command configures logging, and the HTTP
    client's request log is not emitted at the default level, so no id,
    address or key reaches a log. Errors are the printed lines only, and
    carry a step, a variable name or an HTTP status.
12. **The admin calls.** The secret key travels in the `apikey` header, as
    the API keys guide says, and no `Authorization` header is sent. Creation
    posts `{"email": <address>, "email_confirm": true}`. A 422 whose
    `error_code` is `email_exists` is an address that has an account. Ban is
    `PUT` with `{"ban_duration": "876000h"}`. The delete is `DELETE` with
    `{"should_soft_delete": false}`, and a 404 on it counts as done. These
    shapes are read from the auth server's source (`internal/api/admin.go`,
    read 2026-10-15) and are not run until spec 110's staging check.
13. **`harrier store check-deploy` allows the one grant.** Its data API
    roles check (spec 110) fails when `service_role` holds `EXECUTE` on any
    function in `public`, which every deploy meets once migration 22 is
    applied. It now exempts `service_role`'s `EXECUTE` on
    `harrier_erase_owner(uuid)` and nothing else: the same function held by
    `anon`, `authenticated` or `PUBLIC` is still a finding. Its passing line
    says so: `data api roles: anon, authenticated, service_role hold nothing
    in public but service_role's EXECUTE on harrier_erase_owner(uuid)`
    (`services/api/src/harrier/storedeploy.py`, `_check_data_api_roles`).
    This was found by `services/api/tests/test_store_deploy.py` failing.

### Acceptance criteria

- [x] The erase function lists the twelve tables of `OWNED_TABLES` in its
      order and returns twelve rows
      (`services/api/tests/test_owner_erasure.py::test_the_erase_lists_every_owned_table_in_order`,
      `services/api/tests/test_owner_erasure.py::test_erase_removes_one_owner_from_every_owned_table`).
- [x] The migration refuses when an owned table is not owned by the
      migrating role, naming the table
      (`services/api/tests/test_owner_erasure.py::test_the_erasure_migration_refuses_mixed_table_owners`).
- [x] The function holds `EXECUTE` for `service_role` alone, even where the
      migrating role's default privileges grant it to others
      (`services/api/tests/test_owner_erasure.py::test_the_erase_function_holds_only_the_grant_the_migration_gives`).
- [x] `transaction(service_role=True)` runs as `service_role`, is refused as
      stated, and a role that moved inside the block is found before the
      commit. A transaction that did not take the role raises before the
      function runs
      (`services/api/tests/test_operator.py::test_the_seam_takes_service_role_for_one_transaction`,
      `services/api/tests/test_operator.py::test_erase_refuses_a_transaction_that_did_not_act_as_service_role`).
- [x] A refused auth delete after a late write exits 1 with the "run it
      again" text, and a run that fails after step 3 appends an incomplete
      ledger line
      (`services/api/tests/test_operator.py::test_erase_reports_rows_written_after_it`,
      `services/api/tests/test_operator.py::test_erase_resumes_after_a_failed_step`).
- [x] An id with no auth user skips steps 1, 3 and 4 and still writes the
      ledger
      (`services/api/tests/test_operator.py::test_erase_of_an_id_with_no_user_still_clears_storage_and_writes_the_ledger`).
- [x] A ledger that cannot be written fails step 6
      (`services/api/tests/test_operator.py::test_a_ledger_that_cannot_be_written_fails_step_six`).
- [x] Both commands are refused beside SQLite before anything is read or
      called
      (`services/api/tests/test_operator.py::test_the_operator_group_runs_on_postgres_only`).
- [x] `harrier store check-deploy` passes on a migrated store with the one
      grant, and still names the erase function held by `authenticated` or
      by `PUBLIC`
      (`services/api/tests/test_store_deploy.py::test_check_deploy_passes_on_a_migrated_granted_store`,
      `services/api/tests/test_store_deploy.py::test_check_deploy_reads_the_data_api_roles_effective_privileges`).

### Changes to approved specs

Each written by the same change, as one note or line:

- **Spec 107:** a note that its build replaces spec 111's list-only check
  with the delete and updates the test that pins it, and that the seam can
  list and delete every object under one owner's prefix for an operator
  command that holds a secret key, outside any request. The second is the
  line "Changes to approved specs" above always asked of it.
- **Spec 112:** a note that `transaction` takes `service_role` (item 4), and
  its Out of scope line, "A tenant export", now reads spec 117.
- **Spec 110:** a note that its `check-deploy` data API roles check allows
  `service_role`'s `EXECUTE` on `harrier_erase_owner(uuid)` (item 13).
- **Spec 139:** a note that on Postgres one owner's erasure removes that
  owner's `job_verdicts` rows, as it does for `job_eligibility` (item 1).
- **Specs 104, 105 and 122, and ADR-013:** the lines "Changes to approved
  specs" and the amendment of 2026-10-11 ask of them.

### Limitations

- **The shim is not Supabase.** The fake auth server is backed by the shim's
  `auth.users`. The admin and Storage routes, their headers, the shape of a
  duplicate-address answer and the Storage prefix form are read from
  documentation and source, not run.
- **No Storage object is deleted here.** Until spec 107, an owner who has
  objects is refused at step 2, not erased.
- **A test cannot show `harrier.erase_owner` is transaction-local.** The
  function resets the setting before it returns, and a rollback reverts any
  setting, so a session-level setting would behave the same in every case a
  test can build. The rollback test pins that the rows and the setting go
  back together.

### Proof / origin

- Akin's decision of 2026-10-15, in chat: amend this spec rather than build
  spec 107 first.
- The Supabase pages and source named under "Where the request shape comes
  from", and the auth server's `internal/api/admin.go` (read 2026-10-15):
  `adminUserCreate` answers 422 with `error_code` `email_exists` for a
  duplicate address, `adminUserDelete` takes `should_soft_delete` and
  `loadUser` answers 404 `user_not_found` for an unknown id, and
  `adminUserUpdate` takes `ban_duration`.
  https://github.com/supabase/auth/blob/master/internal/api/admin.go
- `services/api/src/harrier/operator.py`,
  `services/api/src/harrier/connection.py` and
  `services/api/src/harrier/tracker/schema.py` (`POSTGRES_ERASURE`).

## Amendment (2026-10-15): the database decides whether a user exists

Found by the first `harrier operator erase-test-owners` run on staging (spec
143). The erase asked only the auth server whether a user exists, and a 404
read as "already erased": steps 1, 3 and 4 were skipped and the ledger line
said complete. Spec 115's test owners were inserted into `auth.users` by SQL
with only an id, so the auth server answers 404 for them while the database
still holds them and their rows. The run reported 167 owners erased and
deleted nothing.

**Behavior after the change.** When the auth server answers 404, the erase
reads `auth.users` as the operator's login:

- The row is there: the auth server does not know this user. Step 1 is
  skipped (such a user cannot sign in), step 3 erases the rows, and step 4
  deletes the `auth.users` row with SQL, since the admin API cannot delete
  what it cannot see. The output says which.
- The row is not there: the user is erased already, and steps 1, 3 and 4
  are skipped as before.
- The login cannot read `auth.users`: the erase stops at step 1 and says so,
  because it cannot tell the two cases apart. A dedicated login that is
  only a member of `service_role` is such a login (spec 143, Amendment);
  `harrier operator erase` of a user the auth server knows does not reach
  this read.

`services/api/tests/test_operator_erase_test_owners.py::test_erase_test_owners_erases_owners_the_auth_server_cannot_see`
proves it, with the fake auth server answering 404 for ids `auth.users`
holds. The test fails on the code before this change.

## Amendment (2026-10-15): `.env` never supplies the secret key

The operator commands refuse a `HARRIER_SUPABASE_SECRET_KEY` defined in
`.env`, but the CLI's loader still copied that key into the environment
first. `load_project_env` copied every key that was not already set, so a
secret key in `.env` reached every command and every child process, including
the ones that never needed it. The refusal fired only on the two operator
commands. Spec 141 closed the same gap for `HARRIER_IMPORT_DATABASE_URL` and
spec 144 for `HARRIER_ACCOUNT_DATABASE_URL`.

**Behavior after the change.** `load_project_env` in
`services/api/src/harrier_cli/main.py` skips `HARRIER_SUPABASE_SECRET_KEY`.
The file's other keys load as before, and a key the shell already exported
is untouched. The secret key reaches a command only from the shell that runs
it. This replaces the sentence that says `load_project_env` copies `.env`
into the environment, in the Behavior paragraph on the `.env` refusal and in
item 9 of Amendment (2026-10-15): what building it settled, as far as this
key is concerned. Nothing else in those two passages changes.

The refusal is unchanged. `read_admin` in `services/api/src/harrier/operator.py`
reads the file itself for the variable's presence, whatever the environment
holds, so `harrier operator invite` and `harrier operator erase` still exit 1
with the text that names the variable and the `.env` file, and never the
value. It does not depend on the loader.

`services/api/tests/test_operator.py::test_dotenv_never_supplies_the_secret_key`
proves both halves. A `.env` holding the secret key and another key loads
the other key and leaves the secret key out of the environment, and
`harrier operator erase` still refuses naming the `.env` file. The
environment assertion fails on the code before this change.

Limitation: the skip is by name, as it is for the two URLs. A key that holds
a secret under another name is not caught, and the loader cannot tell.
