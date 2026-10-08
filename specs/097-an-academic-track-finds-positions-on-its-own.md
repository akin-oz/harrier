---
spec: 097
title: An academic track finds new positions on its own, from a configured Apify source
status: proposed
approved: no
milestone: M9
depends: [009, 011, 020, 021, 022, 023, 029, 031, 033, 035, 052, 091, 092, 093, 094]
---

# Spec 097: An academic track finds new positions on its own, from a configured Apify source

## Problem

An academic track (for example university or research vacancies) is filled
by hand only. Spec 093 made that the whole of it: "There is no discovery,
no scoring, no artifact generation and no outreach on it". Its Out of scope
list hands on "Sources, discovery and importers for an academic track;
scheduled discovery across tracks".

Four things stand in the way today, and each is in the code:

- **`discover` refuses a non-default track.** It is not in
  `NON_DEFAULT_OPERATIONS` (`services/api/src/harrier/tracks.py`), so
  `harrier --track <slug> discover` exits 2.
- **Screening is industry policy.** `screen_jobs`
  (`services/api/src/harrier/screening/pipeline.py`) applies the title
  rules, the remote-only and region gate, and the fit score, all read from
  the `candidate` profile document (imported from the never-in-git
  `config/candidate.json` and read by
  `harrier.screening.config.load_candidate_config`, with the committed
  example as fallback). A university or research position is usually on
  site, so the remote gate would reject most of them. The industry title
  rule `title_allowed` (`services/api/src/harrier/screening/rules.py`)
  applies a compiled table of excluded title hints before it reads any
  list, rejects every title when its include list is empty, matches whole
  words only, and reads text that `normalize`
  (`services/api/src/harrier/screening/normalized.py`) has only lowercased
  and collapsed.
- **Decisions carry the industry policy version.** `screen_jobs` stamps
  `policy_version(candidate_cfg)`, a digest of the industry configuration,
  the industry rule tables and the active learned model
  (`services/api/src/harrier/screening/policy.py`). Seen state keeps one
  file per source with no track in its path
  (`services/api/src/harrier/screening/seen.py`), and `reconsider` reopens
  decisions only by that industry version.
- **No source returns a deadline.** `NormalizedJob`
  (`services/api/src/harrier/screening/normalized.py`) has no deadline
  field, and the academic queue is ordered by the deadline (spec 093).

The operator wants positions from an academic job aggregator that runs as
an actor on Apify, the paid platform the LinkedIn source already uses
(spec 009). The actor is priced per event: per run and per result. Its own
defaults allow runs far larger than a daily driver should buy, it searches
full text with each portal's relevance ranking, it can expand keywords into
other languages on its side, and it can keep its own memory of earlier
runs. Each of those decides what harrier is shown, so the search has to be
written once and compiled into both the actor's input and harrier's gates.

## Scope

**Domain** (`services/api/src/harrier/`)

- `sources/apify_academic.py` (new): ingestion only. It compiles the
  actor's input from the track's search entry, starts the actor named in
  the environment, waits for the run, reads the dataset, and normalizes
  each item into the shared job shape. It filters, scores and writes
  nothing. The actor's input field names (`INPUT_MAP`), its output field
  names (`FIELD_MAP`), the known portal ids (`KNOWN_PORTALS`), the
  supported country codes, the prices used for the worst-case check, the
  run's memory and timeout, and the hard limits (`ACADEMIC_MAX_RESULTS`,
  `ACADEMIC_MAX_CHARGE_USD`) are constants here. They describe the third
  party's interface, not the operator's search.
- `screening/normalized.py`: `NormalizedJob` gains `deadline`, an ISO date
  or the empty string. Every existing source returns it empty.
- `screening/rules.py`: a new academic term matcher (see Behavior). The
  industry `title_allowed` is unchanged.
- `screening/pipeline.py`: `screen_jobs` takes the gates from the scope's
  kind. The industry kind applies exactly today's gates. The academic kind
  has its own row builder.
- `screening/policy.py`: the academic kind has its own policy version.
- `screening/seen.py`: seen state for a non-default track is kept per
  track and source.
- `screening/reconsider.py`: `reconsider` works on an academic track.
- `tracks.py`: `KIND_RULES` gains the screening each kind applies.
  `discover` and `reconsider` join `NON_DEFAULT_OPERATIONS`, and
  `discover` joins `WRITE_OPERATIONS`.
- `discovery.py`: on an academic track, discovery runs only that track's
  configured source and loads no candidate configuration, no hold list and
  no feeds. Its summary and success record are kept apart from the
  default track's.
- `tracker/store.py`: the academic duplicate rule (see Behavior), and
  `all_tracks_dedupe_rows` also returns `deadline`.
- `tracker/queue.py` and the `next` and `review` output: an academic row
  prints its components, one per line.
- `notify.py`: the academic Telegram summary.
- `schedule.py`: a calendar time may name a `weekday` (1 for Monday to 7
  for Sunday), rendered as launchd's `Weekday` key. A time without one runs
  every day, as today.
- `userconfig/store.py`: one new kind, `academic_searches`.

**Command line** (`services/api/src/harrier_cli/main.py`)

- `harrier --track <slug> discover [--dry-run] [--shadow] [--no-notify]
  [--dataset-file PATH ...] [--from-run RUN_ID]`.
- `harrier discover --scheduled --configured-tracks`: one discovery per
  track named in the stored search, each in its own scope. It never runs
  the default track.
- `harrier --track <slug> reconsider`.
- `harrier config set academic_searches --file PATH`, through the existing
  `config set` command.

**API** (`services/api/src/harrier_api/app.py`)

- `GET /config` and `GET /config/{kind}` require the local API token. This
  closes spec 023's open item (planned test_config_reads_require_the_token
  in `services/api/tests/test_api_exposure.py`).
- `packages/contract/openapi.json` and `packages/contract/src/schema.d.ts`,
  regenerated by `just contract`. A guarded path, approved under this spec.

**Configuration**

- `config/academic-searches.example.json` (new, public, placeholder values
  only) documents the entry's shape. There is no never-in-git file for this
  kind and no file fallback. Classified public in
  `config/data-classification.json`, a guarded path approved under this
  spec.
- `.env.example`: `APIFY_ACADEMIC_ACTOR=` with no value. The actor's name
  lives only in `.env`; no default is written into the code.
- `config/schedule.json`: a new `academic-discovery` job (see Behavior).

**Governance**

- The product invariant "Remote-only and EMEA scope are enforced" is
  scoped to the industry kind, in its source under `.ai/`, compiled by
  `aie sync` into `CLAUDE.md` and `AGENTS.md`. The guarded governance edit
  is approved under this spec. See Behavior, "The invariant".

**Other specs**

- Spec 093: a dated note records that its "No profile read on an academic
  track" guarantee is narrowed by this spec, and that `reconsider` leaves
  its list of refused commands.
- Spec 096: a dated note records that its configuration page sends the
  token on the `GET /config` routes, and shows `academic_searches`
  read-only until an editor exists.

**Tests and fixtures**

- `services/api/tests/test_academic_discovery.py` (new), and the existing
  files named in Acceptance criteria.
- `fixtures/academic-dataset.json` (new, public, written by hand), listed
  in `fixtures/PROVENANCE.md`.

**Not touched**: the industry sources and their gates, the fit score, the
browser's pages (spec 096 shows the new kind read-only), the run manager's
run kinds, and artifacts or outreach on any track.

## Behavior

### The search entry

The `user_config` kind `academic_searches` holds one JSON object keyed by
track slug. Each value is that track's search entry:

| Field | Meaning | Compiles into the source input | Compiles into the gates |
|---|---|---|---|
| `areas` | Required, non-empty list of subject areas. Each area has a `label` and `terms`. Each term has `text` and an optional `prefix` (true: it matches the start of any word, which reaches compound words). The operator writes every local-language variant. | Every area term becomes a search keyword. | Matched against the title, the description excerpt and the subject field. Recorded on the row as `matched=`. Rejects only when `require_area_match` is true. |
| `require_area_match` | Optional; default false. | Not sent. | When true, a posting with no area match is rejected as `area_unmatched`. |
| `position` | Optional `{terms, in}`. Terms have the area term shape and name the kind of position. `in` is a subset of `title`, `position_type` and `description`; the default is `title` and `position_type`. | Not sent: the source combines its keywords with OR, so a position word would widen the search. | When set, a posting is kept only if a term matches one of the `in` fields; otherwise it is rejected as `position_unmatched`. |
| `exclude` | Optional list of `{terms, in}`. `in` is a subset of `title`, `organisation` and `position_type`; the default is `title`. | Not sent: the source has no exclusion input. | A match rejects as `exclude:<term>@<field>`. Exclusions never read the description. |
| `countries` | Required, non-empty list of ISO 3166-1 alpha-2 codes, each one the source supports. The track's region scope. | The source's country filter. | Not re-checked by harrier. |
| `window_days` | Required positive integer: how many days back the source searches. | The source's posting-age filter. | The summary names any gap since the last success longer than the window. |
| `portals` | Optional list of portal ids, each in the source's `KNOWN_PORTALS`. | The source's portal selection; when absent, the source's default set. | The summary names every listed portal that returned none. |
| `flag_phrases` | Optional map from a flag name to a list of phrases. The example ships placeholders for `eligibility`, `funding` and `language`. | Not sent. | A phrase found in the title or the excerpt flags the row and records the evidence. Never rejects. |
| `funding_flag_values` | Optional list of the source's funding values that mean a position is unfunded. | Not sent. | Flags `funding` and records the value. Never rejects. |
| `ceilings` | Required `max_results` and `max_charge_usd`. | The source's overall result cap, and the run's `maxItems` and `maxTotalChargeUsd`. | Refused before any request when the worst case exceeds `max_charge_usd`. |

No field names the actor or any of its input or output fields: those are
the source module's constants. The entry is the operator's search, not the
actor's interface, so a later source on the same track compiles its own
input from the same entry and goes through the same gates.

### Where it lives

- One `user_config` row holds every track's entry, so `user_config`'s
  unique key on `kind` is kept and the schema does not change. Slugs are
  stable keys because no track is renamed.
- The store validates the value on write and on read (spec 023): every key
  passes the slug rule; every entry has exactly the fields above, an
  unknown field refused by name; the required fields are present, a
  missing one named; `countries` and `portals` hold only known values; the
  ceilings sit within their hard limits. A refusal names the field.
- Resolution is the stored row or empty. There is no file fallback, because
  a loose file would be a second home for the search (ADR-009, decision 2:
  files are import paths, never the source of truth). `harrier config set
  academic_searches --file PATH` and `PUT /config/academic_searches` are
  the writers, with the same validation.
- Whether a slug names a live academic track needs a connection, so it is
  checked when a run starts. An unknown, archived or industry slug is
  reported there, never at the write.
- The database is never-in-git (ADR-008), so the terms stay off GitHub. The
  value describes the operator's own search, which is why the
  `GET /config` routes require the token before this kind exists.

### Compilation

The source's search terms are every area term, its countries are
`countries`, its posting-age filter is `window_days`, its portal selection
is `portals` when set, and its overall result cap is `max_results`.

Two settings are always compiled and cannot be configured. The source's
keyword translation is off, because the gates cannot know expansions made
on the source's side. The source's own memory of earlier runs is off,
because seen state is harrier's alone (spec 031): a dry run or a failed run
must never change what a later run receives.

`INPUT_MAP` in the source module names the source's input field for each
compiled value, and a test holds that it names every one of them. `FIELD_MAP`
names the source's output field for each shared field, including the
description excerpt, which maps to `description`.

### The ceilings

`maxItems` bounds only actors priced per result. On an actor priced per
event it has no effect, and `maxTotalChargeUsd` is the only ceiling on the
platform side. So the source's own overall result cap is what bounds a run,
and the charge ceiling is the backstop. The compiled run writes
`max_results` into the source's result cap and also passes it as
`maxItems`, and passes the module's memory and timeout as the run's
`memory` and `timeout` options.

An entry is refused, the refusal naming the field and the numbers, when:

- `max_results` is not a positive integer;
- the worst case, the start price for the run's memory plus `max_results`
  times the price per result, exceeds `max_charge_usd`;
- either ceiling exceeds its hard limit (`ACADEMIC_MAX_RESULTS`,
  `ACADEMIC_MAX_CHARGE_USD`).

The hard limits are checked at the write. A value that reaches the store
another way is clamped where it is read, as spec 035 does for the
discovery count, and the summary says so.

### What a run does

`harrier --track <slug> discover`:

1. Resolves the track once (spec 092). An unknown slug, an industry track
   other than the default, and an archived track each exit 2 naming the
   track, since `discover` writes.
2. Reads one `user_config` row, this kind's, and uses only its own track's
   entry. A track with no entry exits 2 with "no search is configured for
   track <slug>". It loads no candidate configuration, no hold list, no
   feeds and no profile document, and runs no industry source.
3. Compiles the input and the gates. A refused entry exits 2 naming the
   field, before any request.
4. Starts the actor, polls until a terminal status, and reads the dataset.
   `APIFY_TOKEN` and `APIFY_ACADEMIC_ACTOR` come from the environment.
5. Saves the raw dataset and the run id, unless this is a dry run (see
   below).
6. Normalizes each item through `FIELD_MAP`. An item with no title or no
   URL is skipped and counted.
7. Screens the jobs through `screen_jobs` with the academic gates and the
   academic policy version.
8. Appends each kept job through `add_job` with the track's scope, as a
   `prospect` with the kind's next action and its deadline, built by the
   academic row builder.
9. Writes its summary and success record, and, unless `--no-notify`, sends
   the Telegram summary. Telegram is the one outbound channel allowed.

### Dry runs, shadow runs and replays

- `--dry-run` starts a billed run, as industry `--dry-run` does, and
  writes nothing: no row, no seen state, no dataset file, no summary file,
  no message. It prints the run id, so the run can be replayed.
- `--shadow` runs no paid source (spec 022). On an academic track it
  resolves the track and its entry, compiles the input and the gates, and
  prints the compiled input with the source's field names, the ceilings
  and the worst-case cost, each gate with its terms, and the policy
  version. It makes no network request, needs neither environment
  variable, starts nothing, writes nothing and records no run outcome. It
  exits 0 when the entry compiles and 2 when it is refused, naming the
  field. This printout goes to the operator's terminal; it is not a log.
- `--dataset-file PATH` (repeatable) screens a saved dataset instead of
  starting a run, as the LinkedIn source's mode does (spec 009).
  `--from-run RUN_ID` reads an existing run's dataset from Apify without
  starting a new run. Neither starts the actor or adds an actor charge.
  Both combine with `--dry-run`, and a replay goes through the same
  normalization, `screen_jobs`, seen state and write path as a live run.
- A billed run that is not a dry run saves its raw dataset and run id
  atomically under the data directory
  (`discovery/<track id>/<source>/runs/<run id>.json`, never-in-git)
  before normalization, so a later change to the field map can be replayed
  too. The summary names the file. Each track keeps its newest files up to
  a constant in code, and older ones are evicted by age.

### The academic matcher

The academic term matcher is its own function in `harrier.screening.rules`,
called from `screen_jobs`. It applies no compiled industry table (not the
industry title hints, not the remote and region patterns, not the scoring
tables), and it reads no candidate configuration.

Before matching, the text and every term are put in Unicode NFKC form,
case-folded and stripped of combining accents, and periods between letters
are removed. A term matches as a whole word. A term with `prefix: true`
matches the start of any word, so a stem reaches compound words. A term of
several words matches with any run of spaces, hyphens or slashes between
its words. An empty or absent list rejects nothing. Area terms go through
the same function and are recorded, not used to reject, unless
`require_area_match` is true.

### The gates per kind

`KIND_RULES` names the gates. `screen_jobs` stays the one screening path;
the kind decides which of its gates apply. The academic gates run in this
order, and a test pins it, because the recorded reason is whichever gate
fails first:

| Order | Gate | Outcome | Reads | Entry field |
|---|---|---|---|---|
| 1 | seen state (spec 031), per track and source | skip a recorded decision, except `deadline_passed` | identity keys | none |
| 2 | exclusions | reject `exclude:<term>@<field>` | title, organisation, position type | `exclude` |
| 3 | position | reject `position_unmatched`; off when unset | title and position type by default; description only when listed in `in` | `position` |
| 4 | area | record `matched=` on every kept row; reject `area_unmatched` only when required | title, description excerpt, subject | `areas`, `require_area_match` |
| 5 | passed deadline | reject `deadline_passed`; judged again on every run | deadline | none (fixed rule) |
| 6 | dedupe against every track (spec 092) | reject `tracker_duplicate:<identity>:<track slug>` | URL, external id, application link with deadline, organisation with title (deadline-aware) | none |
| after keep | flags | flag with evidence; never reject or reorder | title, excerpt, funding | `flag_phrases`, `funding_flag_values` |
| after keep | components | shown on the row; never combined into a number | position type, subject, funding, start, contract, posting language, salary text, portal | none |

Not applied on the academic kind: the company hold list (spec 052), which
stays the industry watchlist's, so an institution to skip is an `exclude`
entry on `organisation`; the remote-only and region gate, since the region
scope is `countries`, applied by the source; the industry title hints and
candidate configuration; the fit score (spec 033 removed the cutoff).

The industry kind's gates are today's, unchanged.

### The deadline

- A deadline is a date only. The gate rejects a posting as
  `deadline_passed` only when its deadline is more than one day before the
  run's date. The run's date is taken once, from `DiscoveryOptions.now`,
  when the run starts. The day of grace covers the time-zone and
  closing-hour difference between harrier's host and the institution. A
  deadline on the run's date or the day before is kept, and the queue
  shows the latter as passed and sinks it (spec 093).
- Conversion is conservative. The source converts an ISO date, a date with
  a written month name, and a numeric date whose order is unambiguous (one
  part above 12). It does not convert a numeric date whose day and month
  could be swapped, a range, or text without a date. Such a deadline is
  stored empty and counted as `deadline_unreadable`, and its raw text is
  kept in the row's notes as `deadline_text`, with separators stripped. A
  time or zone after the date is dropped.
- The seen check never skips a `deadline_passed` decision. Such a posting
  is judged again on every run that returns it, as a lapsed hold is (spec
  052), so an extended deadline is picked up. Every other recorded decision
  is skipped as today.

A job with no deadline is kept: the queue puts it after the dated ones
(spec 093).

### Dedupe

For the academic kind, an organisation-and-title match is a duplicate only
when the two deadlines are equal or either is empty. When either deadline
is empty the rule is exactly today's, so industry rows, which carry no
deadline, are unaffected. The screening index matches normalized company
and title, and the store's `find_duplicate` matches them ignoring case; both
apply the deadline condition, and `all_tracks_dedupe_rows` also returns
`deadline`.

When the source gives a posting's direct application link, it is copied to
`metadata.apply_url` and stored as an `apply_url=` note. The link is
normalized by lowercasing the scheme and host and dropping the fragment and
any trailing slash. The query string is kept, because some boards identify
a posting only by it. A posting whose normalized application link and
deadline both equal an existing row's is a duplicate even when its listing
URL differs.

If the sample run shows an identity shared across portals, `FIELD_MAP` maps
it to `external_id`. The dedupe index keeps the track of each identity, and
the rejection reason names the identity that matched and the track that
holds it. This is how the discovery path names the holding track; today it
counts a duplicate without naming one.

### Components and flags

`FIELD_MAP` also names the source's fields for `position_type`, `subject`,
`funding`, `start`, `contract`, `posting_language`, `salary_text`,
`apply_url` and `portal`, where the source provides them. Each is copied
unchanged, as text, into `NormalizedJob.metadata` under that name; the
source judges nothing. A field the map does not name, or an item without
it, reads `not stated`, and is never treated as a negative. The sample run
decides which of these fields the source fills.

The academic row builder writes each copied field onto the kept row's notes
as its own key, with `;`, `|` and `=` stripped from the value, plus
`matched=<label>:<term>@<field>` (or `none`) and `position=<term>@<field>`.
It writes no `score`, `fit_score`, `signals` or `remote_filter`.

**Flags.** A phrase from `flag_phrases` found in the title or the excerpt
flags the row under that flag's name. A funding value listed in
`funding_flag_values` flags the row as `funding`. Each flag records the
phrase or value that fired it and the field it came from, as
`flags=<name>:<field>:<evidence>|...`. A flag never rejects and never
reorders; the queue stays spec 093's. A row with no matching phrase reads
`not stated`.

On an academic track, `next` and `review` print one component per line
under the row, for example `matched: '<term>' in title` or `funding: not
stated`. No component is summed, counted into a number or used to sort.

An `exclude` entry on `position_type` is a deny-list on purpose: a value the
operator has not seen yet, or one the source renames, is kept and shown.
The summary counts postings per position-type value, so the list is written
from what the source actually returns.

### The policy version and reconsider

- The academic kind has its own policy version, passed to `screen_jobs` as
  `policy=`. It is a digest of the entry fields that decide a rejection
  (`position`, `exclude`, `require_area_match`, and `areas` when
  `require_area_match` is true), the `FIELD_MAP` entries those rules read,
  the deadline rule and the academic matcher's rules. It is computed with
  model `none` passed explicitly, and it includes no industry
  configuration, no industry rule table and no learned-model identity.
  Editing those fields moves it. Editing the industry configuration,
  activating a learned model, or editing a field that only labels rows
  does not.
- Seen state for a non-default track is kept at
  `discovery/<track id>/<source>_seen.json` under the data directory. The
  default track keeps today's path.
- `reconsider` on an academic track computes the version from that track's
  entry, loads no candidate configuration, and reads and clears only that
  track's seen state. It keeps spec 031's two refusals: a human rejection
  is never reopened, and an acceptance is never reconsidered.

### What the summary reports

Every academic rejection records a reason of the form `<gate>:<detail>`:
`exclude:<term>@<field>`, `position_unmatched`, `area_unmatched`,
`deadline_passed`, or `tracker_duplicate:<identity>:<track slug>`. Seen
state stores it as the decision's reason. The summary keys
`rejected_counts` by gate and lists the ten most frequent details beside
it. It also contains:

- counts of fetched items, items skipped for a missing title or URL,
  unreadable deadlines, items without a posting date, duplicates inside
  the dataset, seen skips, kept rows, and kept rows without a deadline;
- counts per portal, always, and per position-type value;
- each area and position term with the number of kept rows it matched,
  naming terms that matched none;
- the result cap, the charge ceiling, the run object's charged event counts
  and their priced total, and the dataset's item count, saying so when the
  run's pricing differs from the module's prices;
- the policy version and the run id.

On an academic track, `--dry-run` prints each rejected posting's title,
organisation and reason, using the rejected-debug rows `screen_jobs`
already builds, turned on for this case.

### Separate records

An academic run writes its summary under `<data dir>/incoming/<track id>/`.
It never writes `incoming/job_imports_run.json`, and it records success
under a run-outcome key of its own, `discovery:<track id>`. The digest's
schedule-health lines list that job, so a stalled academic schedule is
visible (spec 029), and the default track's run summary and health are
unchanged. When a track's last success is older than `window_days`, the
summary names the gap, because postings published during it were never
requested. When a run stops at `max_results`, the summary says that results
ranked below the cap, including new ones, were not fetched.

The Telegram summary names the track's label, carries counts, and for each
kept row only its title, organisation, deadline and URL, showing the
deadline where industry shows a score. It never carries terms, flags or
description text.

### What is logged, sent and recorded

- The source logs a fixed label, the run id, the terminal status and
  counts. It never logs the actor's name, any compiled input value or any
  item text.
- Item text stays in the data directory. It is never logged above debug
  level, printed in the summary, or sent through Telegram.
- The sample run, and every check against it, stays under the data
  directory. The pull request records only the field names the source
  reads and whether each ceiling was honoured. It never records a count,
  cost, search term or posting.

### The invariant

`CLAUDE.md` today says: "Remote-only and EMEA scope are enforced. Hybrid
and onsite are rejected on location signals". After this change it applies
to the industry kind, and the academic kind's region scope is the
`countries` of the track's search entry. The rest of the invariant (the
location-field scoping, the EU-permit phrases as positive signals) is
unchanged. Changing a product invariant needs an explicit spec; this is
that spec.

"Ingestion only" is unchanged: the new source only fetches and normalizes,
and every gate runs in the shared screening path.

### Spec 093's guarantee, narrowed

Spec 093 says no allowlisted command reads `profile_documents` or
`user_config` on an academic track. After this change, `discover` and
`reconsider` on an academic track read one `user_config` row,
`academic_searches`, and use only their own track's entry; they read no
profile document. No other allowlisted command reads either table. The
redaction read during logging setup stays the one shared read (spec 093).

### The schedule

`config/schedule.json` gains:

```json
{
  "name": "academic-discovery",
  "command": ["discover", "--scheduled", "--configured-tracks"],
  "trigger": { "kind": "calendar", "times": [{ "weekday": 1, "hour": 9, "minute": 30 }] }
}
```

The committed file names no track: `--configured-tracks` reads the slugs
from the stored row. Each track runs in turn; an unknown, archived or
industry slug is reported, and one track's failure is reported while the
next still runs. A weekday in a calendar time is new: `harrier schedule
install` renders hours and minutes only today
(`services/api/src/harrier/schedule.py`, spec 020).

## Failure modes

- **No `APIFY_TOKEN` or no `APIFY_ACADEMIC_ACTOR`.** The run is refused
  before any request, naming the missing variable; nothing is billed.
- **A refused entry.** Refused at the write by `config set` and
  `PUT /config`, and again on read and before any request, naming the
  field.
- **The run reaches `max_charge_usd`.** Apify stops accepting results and
  aborts the run, so the run may end ABORTED. Harrier prices the run
  object's `chargedEventCounts` with its `pricingInfo`. When that total is
  within one result's price of `max_charge_usd`, the run counts as stopped
  at its ceiling whatever its status: its dataset is read, screened and
  written, and the summary says so.
- **Any other ABORTED, FAILED or TIMED-OUT run.** Nothing is written and
  seen state is unchanged, so the next run judges the same postings.
- **Harrier's own wait ends first.** Harrier aborts the run on Apify before
  it reports the timeout, so the remote run stops charging.
- **The actor run succeeded but screening or writing failed.** The saved
  file, or `--from-run`, screens those postings later without a new actor
  charge. Apify deletes unnamed datasets after its retention period, and
  `--from-run` says so when the dataset is gone.
- **An item `FIELD_MAP` cannot read.** Skipped and counted, never written
  half-filled.
- **A track in the stored search that is unknown, archived or industry.**
  That entry is reported; with `--configured-tracks` the others still run.
- **The same posting found for two tracks.** The second is rejected with a
  reason naming the identity and the track that holds it.

Must not introduce: an industry source running on a non-default track; a
fit score on an academic row; the actor's name, the operator's search
terms or a track slug in a committed file; a run without both ceilings; a
second screening path; an academic decision recorded under the industry
policy version; a record of seen postings kept outside harrier; an academic
run writing the default track's run summary or success record.

## Acceptance criteria

Tests in `services/api/tests/test_academic_discovery.py` unless named
otherwise. Every database is built under `tmp_path`; every actor response
is the synthetic fixture or a stub, served without network.

The search entry and where it lives

- [ ] The source input is compiled from the entry
      (planned test_the_source_input_is_compiled_from_the_profile)
- [ ] `INPUT_MAP` names every compiled value, the result cap included
      (planned test_the_input_map_names_every_compiled_field)
- [ ] Keyword translation and the source's memory are always compiled off
      (planned test_keyword_translation_and_source_memory_are_always_compiled_off)
- [ ] Position terms are not sent to the source
      (planned test_position_terms_are_not_sent_to_the_source)
- [ ] The entry names no actor field
      (planned test_the_search_entry_carries_no_actor_field_name)
- [ ] The entry is validated on write and on read: an unknown field, a
      missing required field named, a non-slug key, an unknown country or
      portal (planned test_the_academic_search_is_validated_on_write_and_on_read)
- [ ] An entry keyed by a malformed slug is refused at the write
      (planned test_a_profile_keyed_by_a_malformed_slug_is_refused_at_the_write)
- [ ] A corrupted stored row is refused on read
      (planned test_a_corrupted_profile_row_is_refused_on_read)
- [ ] The kind resolves to the stored row or empty, with no file fallback
      (planned test_the_academic_search_resolves_store_or_empty)
- [ ] `config set academic_searches --file` writes and validates it
      (planned test_config_set_writes_the_academic_search)
- [ ] Configuration reads require the token
      (planned test_config_reads_require_the_token in
      `services/api/tests/test_api_exposure.py`)
- [ ] `discover` on an academic track reads only this kind's
      `user_config` row and no profile document, held by a trace callback
      (planned test_academic_discover_reads_only_the_profile_kind)
- [ ] An academic `discover` loads no candidate configuration and no hold
      list (planned test_an_academic_discover_loads_no_candidate_configuration_or_hold_list)
- [ ] `discover` joins spec 093's proof:
      `services/api/tests/test_tracks_cli.py::test_academic_commands_read_no_profile_document`
      covers it, with only that criterion's command list extended
- [ ] No committed file holds a search, a slug or the actor's name, and
      the example holds placeholders
      (planned test_no_committed_file_holds_a_search)

The gates and the matcher

- [ ] A local-language variant is both searched and matched
      (planned test_a_local_language_variant_is_both_searched_and_matched)
- [ ] A posting with no area match is kept and recorded as `none`
      (planned test_a_posting_with_no_area_match_is_kept_and_recorded_as_none)
- [ ] `require_area_match` rejects as `area_unmatched`
      (planned test_require_area_match_rejects_as_area_unmatched)
- [ ] Position terms gate on their configured fields
      (planned test_position_terms_gate_on_their_configured_fields)
- [ ] An `exclude` on `organisation` rejects by the organisation field
      (planned test_an_exclude_on_organisation_rejects_by_the_organisation_field)
- [ ] The academic matcher applies no industry title hint, over every hint
      entry combined with a position term
      (planned test_the_academic_matcher_applies_no_industry_title_hint)
- [ ] An empty list rejects nothing
      (planned test_an_empty_list_rejects_nothing)
- [ ] A prefix term matches a compound word
      (planned test_a_prefix_term_matches_a_compound_word)
- [ ] A whole-word term does not match inside a longer word
      (planned test_a_whole_word_term_does_not_match_inside_a_longer_word)
- [ ] Matching ignores case, accents and Unicode form
      (planned test_matching_ignores_case_accents_and_unicode_form)
- [ ] Periods inside an abbreviation do not block a match
      (planned test_periods_inside_an_abbreviation_do_not_block_a_match)
- [ ] A phrase matches across hyphens and slashes
      (planned test_a_phrase_matches_across_hyphens_and_slashes)
- [ ] The academic gate order is pinned
      (planned test_the_academic_gate_order_is_pinned)
- [ ] `discover` on an academic track runs only its configured source
      (planned test_discover_on_an_academic_track_runs_only_its_sources)
- [ ] An academic row has no score, signals or remote filter
      (planned test_an_academic_row_has_no_score_signals_or_remote_filter)
- [ ] Seen state for one track never suppresses a posting for another
      (planned test_seen_state_is_kept_per_track)
- [ ] Seen state files are keyed by track
      (planned test_seen_state_files_are_keyed_by_track)
- [ ] The industry kind's screening is unchanged:
      `services/api/tests/test_screening.py`,
      `services/api/tests/test_screening_location.py` and
      `services/api/tests/test_seen_policy.py` pass with no assertion
      edited, and the industry reason slugs are unchanged

The ceilings

- [ ] The result cap is written into the source input
      (planned test_the_result_cap_is_written_into_the_source_input)
- [ ] An entry whose worst case can cost more than its ceiling is refused
      (planned test_a_profile_whose_caps_can_cost_more_than_its_ceiling_is_refused)
- [ ] A ceiling above its hard limit is refused at the write
      (planned test_a_ceiling_above_its_bound_is_refused_at_the_write)
- [ ] A stored ceiling above its hard limit is clamped at use
      (planned test_a_stored_ceiling_above_its_bound_is_clamped_at_use)
- [ ] The run carries the timeout and memory options
      (planned test_the_run_carries_timeout_and_memory_options)
- [ ] A run stopped at the charge ceiling is read and screened
      (planned test_a_run_stopped_at_the_charge_ceiling_is_read_and_screened)
- [ ] An aborted run below the ceiling writes nothing
      (planned test_an_aborted_run_below_the_ceiling_writes_nothing)
- [ ] A local wait timeout aborts the remote run
      (planned test_a_local_wait_timeout_aborts_the_remote_run)
- [ ] The summary prices charged events from the run object
      (planned test_the_summary_prices_charged_events_from_the_run_object)
- [ ] A missing token or actor variable refuses the run before any request
      (planned test_a_missing_actor_variable_refuses_before_billing)
- [ ] A failed actor run writes nothing and leaves seen state unchanged
      (planned test_a_failed_run_writes_nothing)

Policy version and reconsider

- [ ] An academic decision carries the entry's policy version
      (planned test_an_academic_decision_carries_the_profiles_policy_version)
- [ ] Editing a deciding field moves the academic version
      (planned test_editing_a_deciding_profile_field_moves_the_academic_version)
- [ ] Editing a labelling field does not move it
      (planned test_editing_a_labelling_field_does_not_move_it)
- [ ] Editing the industry configuration does not move it
      (planned test_editing_the_industry_configuration_does_not_move_the_academic_version)
- [ ] Activating a learned model does not move it
      (planned test_activating_a_learned_model_does_not_move_the_academic_version)
- [ ] An edited exclusion reopens the rejections it caused
      (planned test_an_edited_exclusion_reopens_the_rejections_it_caused)
- [ ] `reconsider` on an academic track clears only its own stale
      rejections (planned test_reconsider_on_an_academic_track_clears_only_its_own_stale_rejections)
- [ ] `reconsider` never reopens a human rejection on an academic track
      (planned test_reconsider_never_reopens_a_human_rejection_on_an_academic_track)

Dry runs, shadow runs and replays

- [ ] `--shadow` on an academic track makes no request and prints the
      compiled input
      (planned test_shadow_on_an_academic_track_makes_no_request_and_prints_the_compiled_input)
- [ ] `--shadow` reports a refused entry and exits 2
      (planned test_shadow_reports_a_refused_profile_and_exits_2)
- [ ] A dataset file replays without a request, the HTTP layer stubbed to
      raise (planned test_an_academic_dataset_file_replays_without_a_request)
- [ ] `--from-run` reads an existing dataset without starting a run
      (planned test_from_run_reads_an_existing_dataset_without_starting_a_run)
- [ ] A billed run saves its raw dataset before normalization
      (planned test_a_billed_run_saves_its_raw_dataset_before_normalization)
- [ ] A dry run writes no dataset file and prints its run id
      (planned test_a_dry_run_writes_no_dataset_file_and_prints_its_run_id)
- [ ] An academic dry run writes no row, seen file, summary or message
      (planned test_an_academic_dry_run_writes_no_row_seen_file_summary_or_message)
- [ ] A dry run followed by a real run judges the same postings
      (planned test_a_dry_run_then_a_real_run_judges_the_same_postings)
- [ ] A replayed dataset uses the same screening and seen state
      (planned test_a_replayed_dataset_uses_the_same_screening_and_seen_state)
- [ ] Saved runs beyond the cap are evicted by age
      (planned test_saved_runs_beyond_the_cap_are_evicted_by_age)
- [ ] A failed write after a successful run is finished from the saved run
      (planned test_a_failed_write_after_a_successful_run_is_finished_from_the_saved_run)

Window and records

- [ ] An entry without a posting-age window is refused
      (planned test_a_profile_without_a_posting_age_window_is_refused)
- [ ] The window is written into the source input
      (planned test_the_window_is_written_into_the_source_input)
- [ ] Each track records its own last successful discovery
      (planned test_each_track_records_its_own_last_successful_discovery)
- [ ] A gap longer than the window is named in the summary
      (planned test_a_gap_longer_than_the_window_is_named_in_the_summary)
- [ ] Items without a posting date are counted
      (planned test_items_without_a_posting_date_are_counted)
- [ ] The summary says results below the cap were not fetched
      (planned test_the_summary_says_results_below_the_cap_were_not_fetched)
- [ ] Each academic rejection records its gate and detail
      (planned test_each_academic_rejection_records_its_gate_and_detail)
- [ ] The summary keys rejections by gate
      (planned test_the_summary_keys_rejections_by_gate)
- [ ] Term hits name unused terms (planned test_term_hits_name_unused_terms)
- [ ] An academic dry run lists rejected postings with their reasons
      (planned test_a_dry_run_on_an_academic_track_lists_rejected_postings_with_reasons)
- [ ] A kept academic row records the terms that kept it in its notes
      (planned test_a_kept_academic_row_records_the_terms_that_kept_it_in_its_notes)
- [ ] An academic run leaves the default track's summary and health alone
      (planned test_an_academic_run_leaves_the_default_tracks_summary_and_health_alone)
- [ ] Schedule health names the academic job
      (planned test_schedule_health_names_the_academic_job)
- [ ] The academic Telegram summary shows deadlines and no terms
      (planned test_the_academic_telegram_summary_shows_deadlines_and_no_terms)
- [ ] `--configured-tracks` reads its slugs from the store
      (planned test_configured_tracks_reads_slugs_from_the_store)
- [ ] `--configured-tracks` runs each track in its own scope, never the
      default track, and reports an unknown, archived or industry slug
      while running the rest
      (planned test_configured_tracks_reports_an_unknown_archived_or_industry_slug_and_runs_the_rest)
- [ ] `--configured-tracks` leaves industry seen state untouched
      (planned test_configured_tracks_leave_industry_seen_state_untouched)

Components, flags and the deadline

- [ ] Optional fields are carried into metadata and notes
      (planned test_field_map_carries_optional_fields_into_metadata_and_notes)
- [ ] A missing optional field reads `not stated`
      (planned test_a_missing_optional_field_reads_not_stated)
- [ ] Each flag records its phrase and field
      (planned test_each_flag_records_its_phrase_and_field)
- [ ] A flag never rejects and never reorders the queue
      (planned test_a_flag_never_rejects_and_never_reorders_the_queue)
- [ ] An excluded position type is rejected, and an unlisted or empty one
      kept (planned test_an_excluded_position_type_is_rejected_and_an_unlisted_or_empty_one_kept)
- [ ] The summary counts postings per position type
      (planned test_the_summary_counts_postings_per_position_type)
- [ ] `next` and `review` print components without a number
      (planned test_next_and_review_print_components_without_a_number)
- [ ] Flag evidence with separators round-trips through notes
      (planned test_flag_evidence_with_separators_round_trips_through_notes)
- [ ] A deadline on the run date or the day before is kept
      (planned test_a_deadline_on_the_run_date_or_the_day_before_is_kept)
- [ ] A deadline two days before the run date is rejected
      (planned test_a_deadline_two_days_before_the_run_date_is_rejected)
- [ ] The run date is taken once per run, with the clock pinned
      (planned test_the_run_date_is_taken_once_per_run)
- [ ] An ambiguous numeric deadline is stored empty with its text
      (planned test_an_ambiguous_numeric_deadline_is_stored_empty_with_its_text)
- [ ] A month-name or unambiguous numeric deadline is converted
      (planned test_a_month_name_or_unambiguous_numeric_deadline_is_converted)
- [ ] A `deadline_passed` posting is judged again and kept when extended
      (planned test_a_deadline_passed_posting_is_judged_again_and_kept_when_extended)
- [ ] An unmappable item is skipped and counted
      (planned test_an_unmappable_item_is_skipped_and_counted)
- [ ] An unreadable deadline is kept empty and counted
      (planned test_an_unreadable_deadline_is_kept_empty_and_counted)

Dedupe and coverage

- [ ] Two calls with one title and different deadlines are both kept
      (planned test_two_calls_with_one_title_and_different_deadlines_are_both_kept)
- [ ] One call listed on two portals is kept once by its application link
      (planned test_one_call_listed_on_two_portals_is_kept_once_by_its_apply_link)
- [ ] Application links differing only in the query string are not merged
      (planned test_apply_links_differing_only_in_query_string_are_not_merged)
- [ ] An academic duplicate names the identity and the track that holds it
      (planned test_an_academic_duplicate_names_the_identity_and_the_track_that_holds_it)
- [ ] An organisation-and-title match with an empty deadline is still a
      duplicate
      (planned test_a_company_and_title_match_with_an_empty_deadline_is_still_a_duplicate)
- [ ] `services/api/tests/test_track_isolation.py` passes with no assertion
      edited
- [ ] The summary counts items per portal
      (planned test_the_summary_counts_items_per_portal)
- [ ] A listed portal with no items is named in the summary
      (planned test_a_listed_portal_with_no_items_is_named_in_the_summary)
- [ ] Unlisted portals are not reported as empty
      (planned test_unlisted_portals_are_not_reported_as_empty)

Fixture, demo and privacy

- [ ] Demo discovery screens the academic fixture through every gate, with
      pinned counts per reason
      (planned test_demo_discovery_screens_the_academic_fixture_through_every_gate)
- [ ] The academic demo makes no request and needs no keys
      (planned test_the_academic_demo_makes_no_request_and_needs_no_keys)
- [ ] `FIELD_MAP` reads the fixture shape and is not the identity
      (planned test_the_field_map_reads_the_fixture_shape)
- [ ] The deadline gate uses the run date, not the clock
      (planned test_the_deadline_gate_uses_the_run_date_not_the_clock)
- [ ] The existing fixture-host and provenance tests in
      `services/api/tests/test_demo.py` cover the new fixture
- [ ] The academic source never logs the actor or its input, captured at
      info level with sentinel values
      (planned test_the_academic_source_never_logs_the_actor_or_its_input)
- [ ] No item text reaches the summary or Telegram
      (planned test_no_item_text_reaches_the_summary_or_telegram)
- [ ] `config/academic-searches.example.json` is public and classified
      (`services/api/tests/test_classification_coverage.py` passes)

Schedule, governance and gates

- [ ] A calendar time with a weekday renders launchd's `Weekday` key, and
      every existing job renders as before
      (planned test_a_weekday_time_renders_the_weekday_key in
      `services/api/tests/test_schedule.py`)
- [ ] `CLAUDE.md` and `AGENTS.md` carry the scoped invariant, compiled by
      `aie sync`, and `aie check` passes
- [ ] `just contract` regenerates the contract with the token on the
      configuration reads, and the web app type-checks against it
- [ ] Each test above fails with its behavior removed, recorded in the
      pull request
- [ ] All gates green on the pull request

## Honest limitations

- **The ceilings depend on Apify.** `maxItems` has no effect on an actor
  priced per event, so the source's own result cap bounds a run and
  `maxTotalChargeUsd` is the backstop. Harrier cannot stop a charge the
  platform makes. The implementation confirms both against Apify's API
  documentation and the sample run, and says in the pull request only
  whether each was honoured.
- **The actor is a third party's.** Its input and output can change without
  notice. The field maps are code constants tested against the fixture, so
  a changed output shows up as skipped items in the summary, not as wrong
  rows, and the fix is a code change.
- **Coverage is the source's.** The source finds what the portals it reads
  carry. A position published only on an institution's own
  applicant-tracking board is not found, so a run that keeps nothing means
  "not found on the covered portals within the window", not "not posted".
  Portals that need a proxy cost extra, and the sample run checks that cost
  against the charge ceiling. Search terms apply to every selected country
  together, so a term added for one country's language widens the search in
  all of them.
- **No relevance ranking.** An academic row has no score, so the review
  queue for a large run is long. The entry's gates and the source's own
  search are the only filters. A score for the academic kind is a later
  spec.
- **Country scope is the source's.** Harrier does not re-check a location
  against `countries`, because the source's location text is not known to
  be machine-readable.
- **Compound words.** A term that sits inside a compound word without
  beginning it is not matched; the operator writes that word form as its
  own term.
- **Reconsider clears and does not re-screen** (spec 031). A cleared
  posting is judged again only when a later run, or a replay of a saved
  dataset, returns it.
- **Flags read an excerpt.** A restriction stated only in the full call is
  not flagged, and `not stated` means not stated in what the source
  returned. Components are only as good as the source's inferred fields,
  which is why they are shown and never used to reject.
- **Same-title calls.** Two distinct calls at one institution with the same
  title and an equal or missing deadline are still merged.
- **Per-track configuration is keyed inside one row's JSON,** not by a
  column. When `user_config` gains a track column (the spec that lifts the
  second-industry refusal, spec 093's open decision 6), this kind becomes
  one row per track in a one-time migration.

## Migration

None for data. After it ships:

1. Add `APIFY_TOKEN` (if absent) and `APIFY_ACADEMIC_ACTOR` to `.env`.
2. Write the entry from `config/academic-searches.example.json` and store it
   with `harrier config set academic_searches --file PATH`.
3. Run `harrier --track <slug> discover --shadow` and read the compiled
   input.
4. For the first run only, set `window_days` to about 60, raise
   `max_results` to the hard limit, and raise `max_charge_usd` to what the
   worst-case check then requires, within its hard limit, so positions
   already open are found. Run one `--dry-run`; it prints the run id.
5. Tune with `--dry-run --from-run <run id>` as often as needed, then write
   with `--from-run <run id>`.
6. Return to the steady values: `window_days` 14, `max_results` 200 and
   `max_charge_usd` 1.
7. Run `harrier schedule install`.

## Options weighed

- **Bypassing screening for the academic source.** Rejected: it would be a
  per-source path, which "Ingestion only" forbids, and it would lose the
  seen state and the cross-track dedupe.
- **Keeping the remote gate for every kind.** Rejected for the reason in
  Problem: it rejects most university and research positions.
- **Reusing the industry title rule.** Rejected: its compiled hints reject
  titles before the entry's lists are read, an empty include list rejects
  everything, and whole-word matching misses compound words.
- **Passing the actor input through unchanged, beside separate title
  lists.** Rejected: the search would be written twice, the lists would
  reject postings the source found by full text, and the source's own
  translation and memory could be switched on behind harrier's back.
- **A never-in-git file as the source of truth.** Rejected: ADR-009 keeps
  files as import paths.
- **One profile document per track for the search.** Rejected: documents
  are stored as they are, without validation, and spec 035 requires
  validated writes for anything that reaches a billed actor. The search is
  source configuration; the research profile is a separate concern (Out of
  scope).
- **A per-track configuration table.** Not needed while one row per kind
  holds a map.
- **Putting the actor's name in code as a default,** as the LinkedIn source
  does. Rejected: the name describes the kind of search, so it stays in
  `.env`.
- **Naming the track in `config/schedule.json`.** Rejected: that file is
  public, and a slug is the operator's own text (spec 094, Honest
  limitations).
- **An academic dry run that never bills.** Not adopted: `--dry-run` would
  mean two things. Replays make repeated tuning free instead.

## Open decisions for Akin

All answered on 2026-10-08:

1. **The search's home.** The `user_config` kind `academic_searches`,
   keyed by track slug, with no file fallback.
2. **The actor's field names.** Constants in the source module, not entry
   data.
3. **Later work.** The profile documents and the claims gate are separate
   specs, listed in Out of scope.
4. **The configuration reads.** They require the token; the contract is
   regenerated and spec 096 gets a dated note.
5. **The ceilings.** `max_results` 200 and `max_charge_usd` 1 per run as
   steady values. Hard limits in code: 500 results and 5 USD. The first
   run raises them for that run only (Migration).
6. **Cadence and window.** Weekly on Monday at 09:30, `window_days` 14; the
   first run uses about 60 days once.
7. **The hold list on the academic kind.** Not applied; an institution to
   skip is an `exclude` entry on `organisation`.
8. **A run from the browser.** Left to spec 095.
9. **Area terms.** They never reject by default (`require_area_match`
   false).
10. **Keyword translation.** Always compiled off; the operator writes the
    local variants.
11. **The academic dry run.** Billed, as on the industry track.
12. **A passed deadline.** Judged again on every run.
13. **Editing in the browser.** Spec 096 shows the kind read-only; an
    editor is later work.
14. **Portals.** An optional top-level `portals` field, validated against
    the source's `KNOWN_PORTALS`. Counts per portal stay in the summary
    even when `portals` is unset.

## Proof / origin

- Spec 093, Problem and Out of scope: the academic track is by hand only,
  and sources and scheduled discovery are handed on. Its guarantee "No
  profile read on an academic track" and its must-not list.
- The allowlist: `services/api/src/harrier/tracks.py`
  (`NON_DEFAULT_OPERATIONS`), specs 093 and 094.
- The screening gates and the order they run in:
  `services/api/src/harrier/screening/pipeline.py` (`screen_jobs`); the
  industry title rule and its compiled hints:
  `services/api/src/harrier/screening/rules.py` (`title_allowed`,
  `EXCLUDED_TITLE_HINTS`); the job shape and `normalize`:
  `services/api/src/harrier/screening/normalized.py`.
- The policy version: `services/api/src/harrier/screening/policy.py`;
  seen state: `services/api/src/harrier/screening/seen.py`; reconsider:
  `services/api/src/harrier/screening/reconsider.py`, spec 031.
- The discovery run, its summary and its success record:
  `services/api/src/harrier/discovery.py`,
  `services/api/src/harrier/runoutcome.py`, spec 029.
- Dedupe: `services/api/src/harrier/tracker/store.py` (`find_duplicate`,
  `all_tracks_dedupe_rows`), spec 092.
- The paid source pattern and its dataset-file replay:
  `services/api/src/harrier/sources/apify_linkedin.py`, spec 009.
- User configuration in the database: spec 023 and its open item on the
  configuration reads; `services/api/src/harrier/userconfig/store.py`;
  ADR-009. Validated writes before a billed actor and clamping at use:
  spec 035.
- The schedule: `config/schedule.json`,
  `services/api/src/harrier/schedule.py`, spec 020, ADR-006.
- The demo fixtures and their exact-URL index:
  `services/api/src/harrier/screening/http.py`, spec 021, ADR-008.
- The classification: `config/data-classification.json`, ADR-002 as
  revised by ADR-008.

## Out of scope

- Starting this run from the browser (spec 095 or later), and editing the
  search in the browser (spec 096 shows it read-only).
- A fit score or any ranking for academic rows.
- Artifacts, outreach and contact discovery on an academic track.
- A second industry track, or industry sources on any non-default track.
- More than one source per academic track.
- Later work, each its own spec:
  - Spec 098: resume content splits into shared facts and an industry
    framing, on the default track.
  - Spec 099: an academic track holds its own framing over the shared
    facts.
  - Spec 100: the truth gate reads every negation it is given.
  - Spec 101: academic application documents pass the truth and claims
    gates.
