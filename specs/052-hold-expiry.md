---
spec: 052
title: Company holds expire on their hold_until date
status: accepted
approved: yes
milestone: M8
depends: [023]
---

# Spec 052: Company holds expire on their hold_until date

## Problem

The hold list exists to keep specific companies out of screening for a
while: typically a reapply cooldown after a rejection. The CSV shape
(`config/companies-hold.example.csv`) has always carried a `hold_until`
column for exactly this reason.

Nothing reads it. `read_hold_file`
(services/api/src/harrier/userconfig/accessors.py) uses only the `company`
column, and the stored form (`company_holds` in `user_config`, spec 023) is
a list of bare names, so the date cannot even survive an import. Every hold
is therefore permanent until someone remembers to hand-edit a row away. A
hold written as "until end of June" keeps excluding the company in August,
silently: discovery reports `skipped_hold` counts but nothing marks a skip
as past its own expiry date. The failure was observed in practice when an
import brought in rows whose `hold_until` had already passed, and every one
of them became an indefinite exclusion.

## Scope

The read path and the config surfaces that carry hold entries:

- `read_hold_file` and `load_hold_companies`
  (services/api/src/harrier/userconfig/accessors.py): expiry evaluated at
  read time.
- The `company_holds` store shape and its validation
  (services/api/src/harrier/userconfig/store.py, spec 023).
- `harrier config import`, `harrier config get company_holds`,
  `GET /config/company_holds`, `PUT /config/company_holds`.

- The seen check in `screen_jobs`
  (services/api/src/harrier/screening/pipeline.py): a posting rejected for
  a hold is judged again once the hold is no longer active (amendment below).

The pipeline otherwise consumes a set of names, as before.
No contract change: `value` stays untyped JSON.

## Behavior

A hold entry may carry an expiry date. A hold is **active** when it has no
date, or when today is on or before its date. Only active holds exclude a
company from screening. Expiry is evaluated at read time, on every read, so
a hold lapses the day after its date with no write, no import, and no other
action.

Definitions:

- Date format is `YYYY-MM-DD`, matching the existing CSV column.
- "Today" is the local calendar date of the machine running the read. This
  is a single-user, single-machine tool (ADR-008); no timezone handling
  beyond that.
- The boundary is inclusive: a hold with `hold_until: 2026-06-30` is active
  on 2026-06-30 and inactive on 2026-07-01.

Where the date lives, per source:

- **CSV file** (`config/companies-hold.csv`): the existing `hold_until`
  column. Empty means no expiry. `reason` and `notes` stay ignored.
- **Store** (`company_holds` kind): an entry is either a bare string (a
  company held with no expiry, the pre-052 shape, still valid) or an object
  `{"company": "<name>", "hold_until": "YYYY-MM-DD"}` where `hold_until` is
  optional. `set_config` and `get_config` validate both forms, in keeping
  with the store's refuse-don't-coerce rule (spec 023, PR #20 finding).

Behavior changes by surface:

- `load_hold_companies` returns the normalized names of **active** holds
  only, from whichever source resolves. The screening pipeline
  (services/api/src/harrier/screening/pipeline.py) is unchanged: it already
  consumes a set of names, and `skipped_hold` now counts only skips caused
  by active holds.
- `harrier config import` preserves `hold_until` from the CSV into the
  store instead of stripping it. It imports expired rows as written:
  expiry is read-time semantics, not import-time filtering, so a
  round-trip does not lose data.
- `harrier config get company_holds` and `GET /config/company_holds` with
  `source: store` return the stored entries as written, including expired
  ones and their dates: the config surface shows what you wrote, screening
  applies what is active. With `source: file` the value remains the
  active normalized name list, as today.
- `PUT /config/company_holds` accepts both entry forms and answers 400
  with the store's `ConfigError` message on a malformed entry, through the
  existing error path.

If several entries name the same company, the company is held while any of
them is active.

A posting skipped because of a hold is recorded in the seen store with the
reason `hold`. Once its company has no active hold, the next discovery run
judges it like a posting never seen before, instead of skipping it as
seen. Every other recorded decision is still skipped. A posting whose
company is still held stays skipped as seen.

## Failure modes

- **Malformed date** (`2026-6-30`, `30-06-2026`, `soon`): refused loudly,
  never treated as "no expiry". A silent fallback would recreate the
  original bug in a new form. `set_config` and `get_config` raise
  `ConfigError` naming the entry; the API answers 400; `config import`
  prints the error and exits 1. A malformed date in the CSV makes the file
  read raise, so a discovery run using the file fallback fails with an
  error naming the file and the company rather than screening with wrong
  holds.
- **Entry object without `company`, or with a blank company**: refused with
  `ConfigError`, same as blank strings are dropped today after trimming.
  An object with a company and unknown extra keys is refused, not
  silently accepted: the extra key is probably a typo of `hold_until`.
- **Empty list, empty file, header-only CSV**: no holds, as today. A
  stored `[]` still means "cleared on purpose" and does not fall through
  to the file (spec 023 semantics unchanged).
- **The second run, and the passage of time**: a hold active yesterday and
  expired today stops excluding the company with no write; `updated_at`
  on the store row does not move. Nothing notifies about the lapse (out of
  scope below).
- **Legacy stored value** (bare name list written before this spec):
  validates and behaves as holds with no expiry. No migration is required
  for it.
- **`conn=None` file path**: same expiry semantics as the store path; the
  filter lives in the accessor, not in the store.

## Acceptance criteria

Proof lives in services/api/tests/test_userconfig.py unless named
otherwise.

- A CSV row with `hold_until` before today does not exclude the company:
  `load_hold_companies` omits it, and a discovery run over a posting from
  that company does not increment `skipped_hold`.
  `test_a_csv_hold_past_its_date_no_longer_excludes_the_company`;
  `tests/test_discovery.py::test_a_lapsed_hold_is_not_counted_as_a_hold_skip`.
- A CSV row with `hold_until` equal to today excludes the company
  (inclusive boundary). `test_a_csv_hold_is_active_on_its_own_date`.
- A CSV row with an empty `hold_until`, and a stored bare-string entry,
  exclude the company indefinitely. `test_a_hold_with_no_date_never_lapses`.
- A stored object entry with a past `hold_until` does not exclude the
  company; the same entry with a future date does.
  `test_a_stored_dated_hold_lapses_after_its_date`; several entries for
  one company: `test_a_company_is_held_while_any_of_its_holds_is_active`.
- `harrier config import` over a CSV with dates, followed by
  `harrier config get company_holds`, shows entries with their
  `hold_until` values preserved, including rows already expired.
  `test_import_keeps_hold_dates_including_expired_ones`.
- `set_config` with `{"company": "X", "hold_until": "2026-6-1"}` raises
  `ConfigError`; `PUT /config/company_holds` with the same body answers
  400. `test_a_malformed_hold_date_is_refused_at_the_write`,
  `test_the_api_refuses_a_malformed_hold_date`.
- `set_config` with an entry object missing `company` or carrying an
  unknown key raises `ConfigError`.
  `test_a_hold_entry_without_a_company_or_with_an_unknown_key_is_refused`.
- Reading a CSV with a malformed `hold_until` raises an error that names
  the company; `harrier config import` over that file exits 1 with that
  message and stores nothing for `company_holds`.
  `test_a_malformed_csv_date_refuses_the_read_and_the_import`.
- A stored value written before this spec (list of bare names) reads back
  without error and holds every listed company.
  `test_a_hold_list_stored_before_expiry_existed_still_holds_everyone`.
- A posting skipped for a hold on one run is judged on the first run after
  the hold lapses and can reach the tracker; while the hold is active a
  repeat run skips it as seen.
  `tests/test_discovery.py::test_a_posting_skipped_for_a_hold_is_judged_again_once_the_hold_lapses`.
- The config surface shows stored entries as written and file holds as
  active names.
  `test_the_api_shows_stored_holds_as_written_and_file_holds_as_active`.
- `just contract` after the change produces no OpenAPI diff:
  `ConfigIn.value` and `ConfigOut.value` are already untyped JSON, so the
  contract does not move.

## What the implementation decided

Recorded here so the spec and the code agree.

- **Amended in review (PR #104): a lapsed hold also releases postings it
  already skipped.** The first version changed only which companies are
  held. The seen store remembers every hold rejection, and `screen_jobs`
  consults it before the hold, so a posting fetched during a hold stayed
  skipped as seen after the hold ended. That broke the promise above that a
  hold lapses "with no write, no import, and no other action". The seen
  check now lets such a posting through when its company has no active
  hold. Holds are not part of the screening policy version, so spec 031's
  `reconsider` could not have released them either.

- **An empty `hold_until` in the object form means no expiry,** as an empty
  CSV cell does. It is dropped on the way in, so
  `{"company": "X", "hold_until": ""}` is stored as `{"company": "X"}`.
- **The date shape is checked before the calendar.**
  `date.fromisoformat` accepts `20260630`; the CSV column's format does not,
  so it is refused. `2026-02-30` passes the shape and is refused by the
  calendar.
- **The import reads every file before storing anything.** A malformed
  hold date therefore stores nothing for any kind, not only for
  `company_holds`. This was already the order in `_cmd_config`; a comment
  now says it is load-bearing.
- **A CSV row with no date imports as a bare name,** the pre-052 shape, and
  a dated row as the object form.
- **`load_hold_companies` and `read_hold_file` take an optional `today`,**
  so the boundary tests do not depend on the date the suite runs.
- **`stored_list` no longer serves holds.** It casts to a list of strings,
  which a hold list may no longer be; `load_hold_companies` reads the store
  through `get_config` instead.
- **The discovery criterion is proved in `tests/test_discovery.py`,**
  through `run_discovery`, rather than in `test_userconfig.py`, so it uses
  that module's isolated config fixture and no private helper.

## Limitations

- **A malformed CSV date made `GET /config` answer 500,** because the file
  fallback raises `ConfigError` and the route did not map it. Spec 073
  replaces the 500 with an `error` field on the kind. Discovery and import
  still fail with a message naming the file and the company.

## Proof / origin

The failure was observed in practice: an import brought in CSV rows whose
`hold_until` had already passed, and every one became an indefinite
exclusion. The unread column is visible in code: `read_hold_file`
(services/api/src/harrier/userconfig/accessors.py) uses only the `company`
column, and the stored form (`company_holds`, spec 023) is a list of bare
names, so the date cannot survive an import. The CSV shape carrying the
column is `config/companies-hold.example.csv`.

## Out of scope

- Pruning: expired entries are not deleted from the store or the CSV, by
  this change or any background job. They stay visible in the config
  surfaces until the user removes them.
- Notification when a hold expires (digest, Telegram, or otherwise).
- Honoring `reason` or `notes` from the CSV anywhere.
- A dedicated UI editor for holds; the web app renders config values
  generically and keeps doing so.
- Typing `company_holds` in the OpenAPI contract; `value` stays `object`.
- Any change to hold matching (normalization via
  `harrier.screening.normalized.normalize` is unchanged).
- Reapply recommendations or any workflow that acts on a lapsed hold.

## Migration

None required. Existing stored bare-name lists keep working as indefinite
holds. To adopt expiry dates, either edit the CSV and re-run
`harrier config import`, or `PUT`/`config set` the object form directly.
