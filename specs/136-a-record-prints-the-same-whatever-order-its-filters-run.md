---
spec: 136
title: A log record prints the same whatever order its filters run in
status: proposed
approved: no
milestone: M10
depends: [045, 133, 135]
---

# Spec 136: A log record prints the same whatever order its filters run in

Recorded as out of scope in PR #308 (spec 135), "Out of scope, noted":
`IdentityRedactionFilter` still formats a traceback the old way when it
runs first. Akin asked for it to be specified on 2026-10-13.

## Problem

Two filters change a log record before a handler prints it
(`services/api/src/harrier/logredact.py`):

- `SecretScrubFilter` (specs 133 and 135) masks the store password and
  other secrets, and prints a driver error (psycopg's or sqlite3's) without
  its text, which on Postgres quotes the row in its `DETAIL` lines.
- `IdentityRedactionFilter` (spec 045) replaces candidate and contact
  identity values.

Each one renders the record itself, and the first to render it decides
what every later filter and the formatter see:

- **The traceback.** A filter fills `record.exc_text` when it is empty,
  and `logging.Formatter` prints an `exc_text` it finds rather than format
  the exception again. `SecretScrubFilter` fills it with
  `format_exception_withheld`. `IdentityRedactionFilter.filter` fills it
  with `traceback.format_exception`, which prints every exception in the
  chain by `str()`, a driver error's `DETAIL` included. A secret scrub
  that runs after it finds `exc_text` set and only masks secrets in it:
  the row values stay.
- **A driver error passed as an argument.** When the rendered message
  holds an identity value, `IdentityRedactionFilter` writes the rendered
  message into `record.msg` and empties `record.args`. The message was
  rendered with `str()` of each argument, so a driver error's `DETAIL` is
  now in `msg`, and a secret scrub that runs after it has no argument left
  to withhold.

So a record prints without row values only because every handler harrier
installs puts `SecretScrubFilter` first (`services/api/src/harrier/logsetup.py`,
`configure_logging` and `_install_identity_redaction`). Nothing states that
the order matters or keeps it. A handler that gets the identity filter
first, or alone, prints the row values a driver error quotes. On such a
handler with no secret scrub at all, it also prints the store password in
a traceback.

No such handler exists today. This spec makes the order stop mattering,
rather than documenting it as a rule a later change has to remember.

## Scope

1. **The identity filter runs the secret scrub first.**
   `IdentityRedactionFilter.filter` passes the record through the secret
   scrub (the one `SecretScrubFilter` instance `harrier.logsetup` installs,
   `SECRET_SCRUB`, or the same steps) before it renders anything. So every
   record that leaves the identity filter has had its secrets masked and
   its driver errors withheld, whatever ran before it.
2. **The secret scrub is idempotent.** A record scrubbed twice prints
   exactly what it prints scrubbed once, so a handler that runs both
   filters, in either order, prints the same text.
3. **Nothing else.** `configure_logging` keeps installing the secret
   scrub first. No filter is added to or removed from any handler. No log
   text that today's handlers print changes.

Classification: every file this spec changes sits under
`services/api/src/` and `services/api/tests/`, which
`config/data-classification.json` classifies as public. No new file.

## Behavior

For any record, and a handler with either filter alone or both in either
order, the printed text:

- holds no identity value the identity filter knows, when that filter is
  on the handler;
- holds no form of the store password and no secret `scrub_outbound`
  masks;
- prints each driver error, in the traceback's chain and as the message
  or an argument, as `driver_error_text` gives it (spec 135), never by
  `str()`.

With both filters, the text is the same in either order.

The identity filter still returns at once when it holds no values. That
case does nothing today, so a handler on which only the identity filter
sits, holding no values, prints the record as the formatter renders it.
That is unchanged here and named in Honest limitations.

## Failure modes

- **The secret scrub fails inside the identity filter.** It withholds the
  record as it does today (`log record withheld: it could not be
  scrubbed`), and the identity filter then has nothing to redact.
- **A record already scrubbed by an earlier filter.** Scrubbed again with
  no change (Scope item 2): `REDACTED` is not re-masked into another
  form, a withheld driver line is not withheld again, and a message whose
  arguments were rendered is rendered once.
- **A format string that does not fit its arguments.** The secret scrub's
  handling (spec 133) runs first, so the identity filter finds a message
  that renders, and its own early return for that case is no longer
  reached with arguments in it.
- **The identity values are refreshed while a record is filtered.**
  Unchanged: the refresh replaces the value list in place (spec 045).

## Acceptance criteria

- [ ] A handler with `IdentityRedactionFilter` alone, holding a synthetic
      identity value, prints a record logged with `logger.exception` for a
      store error chained to a psycopg error whose text quotes a synthetic
      row value and the store password, and whose message holds the
      identity value: the output holds none of the row value, the password
      or the identity value
      (planned test_the_identity_filter_alone_withholds_what_the_scrub_withholds).
- [ ] The same, for a psycopg error passed as an argument to a message
      that holds the identity value
      (planned test_the_identity_filter_alone_withholds_a_driver_argument).
- [ ] For the records above, a handler with the two filters in each order
      prints the same text, and that text equals what the handler
      `configure_logging` installs prints
      (planned test_filter_order_does_not_change_what_a_record_prints).
- [ ] A record passed through the secret scrub twice prints what it prints
      after once, for a record holding a URL password, a secret parameter,
      a driver error in its chain and a driver error as an argument
      (planned test_the_secret_scrub_is_idempotent).
- [ ] Every existing test in `services/api/tests/test_logging.py`,
      `services/api/tests/test_store_password_scrub.py` and
      `services/api/tests/test_hosted_logs.py` passes unchanged.
- [ ] Each new test fails when the code it proves is removed, and the pull
      request records each mutation and its result.
- [ ] `uv run ruff check`, `uv run pyright` and `uv run lint-imports`
      pass, and `just check` passes.

The tests construct psycopg errors in memory, as
`services/api/tests/test_hosted_logs.py::test_an_unrenderable_driver_error_keeps_its_record`
does, so they need no Postgres server.

## Honest limitations

- **The identity filter with no values does nothing.** A handler with only
  that filter, holding no values, has no secret scrub at all. Every
  handler harrier installs carries the secret scrub, so this is a handler
  harrier does not install. Making the empty identity filter scrub too
  would make it a second secret scrub under another name. Not done.
- **Twice the work on a handler with both.** The secret scrub runs twice
  per record there. It holds no state and reads no file per record; the
  cost is a second pass over the record's text.
- **Handlers harrier does not install** carry neither filter, as spec 135
  says.

## Out of scope

- Which filters `configure_logging` installs, and in which order: spec
  045, spec 133 and spec 135, unchanged.
- Identity values in hosted logs: spec 135.
- What the secret scrub masks: spec 133.

## Migration

None. No log text that today's handlers print changes.

## Options weighed

- **Share only the driver step.** Factor the driver withholding (the
  traceback and the arguments) out of `SecretScrubFilter` and call it from
  both filters. Covers the row values, not the password, on a handler
  where the identity filter runs alone. Not chosen: it leaves the order
  mattering for the password.
- **Document the order as a rule.** A comment and a test that reads
  `handler.filters`. It tests the helper, not the decision, and the next
  handler added has to remember the rule. Not chosen.
- **One filter that does both.** Merge the identity redaction into the
  secret scrub. A larger change to spec 045's filter, its refresh registry
  and its tests, for the same result. Not chosen.

## Proof / origin

- PR #308, "Out of scope, noted".
- `services/api/src/harrier/logredact.py` (`IdentityRedactionFilter.filter`,
  `SecretScrubFilter`, `format_exception_withheld`), and
  `services/api/src/harrier/logsetup.py` (`configure_logging`,
  `_install_identity_redaction`).
- `specs/045-claims-and-gates.md`,
  `specs/133-the-store-password-stays-out-of-logs-and-http-errors.md`,
  `specs/135-hosted-logs-carry-no-personal-data.md`.
- Python's `logging.Formatter.format`: it formats `exc_info` only when
  `exc_text` is empty.
