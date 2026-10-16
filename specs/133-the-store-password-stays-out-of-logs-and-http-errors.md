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
   `uvicorn.access` loggers. The CLI installs it on logging's last-resort
   handler, which `harrier store` and `harrier doctor` write through
   (Amendment (2026-10-12): the privacy review of PR #296).
2. **One place for HTTP error detail.** An exception handler for
   Starlette's `HTTPException`, registered in `create_app`
   (`services/api/src/harrier_api/app.py`), passes a string `detail`
   through `scrub_secrets` before the body is written. The app's handlers
   that build `ErrorOut(detail=str(error))` (`run_conflict` today) do the
   same. The raise sites are not edited one by one.
3. **An exception's text in a 200 body.** `ConfigOut.error` and
   `ScheduleOut.error` pass through `scrub_outbound` where each route
   builds them (Amendment (2026-10-12): exception text in a 200 body).
4. **Nothing else.** No route, model, status or contract changes. The body
   of every refusal keeps its shape; only text that `scrub_outbound` masks
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
- **A message quotes a non-ASCII password with `ascii()`, `!a` or `%a`.**
  Each non-ASCII character is written as `\xHH`, `\uHHHH` or
  `\UHHHHHHHH`. That form is masked at every sink, as the `repr` form is
  (Amendment (2026-10-15): a password quoted by `ascii()`).

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
- [ ] With a password generated at run time in `HARRIER_DATABASE_URL`, a
      200 body whose `error` field quotes the store URL, or the password
      alone as the URL writes it, masks the password: `ConfigOut.error`
      from a stored row and from a config file, and `ScheduleOut.error`,
      each driven through its route
      (`services/api/tests/test_store_password_scrub.py::test_an_error_field_in_a_200_body_has_the_store_password_masked`,
      one case per field and shape). With nothing to mask, each field
      reads exactly as the parser wrote it
      (`services/api/tests/test_store_password_scrub.py::test_an_error_field_with_nothing_to_mask_is_unchanged`).
- [ ] With a password generated at run time, a message that quotes the
      password alone with `repr` or with `json.dumps` leaves no
      8-character window of it through any of the four sinks of the first
      criterion. One password holds both `'` and `"`; the other holds `'`,
      a backslash, a control character and a non-ASCII character
      (`services/api/tests/test_store_password_scrub.py::test_a_password_quoted_by_repr_or_json_is_masked_at_every_sink`,
      one case per sink, quoting and password; Amendment (2026-10-12): a
      password quoted by `repr` or `json.dumps`).
- [ ] With a password generated at run time that holds `'`, a backslash
      and one non-ASCII character of each escape width (`\xHH`, `\uHHHH`,
      `\UHHHHHHHH`), a message that quotes the password alone with
      `ascii()`, with `%a`, and inside a dict with `!a` leaves no
      8-character window of it through any of the four sinks of the first
      criterion. The same password holding both `'` and `"` is a second
      case, so both readings of `'` are proved
      (`services/api/tests/test_store_password_scrub.py::test_a_password_quoted_by_ascii_is_masked_at_every_sink`,
      one case per sink, quoting and password; Amendment (2026-10-15): a
      password quoted by `ascii()`).
- [ ] With spec 145's user and host forms in place, a log record that
      quotes a store's non-ASCII user and non-ASCII host with `ascii()`
      or `%a` prints neither; `scrub_outbound` still keeps the host
      (`services/api/tests/test_logging.py::test_a_non_ascii_user_and_host_are_masked_as_ascii_writes_them`;
      Amendment (2026-10-15): a password quoted by `ascii()`).
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
  (spec 035), with `scrub_outbound` since the privacy review of PR #296.
  Text a run writes to its own files, such as a discovery summary's
  source errors (`services/api/src/harrier/discovery.py`), is scrubbed
  where it is written, with `scrub_secrets` alone. Those carry a job
  source's errors, not the store's.
- A handler someone adds later without going through `configure_logging`
  is not filtered. The three places that install the filter are named in
  Scope; a fourth is a new change.
- A password quoted once by `repr`, `json.dumps` or `ascii()` is masked
  (Amendment (2026-10-12): a password quoted by `repr` or `json.dumps`;
  Amendment (2026-10-15): a password quoted by `ascii()`). A password
  quoted twice, such as `%r` of an exception whose message already quotes
  it with `!r`, is escaped twice, and that text is not masked. Nor is a
  password quoted by `repr` of its bytes when it holds a non-ASCII
  character, a password `reprlib` truncates, or any other encoding
  (base64, a hex dump, an HTML entity).
- A value the operator stored is returned as data, unmasked, wherever a
  route returns it: `ConfigOut.value`, the import report
  (`ConfigImportOut.report`, which names a dropped feed and the one kept),
  and a schedule record's `last_success_at` and `summary`. Masking `value`
  would make a PUT round trip store `REDACTED`, and masking the other two
  would hide what `ConfigOut.value` shows anyway. This spec covers error
  text, not the operator's own values (review of PR #298).
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
- The run subprocess's own files: scrubbed where they are written (Honest
  limitations).
- Any change to what a refusal says beyond the scrubbed text.
- The hosted start refusals' texts: spec 104 (configuration) and spec 134
  (the login role and the store version).

## Amendment (2026-10-12): what the implementation settled

Building this spec settled the open decision and four points the text left
implicit or got wrong. Each is stated here so the code and the spec agree.

- **Open decision 1: the recommended answer.** Both sinks pass text through
  `harrier.logredact.scrub_outbound`, which replaces every form
  `UrlParts.passwords` lists for the configured URL with `REDACTED`, the
  union of their spans at once, the rule `harrier.pgstore._scrub` follows,
  and then runs `scrub_secrets` (in that order since the privacy review of
  PR #296, below). The URL is read when the text is
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
a model's field outside this spec's two sinks. (Superseded by
Amendment (2026-10-12): exception text in a 200 body.)

## Amendment (2026-10-12): the privacy review of PR #296

A local privacy review of PR #296 found two leaks in what this spec built,
both reproduced before the fix, and two places the spec's own wording
covered that the code did not. Scope and Honest limitations above now say
what the code does.

- **The scrub order left a password's head.** `scrub_outbound` ran
  `scrub_secrets` first. A password holding a `name=value` shape, such as
  one with `Passphrase=` in it, had its tail rewritten as a parameter's
  value, and the password's forms no longer matched what was left, so the
  head leaked through the log and the HTTP detail. The forms are now
  masked first, on the text as written. The password every test here
  generates holds `pass=`, `token=` and `secret=`, and `&`, `?`, `%`, both
  quotes and a space, percent-encoded in a URL `store_target` accepts, so
  every case of
  `services/api/tests/test_store_password_scrub.py::test_no_log_or_http_error_carries_the_store_password`
  fails with the old order.
- **A log call whose arguments did not fit its format printed them raw.**
  The filter returned early when the message failed to render, and
  logging's error report then wrote the record's arguments to stderr,
  the URL among them. Such a record now keeps its format string, scrubbed,
  followed by `(arguments withheld: they do not fit the format)`, and no
  arguments (`services/api/tests/test_store_password_scrub.py::test_a_log_call_whose_arguments_do_not_fit_leaks_nothing`).
  `IdentityRedactionFilter` has the same early return. It is not changed:
  on every handler it is installed on, this filter runs first and leaves a
  record that renders, so the early return is no longer reached with
  arguments in it.
- **`harrier store` and `harrier doctor` wrote through an unfiltered
  handler.** Both skip `configure_logging`, so their warnings, the
  driver's own included, went to logging's last-resort handler. The CLI
  now installs the filter there before any command runs
  (`harrier.logsetup.scrub_last_resort`;
  `services/api/tests/test_store_password_scrub.py::test_a_command_that_skips_logging_setup_still_scrubs_its_warnings`).
- **A run's output was scrubbed with `scrub_secrets` alone.** The run
  manager's scrub of every event (`harrier_api.runs.scrub_event_data`) now
  uses `scrub_outbound`, so a password a run prints outside a URL is masked
  once runs open a Postgres store (spec 127)
  (`services/api/tests/test_store_password_scrub.py::test_a_run_event_has_the_store_password_masked`).

## Amendment (2026-10-12): exception text in a 200 body

Akin asked in chat on 2026-10-12 for the two fields the first amendment
left out to be covered here. The property this spec owns is that the store
password does not leave the process. Two fields carry an exception's text
out of it in a 200 body, which neither sink sees:

- `ConfigOut.error` (`services/api/src/harrier_api/app.py`, `_config_out`),
  from a `ConfigError` on a stored row or on the file fallback (spec 073).
- `ScheduleOut.error` (`services/api/src/harrier_api/ops_routes.py`,
  `get_schedule`), from a `ScheduleConfigError` (specs 050 and 120).

A search of `services/api/src/harrier_api` for an exception's text in a
response finds no third. Every other `str(error)` there is an
`HTTPException` detail, a run event that `RunManager._append` scrubs, or a
`ValueError` re-raised inside `RunParams`.

**What reaches these fields.** Not a driver error. Both `except` clauses
catch only the parser's own error, and a driver error raised by the read
around them reaches the app's 500 path, which this spec already scrubs.
What reaches them is the parser's message, and it quotes the value it
refused, mostly with `!r`: a stored or file hold entry, a hold date, a
schedule label. A schedule job's name is quoted plain, and a JSON decode
error quotes only the decoder's position. A value an operator pasted the
store URL into is quoted whole. `scrub_secrets` masks a URL's password,
but not the password alone outside the URL shapes it knows, which only
the configured URL's forms mask.

**Behavior.** Each route passes the text through `scrub_outbound` when it
builds the field, as `run_conflict` does for `ErrorOut`. The field keeps
its type, its nullability and its position in the contract; only text
`scrub_outbound` masks changes. A refusal with nothing to mask reads
exactly as before
(`services/api/tests/test_store_password_scrub.py::test_an_error_field_with_nothing_to_mask_is_unchanged`).

**Why here, not in the specs that own the fields.** Spec 073 owns
`ConfigOut.error` and specs 050 and 120 own `ScheduleOut.error`. Each of
them settles what the field says; none settles what must never leave the
process, and splitting this across them would put one rule in three
places. The change is the same call this spec already makes at its other
sinks.

**Why the routes, not one place.** Each field is built at one site, in a
200 path no handler intercepts. A validator on the models would also
scrub, but it would hide a privacy rule inside a response model that
otherwise only declares a shape.

**Proof.** `services/api/tests/test_store_password_scrub.py::test_an_error_field_in_a_200_body_has_the_store_password_masked`
plants a value where each route reads it: a stored `company_holds` row
with an unknown key, a holds CSV whose date is the value, and a schedule
job named by the value with no command. The value is the store URL, with a
password made at run time, or that password alone as the URL writes it.
It drives each route as a client does and finds no 8-character window of
either form of the password in the field or the body. Removing the scrub
at one site fails that site's two cases and no other; scrubbing with
`scrub_secrets` alone, or leaving `HARRIER_DATABASE_URL` unset, fails
every case of the password alone.

**Limitations found.** Building this, and the local review of PR #298,
showed two (Honest limitations). A decoded password quoted alone with
`!r` is not masked when `repr` escapes one of its characters: recorded,
not fixed here, because masking an escaped form changes `scrub_outbound`
for every sink. A value the operator stored is returned as data in other
fields: recorded, because this spec covers error text and `value` cannot
be masked without breaking a round trip.

## Amendment (2026-10-12): a password quoted by `repr` or `json.dumps`

The amendment above found a gap in `scrub_outbound` and left it open. Akin
asked in chat on 2026-10-12 for it to be closed here, since it is the
property this spec owns and it reaches every sink.

**The gap.** `scrub_outbound` masks the forms `UrlParts.passwords` lists:
the password as written, percent-decoded, and as libpq reads it. A message
that quotes the password alone with `repr` writes `\` as `\\`, a control
or other non-printable character as an escape, and `'` as `\'` when the
quoted text holds both quote kinds. `json.dumps` writes `"` as `\"`, `\`
as `\\`, a control character as an escape and, by default, non-ASCII as
`\uXXXX`. The escaped text is none of the forms, so the whole password
leaves the process. Reproduced before this amendment with
`scrub_outbound(repr({'until': password}))` and with `json.dumps` of the
same value, for a password holding both quote kinds. The configuration
and schedule refusals quote the value they refused with `!r`
(`harrier.userconfig.store`, `ConfigError`; `harrier.schedule`,
`ScheduleConfigError`), as does any log call that uses `%r`. A URL keeps
its password percent-encoded, which neither function changes, so a quoted
URL was already masked.

**Behavior.** `harrier.logredact.store_password_forms` lists, for each
form `UrlParts.passwords` lists, the form as written and four quoted
forms of it, each without the quotes around it:

- as `repr` writes it with `'` escaped, which is how it appears inside a
  quoted text holding both quote kinds;
- as `repr` writes it with `'` left as it is, which is how it appears
  inside a text that does not hold both;
- as `json.dumps` writes it, non-ASCII as `\uXXXX`;
- as `json.dumps` writes it with `ensure_ascii=False`, non-ASCII left as
  it is.

A quoted form equal to one already listed is listed once. Every sink
already passes its text through `scrub_outbound`, which masks the union of
every listed form's spans, so no sink changes. The forms are computed
once per URL, as the forms are today.

**Failure modes.** A password with nothing `repr` or `json.dumps` escapes
has no new form, and scrubbing is as before. Computing a form cannot
raise on the logging path: both functions accept every string.

**Proof.** `services/api/tests/test_store_password_scrub.py::test_a_password_quoted_by_repr_or_json_is_masked_at_every_sink`
runs each of the four sinks of the first criterion with a driver error
that quotes a password alone with `repr`, inside a dict as a `!r` message
quotes it, with `json.dumps`, and inside a dict with `json.dumps` and
`ensure_ascii=False`, as Starlette writes a JSON body. Two passwords, each
made at run time: the `store_password` fixture's, which holds both quote
kinds, and one that holds `'`, a backslash, a control character and a
non-ASCII character and no `"`, so `repr` leaves its `'` as it is. Each
case finds no 8-character window of the password in what the sink wrote.
Removing any one of the four quoted forms fails at least one case
(the local privacy review of PR #299 found the first version of the test
proved only two of them).

**Limitation.** One level of quoting. A password quoted twice, such as
`%r` of an exception whose message already quotes it with `!r`, has `\`
escaped again, and that text is not masked (Honest limitations).

## Amendment (2026-10-15): a password quoted by `ascii()`

The amendment above masked a password as `repr` and `json.dumps` quote
it, and left `ascii()` in Honest limitations. CodeRabbit's review of PR
#360 (spec 145) raised the JSON form of a non-ASCII user name, and the
`ascii()` form was found while fixing that. Spec 145 reuses this spec's
quoted forms for the store's user and host, so it leaks the same way. This
amendment closes it here, where the quoted forms are specified.

**The gap.** `ascii()`, the `!a` conversion and the `%a` format write a
string as `repr` does, except that every non-ASCII character becomes an
escape: `\xHH` below U+0100, `\uHHHH` below U+10000, `\UHHHHHHHH` above.
`json.dumps` writes the first kind as `\u00HH` and the third as a
surrogate pair, so none of the listed forms matches. Reproduced on
2026-10-15 on PR #360's head, with `HARRIER_DATABASE_URL` naming a user,
a password and a host that each hold a non-ASCII character:
`scrub_logged` masked each value as written and as `repr` writes it, and
left each one whole as `ascii()` and `%a` write it.

**Behavior.** For each form `UrlParts.passwords` lists,
`harrier.logredact.store_password_forms` also lists two forms as
`ascii()` writes it, each without the quotes around it:

- with `'` escaped, as it appears inside a quoted text holding both quote
  kinds;
- with `'` left as it is, as it appears inside a text that does not hold
  both.

For a form with no non-ASCII character these equal the two `repr` forms
and are listed once, as any repeated form is. Every sink already passes
its text through `scrub_outbound`, so no sink changes. The forms are
computed once per URL, as the forms are today.

Spec 145's user and host forms are built by the same helper, so after
this amendment `scrub_logged` masks a store's user and host as `ascii()`
writes them as well. A host is matched in any case, as spec 145 matches
it, so `H\xf6st` matches `h\xf6st`. `scrub_outbound` still keeps the
host (spec 145).

**Failure modes.**

| Situation | Result |
|---|---|
| A password, user or host with only ASCII characters | No new form; scrubbing is as before |
| A non-ASCII character of each escape width in one value | One form holds all three escapes, as `ascii()` writes the whole value |
| The value holds both `'` and `"` | The form with `\'` is listed as well as the one with `'` |
| Computing a form on the logging path | Cannot raise: `ascii()` accepts every string |

**Order.** The password forms change on `main` alone. The user and host
criterion needs spec 145's code, so this amendment is implemented after
PR #360 merges.

**Proof.**
`services/api/tests/test_store_password_scrub.py::test_a_password_quoted_by_ascii_is_masked_at_every_sink`
runs the four sinks of the first criterion with a password made at run
time, quoted alone by `ascii()`, by `%a`, and inside a dict by `!a`.
One password holds `'`, a backslash and a non-ASCII character of each
escape width; the other holds the same and `"`. Each case finds no
8-character window of the password in what the sink wrote. Removing either `ascii()` form fails at least one
case.
`services/api/tests/test_logging.py::test_a_non_ascii_user_and_host_are_masked_as_ascii_writes_them`
logs a record that quotes a non-ASCII user and host with `ascii()` and
with `%a`, and finds neither in the output.

**Limitations.**

- One level of quoting, as before. `ascii()` of a value already quoted by
  `!a` escapes the backslash again, and that text is not masked.
- A host is matched in any case as text, not as a name: a host written
  `h\xf6st` does not match `H\xd6ST`, where the non-ASCII letter itself
  changed case. The JSON form has the same limit since spec 145.
- `repr` of the password's bytes, and any other encoding, stay as Honest
  limitations lists them.
