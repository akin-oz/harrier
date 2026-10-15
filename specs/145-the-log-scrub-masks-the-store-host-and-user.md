---
spec: 145
title: The log scrub masks the store host and user
status: accepted
approved: yes
milestone: M10
depends: [133, 135, 141, 144]
---

# Spec 145: The log scrub masks the store host and user

## Problem

The secret scrub (`SecretScrubFilter` in
`services/api/src/harrier/logredact.py`, installed by
`services/api/src/harrier/logsetup.py`) masks the store password in every
log record. It does not mask the store's host or user name. Spec 144 promises that no
output line of a run with `--owner` names the host. Specs 133, 135 and 141
do not promise this.

The finding from the privacy review of PR #349 named one route: psycopg's
`_try_cancel` (`psycopg/connection.py`, line 349 in psycopg 3.3.6) logs
`"query cancellation failed: %s"` with the error as the argument. That route
does not leak today. Spec 135's driver-error rule replaces a psycopg error
argument with the seam's text. A client-side error carries no server
message, so the line reads `query cancellation failed: OperationalError`.
This was run against the scrub on the last-resort handler with a libpq
message that named a host and an address. Neither appeared.

A neighbouring route does leak. psycopg logs its own objects as arguments:

- `"error ignored in rollback of %s: %s"` (`psycopg/transaction.py`, 130)
- `"error ignored in rollback on %s: %s"` (`psycopg/connection.py`, 174)
- `"error ignored terminating %r: %s"` and `"error ignored exiting %r: %s"`
  (`psycopg/_pipeline.py`, `psycopg/_pipeline_base.py`)

The repr of a connection, transaction or pipeline is built by
`psycopg.pq.misc.connection_summary`. When libpq reports the connection as
OK, that repr carries `host=`, `port=` (when not 5432), `user=` (when it
differs from the database name) and `database=`. The same reproduction
printed:

```
error ignored in rollback of <psycopg.Transaction (ACTIVE) [INERROR] (host=db.abcdefghijklmnop.supabase.co port=6543 user=postgres.abcdefghijklmnop database=postgres) at 0x1>: OperationalError
```

Where harrier reaches it today: psycopg's own `transaction()` is used
directly only by the migration code in `services/api/src/harrier/pgstore.py`
(`_apply_under_the_session_lock`), which only `harrier store migrate` calls.
`harrier store` never configures logging, so the line goes through the
last-resort handler. It fires when a ROLLBACK fails while libpq still
reports the connection as OK, such as a dropped network link it has not yet
noticed, after Ctrl-C or any other error inside a migration.

`harrier store import-local` (spec 141) and the `--owner` commands (spec
144) go through harrier's own `Connection`, which issues `BEGIN` and
`ROLLBACK` itself and logs no psycopg object. For their URLs the exposure is
latent: the next psycopg warning that takes an object argument, or a
harrier log line that passes one, would print it unmasked.

## Scope

- `services/api/src/harrier/logredact.py`: `SecretScrubFilter` masks the
  hosts described below.
- Tests in `services/api/tests/test_logging.py`.
- `services/api/tests/test_store_password_scrub.py`, as the amendment
  below states.

## Behavior

**The hosts.** For each of `HARRIER_DATABASE_URL`,
`HARRIER_IMPORT_DATABASE_URL` and `HARRIER_ACCOUNT_DATABASE_URL` that holds
a Postgres URL when a record is logged, its hosts are every entry of the
`host` and `hostaddr` values libpq reads from it, split at commas. When
libpq cannot parse the URL, the host is the one `urlsplit` reads.
`HARRIER_DATABASE_URL` is read as `store_target` reads it: the environment,
then `.env` in the working directory. The other two are read from the
environment only, as specs 141 and 144 read them.

**Not a host for this rule.** `localhost`, `127.0.0.1`, `::1`, and an
entry that starts with `/` (a socket directory, which psycopg's repr does
not print). They name no one's server.

**The mask.** `SecretScrubFilter` replaces each host with `REDACTED` in the
record's message, exception text and stack text, wherever the password is
masked today, and in the same pass. Matching is case-insensitive. A host is
replaced only where it is not part of a longer host name: the character
before it and the character after it are each not a letter, digit or `-`,
and not a `.` followed by a letter or digit. A character before the host
that ends an escape counts as a boundary too: a backslash escape such as
`\n` written out by `repr`, or a percent escape such as `%40` (`@`) in an
encoded URL. An IPv6 address is matched with and without its brackets. The
port and everything else on the line stay.

**The user names.** For the same three variables, the user name is the
`user` value libpq reads from the URL, or the one `urlsplit` reads when
libpq cannot parse it. On Supabase's pooler it is `postgres.<project ref>`,
and the ref names the project as surely as the host does. psycopg's repr
prints it whenever it differs from the database name.

A user name is replaced with `REDACTED` wherever it stands as a whole
role name, in any of its forms: as libpq reads it, as the URL writes it,
and percent-encoded. Each of those forms, and each host, is also matched as
`repr` and `json.dumps` quote it, as the password forms are (spec 133):
JSON writes a non-ASCII character as a `\u` escape, so `café` is
`caf\u00e9` there. It stands whole where the character before it and the
character after it cannot continue a role name: neither is a letter, digit,
`_`, `$` or `-`, and a `.` counts only where another such character follows
it after the name, or comes before it. A letter or digit before the name
that ends an escape counts as a boundary, as it does for a host. So the
user is masked after `user=` (bare or quoted, with or without spaces), in
a URL's userinfo (plain, JSON-escaped or percent-encoded), in a dict or
JSON value, before `@host` without a scheme, and in libpq's own text
(`for user "<name>"`).

**The plain role `postgres` is never masked.** It is the default role of
every Postgres server and names no project, and as a word it appears in
ordinary messages (`the Postgres store`, `postgresql://`). A store whose
user is `postgres` masks its host and password only.

Matching is exact, as Postgres role names are. A role name that is not a
store's user, such as one the schema creates, stays. A store's user that is
also a word in a message, such as a role named `harrier_account`, is masked
in that message too: the rule hides more than the identifying value, never
less.

So the line in Problem prints as:

```
error ignored in rollback of <psycopg.Transaction (ACTIVE) [INERROR] (host=REDACTED port=6543 user=REDACTED database=postgres) at 0x1>: OperationalError
```

**Every handler.** The filter is the one instance already installed on the
last-resort handler, the configured stream and file handlers, and
uvicorn's handlers, so all of them mask. No new installation point.

**Not the log.** `scrub_outbound`, which also shapes HTTP error bodies and
run events, does not change. Nor do the refusal lines a command prints
itself: `cannot connect to the Postgres store at host:port/db`
(`services/api/src/harrier/pgstore.py`) keeps naming the host it is
refusing, as spec 103 meant.

## Failure modes

| Case | Result |
|---|---|
| None of the three variables set | No host masked; records unchanged from today |
| A variable set to a non-Postgres or unparseable value | No host taken from it; the filter does not raise |
| A host that is a substring of a longer name (`db.example.co` inside `db.example.com`) | Not masked: the boundary rule fails |
| A host written in another case than the URL's (`DB.Example.co`) | Masked |
| A host that appears in ordinary text, such as a URL in a source error | Masked: it names the same server |
| A loopback host or a socket directory | Not masked |
| A multi-host URL (`host1,host2`) | Each host masked |
| Two variables naming the same host | Masked once; no doubled marker |
| The host overlaps a password form in the same text | The union of the spans is replaced, as `_mask` does for overlapping password forms |
| The user name appears anywhere as a whole role name | Masked, in any form: `{'user': '<name>'}`, `"user": "<name>"`, `user = '<name>'`, `<name>@host`, `postgresql%3A%2F%2F<name>%3A...`, `for user "<name>"` |
| The user is `postgres` | Not masked anywhere; the host and password still are |
| The user inside a longer role name (`x.<name>`, `<name>x`, `<name>.more`) | Not masked |
| A log call passes the user or a host as an argument of its own, and the format string continues it (`"user=%sx"`) | The argument is masked on its own, as every string argument is scrubbed before the record is rendered: `user=REDACTEDx`. This hides more, never less |
| A logged store URL, `postgresql://postgres.<ref>:...@host/db` | The user, the password and the host are masked: `postgresql://REDACTED:REDACTED@REDACTED/db` |
| `user=` followed by the name and `&`, `;`, `.` or a closing `"` | Masked |
| A host after a written-out escape (`\ndb.example.co`) or a percent escape (`%40db.example.co`) | Masked |
| `user=` followed by a name that none of the three URLs holds | Not masked |
| `user=` followed by a longer name that starts with the URL's user (`user=postgres.abcx` for `postgres.abc`) | Not masked: the name must end where the boundary rule says |
| A URL with no user name | No user masked from it. libpq then connects as the operating system's login name, which the repr may print; see Honest limitations |
| Reading a variable or `.env` raises | The filter's existing catch-all: the record is withheld, not printed raw |
| A variable changes after the first record | The next record uses the new value, as the password forms do today |

## Acceptance criteria

- [ ] A record logged through `logging.lastResort` after
  `scrub_last_resort()`, with psycopg's rollback format and a transaction
  repr naming the host of `HARRIER_DATABASE_URL`, prints no host and keeps
  the port (`services/api/tests/test_logging.py::test_the_last_resort_masks_the_store_host`).
- [ ] The same for a host named by `HARRIER_IMPORT_DATABASE_URL` and by
  `HARRIER_ACCOUNT_DATABASE_URL`, each set alone in the environment
  (`services/api/tests/test_logging.py::test_each_store_url_host_is_masked`).
- [ ] `HARRIER_IMPORT_DATABASE_URL` and `HARRIER_ACCOUNT_DATABASE_URL` set
  only in `.env` are not read; `HARRIER_DATABASE_URL` set only in `.env` is
  (`services/api/tests/test_logging.py::test_the_host_variables_are_read_where_their_specs_read_them`).
- [ ] The same record prints `user=REDACTED` for the user name of each
  of the three URLs, keeps `postgres` when a URL's user is `postgres`, and
  keeps `user=` followed by a name no URL holds
  (`services/api/tests/test_logging.py::test_the_store_user_is_masked_after_user_equals`).
- [ ] A store URL's user is masked in its userinfo, as written and
  percent-encoded, and after `user=` before `&`, `;`, `.` and inside
  double quotes
  (`services/api/tests/test_logging.py::test_the_store_user_is_masked_in_a_url_and_at_every_ending`).
- [ ] A store's user is masked as a whole role name in a dict repr, a
  JSON value, libpq's keyword form with spaces, an escaped double quote,
  `<name>@host` without a scheme, a percent-encoded URL and libpq's
  `for user "<name>"`, and is kept inside a longer role name
  (`services/api/tests/test_logging.py::test_the_store_user_is_masked_wherever_it_stands_as_a_role_name`).
- [ ] A non-ASCII user and host are masked as `json.dumps` and `repr`
  write them
  (`services/api/tests/test_logging.py::test_a_non_ascii_user_and_host_are_masked_as_json_and_repr_write_them`).
- [ ] A host after a written-out backslash escape or a `%40` escape is
  masked
  (`services/api/tests/test_logging.py::test_a_host_after_an_escape_is_masked`).
- [ ] A configured stream handler masks the same record
  (`services/api/tests/test_logging.py::test_a_configured_handler_masks_the_store_host`).
- [ ] `db.example.co` is not masked inside `db.example.com`, is masked in
  upper case, and `localhost` is never masked
  (`services/api/tests/test_logging.py::test_the_host_mask_keeps_its_boundaries`).
- [ ] Every host of a multi-host URL is masked
  (`services/api/tests/test_logging.py::test_every_host_of_a_multi_host_url_is_masked`).
- [ ] A record logged before psycopg is loaded masks every host of a
  multi-host URL, as libpq reads it
  (`services/api/tests/test_logging.py::test_a_store_url_is_read_as_libpq_reads_it_before_the_driver_loads`).
- [ ] A URL libpq cannot read has its `urlsplit` host and user masked
  (`services/api/tests/test_logging.py::test_a_url_libpq_cannot_read_is_read_with_urlsplit`).
- [ ] A record whose format string does not fit its arguments keeps the
  format string with the host masked
  (`services/api/tests/test_logging.py::test_a_format_that_does_not_fit_its_arguments_masks_the_host`).
- [ ] `query cancellation failed: %s` with a psycopg `OperationalError`
  whose text names the host still prints `OperationalError` alone
  (`services/api/tests/test_logging.py::test_a_cancel_failure_prints_no_host`).
- [ ] `scrub_outbound` returns a text naming the host unchanged
  (`services/api/tests/test_logging.py::test_scrub_outbound_keeps_the_host`).
- [ ] `uv run ruff check`, `uv run pyright` and `just check` pass.

## Proof / origin

- The finding: the privacy review of PR #349 (spec 144).
- The leaking format strings: `psycopg/transaction.py`,
  `psycopg/connection.py`, `psycopg/_pipeline.py`,
  `psycopg/_pipeline_base.py`; the repr: `psycopg/pq/misc.py`,
  `connection_summary`. psycopg 3.3.6, libpq 18.
- Why the cancel route does not leak: `_withhold_driver_arguments` in
  `services/api/src/harrier/logredact.py` and `driver_error_text` in
  `services/api/src/harrier/connection.py` (spec 135).
- The promise this keeps: spec 144, its Output section and its criterion
  that no output line names the host.
- Where `harrier store` skips logging setup: `main` in
  `services/api/src/harrier_cli/main.py`.

## Out of scope

- The database name in psycopg's repr. On Supabase it is `postgres`,
  which names no project.
- HTTP error bodies, run events and `ConfigOut.error`, which use
  `scrub_outbound` and show the host on purpose.
- Refusal lines a command prints itself, outside logging.
- The resolved IP address libpq prints in a connect failure. Spec 135
  already withholds that text whole; nothing else prints it.
- Masking the password of `HARRIER_IMPORT_DATABASE_URL` or
  `HARRIER_ACCOUNT_DATABASE_URL` by its forms, as `store_password_forms`
  does for `HARRIER_DATABASE_URL`. Noticed while writing this spec. It is
  spec 141's and spec 144's to decide, and is noted on PR #349, not fixed
  here.
- Changing how harrier's migration code uses psycopg's `transaction()`.

## Decisions

1. **The user name is masked, only after `user=`.** Asked as an open
   decision in the first draft. Akin chose this option on 2026-10-15 over
   masking the host alone.

## Honest limitations

- The rule masks the hosts and user names of the three variables only. A
  host or user the process learns any other way is not masked.
- A URL with no user name connects as the operating system's login name.
  psycopg's repr prints it when it differs from the database name, and
  this spec does not mask it. Hosted URLs always carry a user name.
- The route found is narrow: a failed ROLLBACK on a connection libpq still
  reports as OK. No test drives it against a real server; the tests build
  the repr with `connection_summary` on a stub connection.

## Amendment (2026-10-15): spec 133's log assertions

Found while implementing. Three assertions in
`services/api/tests/test_store_password_scrub.py` read a log record's text
for the URL with its password masked and its host kept,
`postgresql://harrier:REDACTED@db.example.test`. That host is a store
URL's host, so this spec masks it. With the user masked too, by the
amendment below, they now read
`postgresql://REDACTED:REDACTED@REDACTED:5432/tracker`, and assert the
host appears nowhere in the record, its exception and stack text included.
They still prove what they proved, and prove this spec on the root handler
and the log file as well. The absence check was added after the local
review of PR #360 showed that a masked copy of the URL being present did
not rule out an unmasked one in the exception text.

`test_a_record_that_cannot_be_scrubbed_is_withheld` replaced
`scrub_outbound` with a function that raises, to make the filter fail. The
filter now calls `scrub_logged`, which applies `scrub_outbound`'s masking
and this spec's, so the test replaces that instead. Its assertions do not
change.

The assertions on HTTP error bodies and run events keep the host, as
Behavior says, and pass unchanged.

A store URL is read as libpq reads it whenever psycopg is installed, the
driver imported if it is not yet loaded, as `url_parts` already does for
the password forms. A first draft read it only when the driver was already
loaded and cached the `urlsplit` reading otherwise, so a record logged
before the driver loaded left every host after the first of a multi-host
URL unmasked for the rest of the process (privacy review of PR #360).

## Amendment (2026-10-15): the user in a URL, more endings, escapes

The local privacy review of PR #360 found three ways the approved text let
a store's identity through. Akin chose on 2026-10-15 to close all three:

1. **The user in a URL.** Masking the user only after `user=` left it in a
   logged URL, `postgresql://postgres.<ref>:REDACTED@REDACTED/...`. On
   Supabase's shared pooler the host is regional, so there the user is the
   only part that names the project. It is now masked in a URL's userinfo
   as well.
2. **More endings after `user=`.** The first text ended a bare name only
   at whitespace, `)`, `,`, a closing `'` or the end of the text, so
   `?user=<name>&sslmode=require`, `user=<name>;`, `user=<name>.` and
   `user="<name>"` printed it. A bare name now ends at any character that
   cannot continue a role name, and double quotes are read as single ones
   are.
3. **A host after an escape.** The boundary rule read the letter or digit
   that ends `\n` or `%40` as part of a longer host name, so a host after
   either printed. Both now count as a boundary.

The spec 133 log assertions read the URL with the user kept; they now
read `postgresql://REDACTED:REDACTED@REDACTED:5432/tracker`.

A local privacy review of the first build of this amendment found two
places where the code did less than the text above says. The user was
masked as libpq reads it and as `quote` encodes it, but not as the URL
writes it, so a URL encoding only some characters, as
`encodeURIComponent` does, printed its own user. And only a backslash
followed by one letter counted as an escape, so a host after `\x1b`,
`\xa0`, `\u200b` or a JSON `\u00a0` printed. Both are fixed: the user as
written is masked, and `\xHH`, `\uHHHH` and `\UHHHHHHHH` count as
escapes.

## Amendment (2026-10-15): the user wherever it stands

A local privacy review of the amendment above found the user still
printed in shapes the "only after `user=` or in userinfo" rule left out:
a percent-encoded URL, a dict or JSON value, libpq's keyword form with
spaces, an escaped double quote, `<name>@host` without a scheme, and
libpq's own `password authentication failed for user "<name>"` when that
text is copied outside a driver error. Akin chose on 2026-10-15 to mask
the user wherever it stands as a whole role name, with the host's
boundaries and escapes, except the plain role `postgres`. Behavior above
states the rule. It replaces the two places of the amendment above.

## Migration

None.
