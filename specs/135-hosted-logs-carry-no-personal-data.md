---
spec: 135
title: Hosted logs carry no personal data, and the hosted start opens no SQLite
status: accepted
approved: yes
milestone: M10
depends: [029, 045, 112, 133, 134]
---

# Spec 135: Hosted logs carry no personal data, and the hosted start opens no SQLite

Spec 134 lifts the spec 103 gate, so the hosted API (`HARRIER_AUTH=supabase`
and a Postgres `HARRIER_DATABASE_URL`) serves. Its Out of scope leaves
"Identity values in hosted logs" to a later spec. This is that spec. The
local privacy review of PR #301 (spec 134's pull request) found the two
gaps below. Both run in production once PR #301 merges.

## Problem

### The hosted start opens a local tracker it never uses

`create_app` (`services/api/src/harrier_api/app.py`) calls
`configure_logging()` (`services/api/src/harrier/logsetup.py`) in both
modes. Its `_load_identity_values()` opens the local SQLite store with
`harrier.db.connect()` under `HARRIER_DATA_DIR`. Hosted, there is no local
tracker. So each hosted start:

- runs `check_database_ownership` and creates `harrier.db`, migrated, in
  the data directory (`harrier.db.connect` creates the file and applies
  the schema);
- reads identity values from that empty file and installs an
  `IdentityRedactionFilter` holding none, so the log says nothing about
  redaction being absent while none happens;
- opens `logs/harrier.log` on the machine's disk, which nothing reads.

The identity values that matter hosted are in the owners' rows on Postgres,
under spec 105's policy. The API's login role holds no grant of its own
and may not bypass the policy (spec 134, The login role), so it cannot
read every owner's identity values at start, and should not.

### A driver error's text reaches the log stream

The seam raises `StoreDatabaseError` from a driver error with
`raise mapped from error` (`services/api/src/harrier/connection.py`,
`_raise_translated`). The store error's own text carries no row value:
on Postgres it is the server's primary message, a data exception's value
withheld (`store_error_from_postgres`). The chained psycopg error is
different. `str()` of a psycopg error includes the server's `DETAIL`
lines, which quote the row: `Key (email)=(...) already exists`, `Failing
row contains (...)`.

A route's unhandled exception is logged by uvicorn on `uvicorn.error`
with the traceback. `SecretScrubFilter` (`services/api/src/harrier/logredact.py`,
spec 133) formats that traceback with `logging.Formatter().formatException`,
which prints every exception in the chain by its `str()`, and then masks
only the store password. Nothing hosted redacts row values: there is no
identity filter (above), and an identity filter would miss every row
value that is not an identity value anyway.

So today a hosted request whose statement Postgres refuses, on a path no
route catches, writes the refused row's values, personal data included,
to the platform's log stream.

## Scope

1. **Hosted logging opens nothing.** `configure_logging` takes a `hosted`
   flag. `create_app` passes it when hosted mode is configured. With it,
   logging opens no database, loads no identity values, installs no
   identity filter and writes no log file.
2. **A driver error's text never reaches a log record.** The filter that
   already formats every record's exception text (`SecretScrubFilter`,
   spec 133) renders a driver error without its text. The same filter
   renders a driver error passed as a log call's argument without its
   text. The seam supplies the rendering, since only
   `harrier.connection` and `harrier.pgstore` may import psycopg (the
   import contract in `services/api/pyproject.toml`, spec 128).
3. **Proof that the routes which touch contacts log no identity value
   hosted** (Acceptance criteria).
4. **Documents.** `docs/privacy-plan.md` and `docs/architecture.md`'s
   hosted section say what a hosted log carries and name this spec's
   tests.

Classification: every file this spec changes sits under
`services/api/src/`, `services/api/tests/`, `docs/` or `specs/`, which
`config/data-classification.json` classifies as public. No new directory.

## Behavior

### What a hosted log record may carry

| Kind | Hosted log | How it is kept out |
|---|---|---|
| The store password | never | spec 133's scrub, unchanged |
| An identity value (candidate or contact) | never | no row value reaches a log (next two rows); no value filter hosted |
| A row value quoted by a driver error | never | the driver error's text is withheld (below) |
| A row value in a harrier log call | never, on the routes this spec's test drives | the test (Acceptance criteria) |
| Method, path, status, a job id, a track slug, a selector | yes | not personal: a path names a job, never a person (Honest limitations) |
| An exception's class, frames and the seam's store-error text | yes | the store error's text carries no row value (spec 112) |
| A Postgres SQLSTATE and the names of a table, column or constraint | yes | names, not values |

### The hosted start

`create_app` hosted calls `configure_logging(hosted=True)`. That call:

- installs the stderr handler with the secret scrub filter, as today;
- does not call `_load_identity_values`, so `harrier.db.connect` is not
  called and no ownership check runs;
- opens no `RotatingFileHandler` and creates no `logs` directory;
- installs no `IdentityRedactionFilter` and registers none;
- logs one line at INFO: `hosted: identity values are not loaded, and
  driver errors are logged without their text (spec 135)`.

So a hosted start leaves the data directory as it found it, as far as
logging goes. The platform collects stderr (`Dockerfile`, `CMD`), which
is the only hosted log.

The local start is unchanged: the CLI and the local API call
`configure_logging()` without the flag and get spec 029's and spec 045's
behavior, the file and the identity filter included.

### A driver error in a log record

A driver error is an exception raised by psycopg or by `sqlite3`. The
seam answers whether an exception is one, and renders one, in
`services/api/src/harrier/connection.py`.

When a record's exception text is formatted, every driver error anywhere
in the exception's chain (`__cause__`, `__context__`, and the members of
an `ExceptionGroup`) is printed as one line:

```
<module>.<class>: [<SQLSTATE>] <the seam's text for it>
```

The seam's text is what `store_error_from_postgres` (or its SQLite
counterpart) gives the store error: the server's primary message, a data
exception's value withheld, or the class name when the server sent no
message. `[<SQLSTATE>]` is omitted for SQLite and when the error has
none. The `DETAIL`, `HINT`, `CONTEXT` and `WHERE` lines and the client's
own text are never printed. Every frame of the traceback is printed as
today, and so is every exception in the chain that is not a driver
error.

When a log call passes a driver error as one of its arguments
(`logger.warning("failed: %s", error)`), the argument is rendered as the
same line's text, without the class prefix.

This is the same filter, on the same handlers, as spec 133's scrub: every
root handler `configure_logging` creates, the handlers of `uvicorn` and
`uvicorn.access`, and the CLI's last-resort handler. So it holds in both
modes, on both stores, and in the CLI (Decision 2). It runs before
the password scrub, and the password scrub runs on its output.

### Identity values hosted

The hosted process holds no list of identity values and redacts none by
value. What keeps them out is that no row value reaches a log record:

- a driver error's text is withheld (above);
- the hosted path's own log calls carry a method, a path, a reason name,
  the names of the values that differed, or a check name
  (`services/api/src/harrier_api/hostedauth.py`,
  `services/api/src/harrier_api/hostedstore.py`), never a claim value, a
  row value or a body;
- the domain's log calls on the routes that read or write a contact row
  carry no identity value, which a test proves by driving them with
  marker values at `DEBUG` (Acceptance criteria).

## Failure modes

- **A statement fails on a row and no route catches it.** uvicorn logs the
  traceback; the chained psycopg error is one line of class, SQLSTATE and
  primary message; no `DETAIL` line, no row value.
- **A route catches the store error and logs it with `logger.exception`.**
  The same rendering: the chain is formatted by the same filter.
- **A driver error raised inside an `except` block that handles another
  driver error.** Both are in the chain (`__context__`); both are
  rendered without their text.
- **A driver error inside an `ExceptionGroup`.** anyio and Starlette can
  wrap a route's exception in a group. Every member is rendered as above.
- **A data exception whose primary message quotes the value**
  (`invalid input syntax for type integer: "..."`). The value is withheld,
  as `store_error_from_postgres` withholds it today.
- **A record whose `exc_text` another filter or handler already set.**
  The filter formats `exc_text` only when it is unset, as spec 133's does.
  A handler that set `exc_text` before this filter ran is not one
  harrier installs; it is named in Honest limitations.
- **The seam cannot render an error** (an attribute missing on an
  unexpected driver error class). The error is printed by its class name
  alone. The rendering never raises: a filter that raises drops the
  record, and losing the record of a failure is worse than a short one.
- **`HARRIER_DATA_DIR` unset hosted.** Nothing in logging reads it, so
  nothing changes.
- **`HARRIER_DATA_DIR` read-only hosted.** Logging writes nothing there,
  so the start does not warn `file logging is unavailable`.
- **`configure_logging` is called twice in one process.** Idempotent as
  today. A hosted call after a local one, or the reverse, with
  `force=True` replaces the handlers, and the identity filter registered
  by the local call is forgotten.

## Acceptance criteria

The hosted start (Postgres tests, run in CI as spec 103 requires):

- [ ] A hosted `create_app` with a complete configuration, a store at
      `target_version()` and `HARRIER_DATA_DIR` set to an empty temporary
      directory leaves that directory with no `harrier.db`, no
      `harrier.db-wal`, no `harrier.db-shm` and no `logs` directory
      (`services/api/tests/test_hosted_logs.py::test_a_hosted_start_opens_no_local_store`).
- [ ] After that start, no handler of the root logger is a
      `RotatingFileHandler`, none carries an `IdentityRedactionFilter`,
      every one carries the secret scrub filter, and the INFO line above
      was logged once
      (`services/api/tests/test_hosted_logs.py::test_hosted_logging_writes_stderr_only`).
- [ ] A local `create_app`, and the CLI, still create the log file and
      install the identity filter: the existing tests in
      `services/api/tests/test_logging.py` pass unchanged.

A driver error in a log record:

- [ ] A hosted request whose statement Postgres refuses with a `DETAIL`
      quoting the row, planted in the throwaway database so that no route
      catches the error, leaves none of that row's values in any log
      record of any logger, formatted through the handlers `create_app`
      configured, the `uvicorn.error` record of the unhandled exception
      included; and that record still names the psycopg class, the
      SQLSTATE and the route's frames
      (`services/api/tests/test_hosted_logs.py::test_a_failed_statement_logs_none_of_its_row`).
- [ ] The same chain logged with `logger.exception` from domain code, a
      psycopg error passed as a log argument, a psycopg error in a
      `__context__`, and one inside an `ExceptionGroup` each leave none of
      the row's values in the formatted record
      (`services/api/tests/test_hosted_logs.py::test_every_shape_of_driver_error_is_withheld`).
- [ ] A SQLite driver error whose text quotes a value, chained to a store
      error, leaves that value out of the formatted record
      (`services/api/tests/test_hosted_logs.py::test_a_sqlite_driver_error_is_withheld_too`).
- [ ] A driver error the seam cannot render is printed by its class name,
      and the record is not dropped
      (`services/api/tests/test_hosted_logs.py::test_an_unrenderable_driver_error_keeps_its_record`).

Identity values hosted:

- [ ] On a hosted app over a throwaway store, with the root logger at
      `DEBUG`, one synthetic owner's contact holds marker values, and `POST
      /outreach/{selector}/candidates/approve`, `POST
      /outreach/{selector}/best-contact` and `GET /outreach/contacts` each
      run; no log record of any logger contains a marker
      (`services/api/tests/test_hosted_logs.py::test_contact_routes_log_no_identity_value_hosted`).

Gates:

- [ ] Each new test fails when the code it proves is removed, and the pull
      request records each mutation and its result.
- [ ] `docs/privacy-plan.md` and `docs/architecture.md` say what a hosted
      log carries and name the tests above.
- [ ] `uv run ruff check`, `uv run pyright` and `uv run lint-imports`
      pass, and `just check` passes.

## Honest limitations

- **No value filter hosted.** A future log call on a hosted path that
  interpolates a contact's name is caught by nothing at run time. The
  test covers the routes it drives; a new route that touches contacts
  should be added to it.
- **The access log keeps paths.** uvicorn's access log prints the method,
  the path with its selector, and the status. A selector names a job
  (a tracker row), not a person. It is not personal data under the
  privacy rule ("real company names in job data are fine"), but it does
  show which jobs an owner works on, to whoever reads the platform's log.
- **The client address in the access log** is the platform proxy's,
  because uvicorn trusts forwarded headers only from `127.0.0.1` by
  default. A change to `--forwarded-allow-ips` would put the user's
  address in the log; that is a new change.
- **Driver text is lost for debugging.** A `DETAIL` line is useful when a
  constraint fails. The SQLSTATE and the constraint name stay; the values
  do not. Debugging with values is done against a copy of the store, not
  by reading the production log.
- **A handler harrier does not install** (a platform agent, a library's
  own handler on a logger that does not propagate) does not carry the
  filter. Today the API has none.

## Out of scope

- Identity values in local logs: spec 045, unchanged.
- The store password in logs and HTTP errors: spec 133, unchanged.
- HTTP error bodies: the store error's text carries no row value
  (spec 112), and spec 133 scrubs them.
- Logs of hosted runs: runs are refused hosted until spec 106 (spec 132).
  Spec 106 says what a run's log carries.
- Audit logging of successful requests, request IDs and structured (JSON)
  logs.
- The access log's paths and client address (Honest limitations).
- Other SQLite opens on the hosted start, if any: the file-backed routes
  are spec 107's. The first criterion fails if one exists, and it is then
  recorded there.

## Migration

None for the local product: the CLI and the local API log as before, with
the one difference that a driver error's text is withheld (Decision 2).
Hosted: the next start writes no `harrier.db` and no `logs/harrier.log`.
A file a previous start left on the machine is not removed; a Fly
machine's disk does not survive a deploy.

## Options weighed

- **Break the chain at the seam** (`raise mapped from None`). Keeps the
  text out of every traceback with no filter. Not chosen: spec 112 keeps
  the driver error as `__cause__` for debugging, it would not cover a
  driver error raised outside the seam, and `__context__` would still
  hold it for any code that reads it.
- **Load each owner's identity values per request** and filter by value.
  A query per request, and it covers only that owner's values; a record
  quoting another owner's row (a bug in the policy) would pass. Not
  chosen: keeping row values out of records covers every owner and
  every value, identity or not.
- **Load every owner's identity values at start.** Needs a role that
  bypasses the policy, which spec 134 refuses. Not chosen.
- **Withhold driver text hosted only.** Two renderings of one traceback
  depending on the mode. Not chosen (Decision 2).

## Decisions

Akin took both recommendations in chat on 2026-10-13.

1. **No log file hosted.** Stderr only: the platform collects it, and a
   file on a machine's disk is a second copy nothing reads. Not chosen:
   keeping the rotating file hosted, still without opening SQLite.
2. **Driver text withheld in both modes.** One filter and one rule, in the
   CLI and the local API too: spec 127 lets more commands run on
   Postgres, where the same `DETAIL` lines appear. Not chosen: hosted
   only, keeping SQLite's full text in local logs.

## Proof / origin

- `specs/134-each-hosted-request-runs-as-its-user.md`, Out of scope:
  "Identity values in hosted logs".
- The local privacy review of PR #301.
- `specs/133-the-store-password-stays-out-of-logs-and-http-errors.md`:
  `SecretScrubFilter` and the handlers it is installed on.
- `specs/045-claims-and-gates.md` and `specs/029-run-outcome-visibility.md`:
  the identity filter and the logging setup this spec leaves unchanged
  locally.
- `specs/112-the-tracker-domain-runs-on-either-store.md`: the store
  error's text carries no row value.
- `services/api/src/harrier/logsetup.py` (`configure_logging`,
  `_load_identity_values`), `services/api/src/harrier/logredact.py`
  (`SecretScrubFilter`, `IdentityRedactionFilter`),
  `services/api/src/harrier/connection.py` (`_raise_translated`,
  `store_error_from_postgres`), `services/api/src/harrier/db.py`
  (`connect`), `services/api/src/harrier_api/app.py` (`create_app`).
- psycopg 3, `Error.diag`: `str()` of an error includes the `DETAIL`.
  https://www.psycopg.org/psycopg3/docs/api/errors.html

## Amendment (2026-10-13): prompt logging and query strings

Akin decided in chat on 2026-10-13 that this spec carries two items spec
110 dropped when it gave hosted logs to this spec (spec 110, "Amendment
(2026-10-13): Akin's decisions", decision 2 and Open decision 13, taken).
Spec 110 had specified both, under its "hosted logs", and this spec's text
above carries neither: its Out of scope leaves the access log's paths and
client address as they are. This changes an approved spec by Akin's
decision. The text above is not changed; this section adds to Scope and
Behavior, and narrows the Out of scope line on the access log to the paths
and the client address.

### Scope (amendment)

5. **The hosted start refuses prompt logging.**
6. **A hosted access line carries no query string.**

Classification: both change files under `services/api/src/` and
`services/api/tests/`, which `config/data-classification.json` classifies
as public. No new directory.

### Behavior (amendment)

**Prompt logging.** With any of `AI_DEBUG`, `CODEX_CLI_DEBUG`,
`CLAUDE_CLI_DEBUG`, `OPENAI_DEBUG` or `ANTHROPIC_DEBUG` on, `_log_call`
writes every prompt and output under `data/llm-logs`
(`services/api/src/harrier/llm/providers.py:46-54`). Hosted, that is
tenants' application content on a shared machine's disk. "On" is the rule
`debug_enabled` already applies
(`services/api/src/harrier/llm/config.py:161-169`): the value, stripped and
lowercased, is `1`, `true` or `yes`.

`create_app` hosted refuses to start when any of the five is on, before it
serves anything:

```
error: prompt logging (<variable>) is local only; unset it for the hosted API
```

Exit 1. It names the first variable found on, in the order above, and
never its value. A variable set to anything else (`0`, `false`, empty) is
off and the start goes on. The local API and the CLI are unchanged.

**Query strings.** Hosted, uvicorn's access line prints the method, the
path without `?` and everything after it, the protocol and the status. A
`?` encoded in the path (`%3F`) is part of the path and is kept. The query
is dropped by the hosted logging set up under "The hosted start", on the
`uvicorn.access` handlers this spec's filter already covers, before the
secret scrub runs. Locally the access line is unchanged.

Why: a query string carries what a person typed into a search or a
filter. A path names a route and a job, which this spec's Honest
limitations already accept.

### Failure modes (amendment)

- **A debug variable set on a Fly machine**, in `[env]` or as a secret.
  The hosted start refuses, naming it; the machine never passes its health
  check, and Fly keeps the previous one (spec 110).
- **A debug variable set to `0` or `false` hosted.** Off; the start goes
  on.
- **A request with a query string hosted.** Its access line shows the path
  alone.

### Acceptance criteria (amendment)

- [ ] The hosted start refuses with each of the five variables set to
      `1`, `true` and `yes`, one case each, with the text above naming the
      variable and not its value, exit 1; it starts with each set to `0`
      and to empty; and the local API starts with any of them on
      (planned test_a_hosted_start_refuses_prompt_logging).
- [ ] Hosted, a request to a path with a query string writes an access
      line holding the path and no part of the query; a path holding
      `%3F` keeps it; locally the same request's line keeps its query
      (planned test_a_hosted_access_line_carries_no_query).

### Honest limitations (amendment)

- **Read at start.** A debug variable is read once, when the app is built.
  On Fly a changed variable restarts the machine, so it is read again.
- **Paths stay.** Data a route takes in its path, rather than its query,
  is still printed, as this spec's Honest limitations say of selectors.

## Amendment (2026-10-13): what building it settled

Building this spec found two places in the identity criterion where the
text was wrong, and settled one mechanism the text left open. Each is
stated here so the code and the spec agree.

- **`best-contact` is a `POST`.** The criterion said `GET
  /outreach/{selector}/best-contact`. The route sets the best contact and
  is `POST` (`services/api/src/harrier_api/outreach_routes.py`,
  `set_best_contact`). The criterion above now says `POST`.
- **The candidate profile is not seeded.** The criterion seeded marker
  values in the owner's candidate profile too. None of the three routes
  reads the profile, so a marker there proves nothing about them. The
  criterion now seeds the contact only, staged as discovery stages it and
  stored by the approve route, and the test checks that the marker reached
  the routes (`GET /outreach/contacts` returns it) before it checks the
  log.
- **How a driver error's text is withheld.** `traceback.TracebackException`
  builds its tree of a chain in step with the exceptions themselves:
  `__cause__`, `__context__` and a group's members. `SecretScrubFilter`
  walks the two together and replaces only a driver error's printed text,
  so its type and frames stay, and so does every other exception's text
  (`services/api/src/harrier/logredact.py`, `format_exception_withheld`).
  The text it replaces is a private attribute of `traceback`
  (`_str`). On a Python that keeps it elsewhere the filter withholds the
  whole record rather than print the driver's text; the tests above fail
  there, so the change is seen.
- **What tells a driver error.** `harrier.connection.is_driver_error` reads
  psycopg's error class from `sys.modules` and never imports it: a process
  that never loaded psycopg holds none of its errors, and the import
  contract keeps psycopg inside the seam.

Each test fails when the code it proves is removed. The mutations, run
against a local Postgres 17:

| Mutation | Fails |
|---|---|
| `create_app` calls `configure_logging()` without `hosted` | `test_a_hosted_start_opens_no_local_store`, `test_hosted_logging_writes_stderr_only` |
| the hosted branch leaves earlier identity filters registered | `test_hosted_logging_writes_stderr_only` |
| the filter formats the traceback with `str()` of each exception | `test_a_failed_statement_logs_none_of_its_row`, three shapes of `test_every_shape_of_driver_error_is_withheld`, `test_a_sqlite_driver_error_is_withheld_too`, `test_an_unrenderable_driver_error_keeps_its_record` |
| driver errors passed as arguments are not withheld | three shapes of `test_every_shape_of_driver_error_is_withheld`, `test_an_unrenderable_driver_error_keeps_its_record` |
| the walk skips `__context__` | the `__context__` shape |
| the walk skips a group's members | the `ExceptionGroup` shape |
| the seam's rendering may raise | `test_an_unrenderable_driver_error_keeps_its_record` |
| the approve route logs the contact's name at `DEBUG` | `test_contact_routes_log_no_identity_value_hosted` |
