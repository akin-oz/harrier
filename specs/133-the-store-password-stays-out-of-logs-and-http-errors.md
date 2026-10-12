---
spec: 133
title: The store password stays out of logs and HTTP errors
status: accepted
approved: yes
milestone: M10
depends: [045, 104, 112, 128]
---

# Spec 133: The store password stays out of logs and HTTP errors

Split from spec 104 on 2026-10-12 (spec 104, "Amendment (2026-10-12):
split into hosted mode, run ownership, store password and the request
transaction", decision 1). This is part C. It is spec 104's "Amendment
(2026-10-12): the store password in logs and HTTP errors", moved here
whole, with three gaps the split's analysis found in it (Where the moved
text was short, below). Spec 134 lifts the gate that keeps the API off a
Postgres store, and this spec lands before it. It does not depend on
specs 132 or 134.

The criterion's history: spec 128's privacy review (PR #281) found it.
Spec 112 carried it until its pull request for the seam (PR #288) showed
it could not be built there. Akin placed it in spec 104 in chat on
2026-10-12, and moved it here with the split the same day.

## Problem

Domain code on a Postgres store can raise a driver error whose text quotes
the store URL, password included. The seam's own store errors carry the
server's primary message or the driver error's class, never the client's
text, so their message quotes no URL (spec 112, "Amendment (2026-10-12):
what the seam's pull request settled"). The driver's error stays chained
as `__cause__`, and two sinks in the API process do not run
`scrub_secrets` (`services/api/src/harrier/sources/__init__.py`):

- **The log stream.** `IdentityRedactionFilter`
  (`services/api/src/harrier/logredact.py`) redacts identity values only,
  and a logged traceback prints the chained driver error.
- **HTTP error detail.** The routes in `services/api/src/harrier_api/`
  that answer `HTTPException(detail=str(error))`, and the app's own
  exception handlers that build `ErrorOut(detail=str(error))`.

Today neither sink is reached with a Postgres error, because the API
refuses a Postgres URL (spec 103) and only `harrier store` runs on one
(spec 112, decision 4). Spec 134 lifts the API's refusal, and spec 127
lets more commands run on Postgres. The log filter is shared with the
CLI, so a command spec 127 lets run on Postgres is covered by the same
change.

## Scope

1. **A scrub filter on every log handler of the API and the CLI.** A new
   `logging.Filter`, in `services/api/src/harrier/logredact.py`, passes a
   record's rendered message, its formatted exception text and its stack
   text through `scrub_secrets`. `configure_logging`
   (`services/api/src/harrier/logsetup.py`) installs it on every root
   handler it creates, whether or not the identity values load, and the
   API also installs it on the handlers of uvicorn's `uvicorn` and
   `uvicorn.access` loggers.
2. **One place for HTTP error detail.** An exception handler for
   Starlette's `HTTPException`, registered in `create_app`
   (`services/api/src/harrier_api/app.py`), passes a string `detail`
   through `scrub_secrets` before the body is written. The app's handlers
   that build `ErrorOut(detail=str(error))` (`run_conflict` today) do the
   same. The raise sites are not edited one by one.
3. **Nothing else.** No route, model, status or contract changes. The body
   of every refusal keeps its shape; only text that `scrub_secrets` matches
   changes.

Every file this spec changes sits under `services/api/src/` and
`services/api/tests/`, which `config/data-classification.json` classifies
as public.

## Behavior

### The log stream

A record that reaches any handler of the API process or the CLI is
scrubbed before it is formatted:

- `record.getMessage()`, rewritten into `record.msg` with no arguments
  left, when scrubbing changes it;
- the formatted exception, chained causes included, written into
  `record.exc_text`, which `logging.Formatter` then prints instead of
  formatting `exc_info` again (the mechanism `IdentityRedactionFilter`
  already uses, `logredact.py`, `filter`);
- `record.stack_info`.

The filter is installed before the identity filter on the same handler,
so the identity filter sees scrubbed text. It holds no state and needs no
database, so it is installed even when `_load_identity_values` returns
None or ownership is refused, which are the two paths where no identity
filter is installed today.

### HTTP error detail

An `HTTPException` whose `detail` is a string is answered with that string
after `scrub_secrets`, and with the status and headers it was raised with.
A `detail` that is not a string (FastAPI's validation errors, a model) is
answered unchanged. The same applies to every exception handler
`create_app` registers whose body carries an exception's text.

### Where the moved text was short

Spec 104's amendment named the two sinks. Reading the code for the split
found three ways past them, each closed above:

- **The identity filter does nothing without identity values.**
  `IdentityRedactionFilter.filter` returns at once when its value set is
  empty (`logredact.py`, `if not self._values`), and `configure_logging`
  installs no filter at all when the values cannot be loaded
  (`logsetup.py`, `_install_identity_redaction`). Adding `scrub_secrets`
  inside that filter would scrub nothing on a store with no contacts.
  Scope item 1 is therefore a filter of its own, always installed.
- **uvicorn logs an unhandled exception on its own handler.** uvicorn
  logs "Exception in ASGI application" with the exception to the
  `uvicorn.error` logger (`uvicorn/protocols/http/h11_impl.py`), which
  propagates to `uvicorn`, whose default configuration gives it its own
  handler and `propagate: False` (`uvicorn/config.py`, `LOGGING_CONFIG`).
  A filter on the root handlers never sees that record. Checked in the
  installed uvicorn on 2026-10-12.
- **Not every refusal is an `HTTPException`.** `create_app`'s
  `run_conflict` handler builds `ErrorOut(detail=str(error))` itself.

## Failure modes

- **A route raises an exception the app does not handle, chaining a
  driver error that quotes the URL.** The client gets Starlette's plain
  500 body, which quotes nothing. uvicorn's log record of it is scrubbed.
- **A route raises `HTTPException(detail=str(error))` with a URL in the
  text.** The body's `detail` has the password replaced.
- **A log call passes the URL as an argument.** The rendered message is
  scrubbed, and the record carries no argument holding it.
- **No identity values, or the database is unreadable at start.** The
  scrub filter is installed anyway.
- **A second handler on the same logger.** The record is mutated, as the
  identity filter mutates it, so every handler sees the scrubbed text.
- **A secret in a shape `scrub_secrets` does not match.** It is not
  scrubbed (Honest limitations).

## Acceptance criteria

- [ ] With a password generated at run time in `HARRIER_DATABASE_URL`, an
      error raised through each sink leaves no 8-character window of the
      password in what leaves the process: a log record with the chained
      driver error, through a root handler and through uvicorn's
      `uvicorn.error` logger; an `HTTPException` detail built from the
      error; and the `run_conflict` handler's body
      (`services/api/tests/test_store_password_scrub.py::test_no_log_or_http_error_carries_the_store_password`,
      one case per sink).
- [ ] With no identity values to load, and with the database unreadable,
      a root handler's record is still scrubbed
      (`services/api/tests/test_store_password_scrub.py::test_the_scrub_filter_needs_no_identity_values`).
- [ ] An `HTTPException` with a non-string `detail`, and a 422, are
      answered exactly as before, and a string `detail` with nothing to
      scrub is answered byte for byte as before
      (`services/api/tests/test_store_password_scrub.py::test_a_detail_with_nothing_to_scrub_is_unchanged`).
- [ ] Each new behavior has a test that fails when the code it proves is
      removed, and the pull request records each mutation and its result.
- [ ] `uv run ruff check`, `uv run pyright` and `uv run lint-imports`
      pass, and `just check` passes.

## Honest limitations

- `scrub_secrets` matches shapes: a Postgres URL's password, parameters
  named as secrets, a bot token path. A password that reaches a sink in
  another shape, for instance quoted alone by a driver message, is not
  scrubbed. Open decision 1 closes that for the store URL's own password.
- Only the API process and the CLI are covered. A run subprocess writes
  its own output, which the run manager scrubs at `RunManager._append`
  (spec 035), unchanged here.
- A handler someone adds later without going through `configure_logging`
  is not filtered. The two places that install handlers are named in
  Scope; a third is a new change.
- uvicorn's handlers are filtered after uvicorn configures them, when the
  app is built. A record uvicorn logs before the app is imported, such as
  a failure to import it, is not filtered. The hosted start refusals are
  written so they quote no URL (spec 104, Failure modes).

## Migration

None. Log lines and refusal bodies change only where they held a secret.

## Options weighed

- **Scrub inside `IdentityRedactionFilter`.** One filter fewer. It returns
  early with no identity values and is not installed when they fail to
  load, so it would scrub nothing in exactly the hosted case. Not chosen.
- **Edit each `HTTPException(detail=str(error))` site.** Every site found
  today, and none added tomorrow. The finding is a sample of a shape, not
  a list (review-response rule). Not chosen.
- **Drop the driver error's chain at the seam.** The chain is what an
  operator debugs with, and spec 112 requires it. Not chosen.

## Open decisions for Akin

1. **The store URL's own password, in every form.** Recommended: the
   scrub filter and the detail handler also replace each form of the
   configured URL's password that `harrier.connection.url_parts` already
   lists (`UrlParts.passwords`), when `HARRIER_DATABASE_URL` names a
   Postgres store. The API knows its own password, so no shape needs
   guessing. Alternative: `scrub_secrets` only, as spec 104's amendment
   said, with the limitation above.

## Proof / origin

- `specs/104-the-hosted-api-authenticates-every-request.md`, "Amendment
  (2026-10-12): the store password in logs and HTTP errors" (moved here)
  and "Amendment (2026-10-12): split into hosted mode, run ownership,
  store password and the request transaction".
- `specs/112-the-tracker-domain-runs-on-either-store.md`, "Amendment
  (2026-10-12): what the seam's pull request settled", "The store password
  in logs and HTTP errors".
- `specs/128-three-postgres-store-review-findings.md`: `scrub_secrets` and
  a Postgres URL's password.
- `specs/045-claims-and-gates.md`: the identity filter and its
  installation.
- `services/api/src/harrier/logredact.py` (`IdentityRedactionFilter`),
  `services/api/src/harrier/logsetup.py` (`configure_logging`,
  `_install_identity_redaction`), `services/api/src/harrier/sources/__init__.py`
  (`scrub_secrets`), `services/api/src/harrier_api/app.py` (`create_app`,
  `run_conflict`).
- uvicorn, as installed: `uvicorn/config.py` (`LOGGING_CONFIG`) and
  `uvicorn/protocols/http/h11_impl.py` (the "Exception in ASGI
  application" record).

## Out of scope

- Identity values in hosted logs, which are per owner: a later spec.
- The run subprocess's output: scrubbed by the run manager (spec 035).
- Any change to what a refusal says beyond the scrubbed text.
- The hosted start refusals' texts: spec 104 (configuration) and spec 134
  (the login role and the store version).

## Amendment (2026-10-12): what the implementation settled

Building this spec settled the open decision and four points the text left
implicit or got wrong. Each is stated here so the code and the spec agree.

- **Open decision 1: the recommended answer.** Both sinks pass text through
  `harrier.logredact.scrub_outbound`, which runs `scrub_secrets` and then
  replaces every form `UrlParts.passwords` lists for the configured URL
  with `REDACTED`, the union of their spans at once, the rule
  `harrier.pgstore._scrub` follows. The URL is read when the text is
  scrubbed, as `store_target` reads it: the process environment, then
  `.env` in the working directory. Reading it then rather than when
  logging is configured is what lets the scrub follow a URL set after the
  app is built, and it keeps the filter free of state. Proved by every case
  of `services/api/tests/test_store_password_scrub.py::test_no_log_or_http_error_carries_the_store_password`,
  whose driver error quotes the password alone as well as in the URL, and
  by `services/api/tests/test_store_password_scrub.py::test_a_password_set_only_in_dotenv_is_scrubbed_as_well`.
- **A record's arguments are scrubbed one by one when that is enough.**
  Behavior said the rendered message is written into `record.msg` with no
  arguments left. uvicorn's access formatter unpacks the record's five
  arguments itself (`uvicorn/logging.py`, `AccessFormatter`), and its
  request line carries the query, so a record flattened that way was lost
  as a logging error. The filter now scrubs each string argument and the
  format string, and keeps them when the message they render holds nothing
  left to scrub. Otherwise, as when the secret is in an exception passed as
  an argument, the message is flattened as Behavior says, and uvicorn's
  `color_message` is dropped with the arguments, since its coloured
  formatter renders that instead of `msg`
  (`services/api/tests/test_store_password_scrub.py::test_an_access_line_with_a_secret_in_its_query_keeps_its_shape`,
  `services/api/tests/test_store_password_scrub.py::test_a_coloured_record_scrubbed_whole_is_still_rendered`).
- **A record the filter cannot scrub is withheld.** An exception raised in
  a filter reaches the code that logged. If the scrub itself fails, the
  record's message becomes `log record withheld: it could not be scrubbed`
  and its exception and stack text are dropped
  (`services/api/tests/test_store_password_scrub.py::test_a_record_that_cannot_be_scrubbed_is_withheld`).
- **Every root handler scrubs on its own.** Through a logger, the first
  handler's filter scrubs the record for the rest, so a test through the
  logger could not see a handler installed without the filter. Each root
  handler is also driven alone
  (`services/api/tests/test_store_password_scrub.py::test_every_root_handler_scrubs_a_record_on_its_own`).
- **Where uvicorn's handlers are filtered.** `create_app` calls
  `harrier.logsetup.scrub_uvicorn_handlers`, which installs the same filter
  instance on the handlers of `uvicorn` and `uvicorn.access`, so building
  the app twice installs it once. The uvicorn case of the first criterion
  runs a real uvicorn server on the h11 protocol, whose handlers are built
  as uvicorn's default configuration builds them.
- **Specs 104 and 112 cite the criterion's test as built.** Each named it
  as planned, and the citation check fails on a planned test that exists,
  so `specs/104-the-hosted-api-authenticates-every-request.md` (the moved
  amendment) and `specs/112-the-tracker-domain-runs-on-either-store.md`
  ("The store password in logs and HTTP errors") now cite it by file and
  say spec 133 built it. No other text there changes.

Two further limitations, added to those above:

- Every form of the configured password is masked wherever it appears. A
  very short password would mask unrelated text that holds it. Nothing
  guards against that.
- `.env` is read once per working directory for the scrub, so a `.env`
  edited while the process runs is not seen by it until the process
  restarts. An exported `HARRIER_DATABASE_URL` is read each time.

Out of this spec, and left as it is: two response bodies carry an
exception's text with status 200, `ConfigOut.error`
(`services/api/src/harrier_api/app.py`, `_config_out`) and
`ScheduleOut.error` (`services/api/src/harrier_api/ops_routes.py`). Neither
is an `HTTPException` nor an exception handler's body, and each carries a
configuration parse error, not a store error. Scrubbing them would change
a model's field outside this spec's two sinks.
