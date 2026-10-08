---
spec: 097
title: An academic track finds new positions on its own, from a configured Apify source
status: proposed
approved: no
milestone: M9
depends: [009, 011, 020, 021, 022, 031, 052, 091, 092, 093, 094]
---

# Spec 097: An academic track finds new positions on its own, from a configured Apify source

## Problem

An academic track (for example university or research vacancies) is filled
by hand only. Spec 093 made that the whole of it: "There is no discovery,
no scoring, no artifact generation and no outreach on it". Its Out of scope
list hands on "Sources, discovery and importers for an academic track;
scheduled discovery across tracks".

Three things stand in the way today, and each is in the code:

- **`discover` refuses a non-default track.** It is not in
  `NON_DEFAULT_OPERATIONS` (`services/api/src/harrier/tracks.py`), so
  `harrier --track <slug> discover` exits 2.
- **Screening is industry policy.** `screen_jobs`
  (`services/api/src/harrier/screening/pipeline.py`) applies the title
  rules, the remote-only and region gate, and the fit score, all read from
  the industry candidate configuration (`config/candidate.json`). A
  university or research position is usually on site, so the remote gate
  would reject most of them, and the fit score measures the wrong thing.
- **No source returns a deadline.** `NormalizedJob`
  (`services/api/src/harrier/screening/normalized.py`) has no deadline
  field, and the academic queue is ordered by the deadline (spec 093).

The operator wants positions from an academic job aggregator that runs as
an actor on Apify, the paid platform the LinkedIn source already uses
(spec 009). Such an actor charges per run and per result, so an unbounded
run costs real money, and a schedule repeats that cost.

## Scope

**Domain** (`services/api/src/harrier/`)

- `sources/apify_academic.py` (new): ingestion only. It starts the actor
  named in the environment with the input from the track's configuration,
  waits for the run, reads the dataset, and normalizes each item into the
  shared job shape. It filters, scores and writes nothing.
- `screening/normalized.py`: `NormalizedJob` gains `deadline`, an ISO date
  or the empty string. Every existing source returns it empty.
- `tracks.py`: `KIND_RULES` gains the screening each kind applies (see
  Behavior). `discover` joins `NON_DEFAULT_OPERATIONS` and
  `WRITE_OPERATIONS`.
- `screening/pipeline.py`: `screen_jobs` takes the gates from the scope's
  kind. The industry kind applies exactly today's gates.
- `discovery.py`: on a non-default track, `run_discovery` runs only the
  sources configured for that track, never the industry sources.
- `screening/seen.py`: seen state is kept per track and source, so one
  track's decision never suppresses a posting for another.
- `schedule.py`: a calendar time may name a `weekday` (1 for Monday to 7
  for Sunday), rendered as launchd's `Weekday` key. A time without one runs
  every day, as today.

**Command line** (`services/api/src/harrier_cli/main.py`)

- `harrier --track <slug> discover [--dry-run] [--shadow] [--no-notify]`.
- `harrier discover --scheduled --configured-tracks`: one discovery per
  live track named in the configuration, each in its own scope. It never
  runs the default track.

**Configuration**

- `config/academic-discovery.json` (new, never-in-git) and
  `config/academic-discovery.example.json` (new, public, placeholder
  values only). Classified in `config/data-classification.json`, a guarded
  path approved under this spec.
- `.env.example`: `APIFY_ACADEMIC_ACTOR=` with no value. The actor's name
  lives only in `.env`; no default is written into the code.
- `config/schedule.json`: a new `academic-discovery` job (see Behavior).

**Governance**

- The product invariant "Remote-only and EMEA scope are enforced" is
  scoped to the industry kind, in its source under `.ai/`, compiled by
  `aie sync` into `CLAUDE.md` and `AGENTS.md`. The guarded governance edit
  is approved under this spec. See Behavior, "The invariant".

**Tests and fixtures**

- `services/api/tests/test_academic_discovery.py` (new).
- A synthetic dataset under `fixtures/` in the actor's output shape, with
  invented institutions and positions (ADR-008), served offline in demo
  mode as the other fixtures are (spec 021).

**Not touched**: the industry sources and their gates, the fit score, the
browser (starting this run from the browser is spec 095's or later), the
run manager's run kinds, and artifacts or outreach on any track.

## Behavior

### The configuration

`config/academic-discovery.json` holds one entry per track:

| Field | Meaning |
|---|---|
| `track` | the slug of a live academic track |
| `actor_input` | the object passed to the actor unchanged, as its own input schema defines it |
| `max_results` | the most results one run may produce, passed to Apify as the run's `maxItems` |
| `max_charge_usd` | the most one run may cost, passed to Apify as the run's `maxTotalChargeUsd` |
| `title_include` | optional list; when set, a title must contain one of them |
| `title_exclude` | optional list; a title containing one is rejected |
| `field_map` | the actor's output field for each shared field the source reads |

`max_results` and `max_charge_usd` are required. An entry without both is
refused before any run starts, naming the field, because the actor's own
defaults allow runs far larger than a daily driver should buy.

The track slug, the actor input (search terms, countries) and the title
lists describe the operator's own search, so the file is never-in-git like
`config/feeds.txt` and `config/linkedin_search_urls.txt`.

`field_map` exists because the actor publishes no output schema. Before
implementation, one sample run with a small `max_results` shows the output
field names. The example file and the fixture follow that shape; no field
name is guessed in this spec.

### What a run does

`harrier --track <slug> discover`:

1. Resolves the track once (spec 092). An unknown slug exits 2 naming it.
   An archived track exits 2 naming it, since `discover` writes.
2. Finds the track's entry in the configuration. A track with no entry
   exits 2 with "no sources are configured for track <slug>". No industry
   source runs on a non-default track.
3. Starts the actor with `actor_input`, `maxItems` and `maxTotalChargeUsd`,
   polls until a terminal status, and reads the dataset, as
   `sources/apify_linkedin.py` does. `APIFY_TOKEN` and
   `APIFY_ACADEMIC_ACTOR` come from the environment.
4. Normalizes each item into `NormalizedJob` through `field_map`. An item
   with no title or no URL is skipped and counted. A deadline that is not
   an ISO date, or a date the source can convert to one, is stored empty
   and counted.
5. Screens the jobs through `screen_jobs` with the academic kind's gates.
6. Appends each kept job through `add_job` with the track's scope, so it
   lands as a `prospect` with the kind's next action and its deadline, and
   with no `fit_score`, `score`, `signals`, `scoring_version` or
   `remote_filter`, as spec 093's hand-added rows have.
7. Prints the summary and, unless `--no-notify`, sends the Telegram summary
   naming the track's label. Telegram is the one outbound channel allowed.

`--dry-run` screens and writes nothing, and still starts a billed run, as
industry `--dry-run` does. `--shadow` runs no paid source (spec 022), so on
an academic track it fetches nothing and says so.

### The gates per kind

`KIND_RULES` names the gates. `screen_jobs` stays the one screening path;
the kind decides which of its gates apply.

| Gate | Industry (unchanged) | Academic |
|---|---|---|
| seen state (spec 031) | yes, per source | yes, per track and source |
| company hold list (spec 052) | yes | no: the hold list is the industry watchlist's |
| title rules | from `config/candidate.json` | `title_include` and `title_exclude` |
| remote-only and region | yes | no: the actor input sets the countries |
| passed deadline | no | rejected as `deadline_passed`, recorded in seen state |
| dedupe against every track (spec 092) | yes | yes |
| fit score and cutoff | yes | no |

A job with no deadline is kept: the queue puts it after the dated ones
(spec 093).

### The invariant

`CLAUDE.md` today says: "Remote-only and EMEA scope are enforced. Hybrid
and onsite are rejected on location signals". After this change it applies
to the industry kind, and the academic kind's region scope is the country
list in its actor input. The rest of the invariant (the location-field
scoping, the EU-permit phrases as positive signals) is unchanged. Changing
a product invariant needs an explicit spec; this is that spec.

"Ingestion only" is unchanged: the new source only fetches and normalizes,
and every gate runs in the shared screening path.

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
from the never-in-git configuration. Each track runs in turn; one track's
failure is reported and the next still runs. The cadence is an open
decision below. A weekday in a calendar time is new: `harrier schedule
install` renders hours and minutes only today
(`services/api/src/harrier/schedule.py`, spec 020).

## Failure modes

- **No `APIFY_TOKEN` or no `APIFY_ACADEMIC_ACTOR`.** The run is refused
  before any request, naming the missing variable; nothing is billed.
- **The actor run fails, aborts or times out.** The summary names the
  status. Nothing is written and seen state is unchanged, so the next run
  judges the same postings.
- **The run stops at `max_results` or `max_charge_usd`.** The items already
  produced are screened and written; the summary says the run stopped at
  its ceiling.
- **An item the field map cannot read.** Skipped and counted, never
  written half-filled.
- **A track in the configuration that is unknown or archived.** That entry
  is reported; with `--configured-tracks` the others still run.
- **The same posting found for two tracks.** The second is a tracker
  duplicate naming the track that holds it (spec 092).

Must not introduce: an industry source running on a non-default track; a
fit score on an academic row; the actor's name, the operator's search
terms or a track slug in a committed file; a run without both ceilings; a
second screening path.

## Acceptance criteria

Tests in `services/api/tests/test_academic_discovery.py` unless named
otherwise. Every database is built under `tmp_path`; every actor response
is the synthetic fixture, served without network.

- [ ] `harrier --track <slug> discover` runs the configured source and no
      industry source, and adds the kept positions to that track with
      their deadlines (planned test_discover_on_an_academic_track_runs_only_its_sources)
- [ ] An academic row from discovery carries no score fields and the
      kind's next action (planned test_a_discovered_academic_row_has_no_score)
- [ ] The academic gates reject a passed deadline and the title
      exclusions, keep a position with no deadline, and never apply the
      remote gate (planned test_the_academic_kind_applies_its_own_gates)
- [ ] The industry kind's screening is unchanged:
      `services/api/tests/test_screening.py` passes with no assertion
      edited
- [ ] Seen state for one track never suppresses a posting for another
      (planned test_seen_state_is_kept_per_track)
- [ ] The run is started with `maxItems` and `maxTotalChargeUsd` from the
      configuration, and an entry missing either is refused before any
      request (planned test_every_run_carries_both_ceilings)
- [ ] A missing token or actor variable refuses the run before any request
      (planned test_a_missing_actor_variable_refuses_before_billing)
- [ ] A failed actor run writes nothing and leaves seen state unchanged
      (planned test_a_failed_run_writes_nothing)
- [ ] A track with no configuration, an unknown slug and an archived track
      each exit 2 naming the track (planned test_discover_refuses_an_unconfigured_track)
- [ ] `--configured-tracks` runs every configured track in its own scope,
      never the default track, and reports one track's failure while
      running the rest (planned test_configured_tracks_run_each_in_its_own_scope)
- [ ] A calendar time with a weekday renders launchd's `Weekday` key, and
      every existing job renders as before
      (planned test_a_weekday_time_renders_the_weekday_key in
      `services/api/tests/test_schedule.py`)
- [ ] `config/academic-discovery.json` is never-in-git and its example is
      public (`services/api/tests/test_classification_coverage.py` passes),
      and no committed file names the actor
- [ ] `CLAUDE.md` and `AGENTS.md` carry the scoped invariant, compiled by
      `aie sync`, and `aie check` passes
- [ ] Each test above fails with its behavior removed, recorded in the
      pull request
- [ ] All gates green on the pull request

## Honest limitations

- **The ceilings depend on Apify honoring them.** `maxItems` and
  `maxTotalChargeUsd` are Apify's run options; harrier cannot stop a charge
  the platform makes. The implementation confirms both against Apify's API
  documentation and the sample run, and says so in the pull request.
- **The actor is a third party's.** Its input and output can change
  without notice. `field_map` keeps that change in configuration, and a
  changed output shows up as skipped items in the summary, not as wrong
  rows.
- **No relevance ranking.** An academic row has no score, so the review
  queue for a large run is long. The title lists and the actor's own
  keyword search are the only filters. A score for the academic kind is a
  later spec.
- **Country scope is the actor's.** Harrier does not re-check a location
  against a country list, because the actor's location text is not known
  to be machine-readable.

## Migration

None for data. After it ships: add the two variables to `.env`, write
`config/academic-discovery.json` from the example, run one
`harrier --track <slug> discover --dry-run` to see the result before any
write, then `harrier schedule install`.

## Options weighed

- **Bypassing screening for the academic source.** Rejected: it would be a
  per-source path, which "Ingestion only" forbids, and it would lose the
  seen state and the cross-track dedupe.
- **Keeping the remote gate for every kind.** Rejected for the reason in
  Problem: it rejects most university and research positions.
- **Putting the actor's name in code as a default,** as the LinkedIn source
  does. Rejected: the name describes the kind of search, so it stays in
  `.env`.
- **Naming the track in `config/schedule.json`.** Rejected: that file is
  public, and a slug is the operator's own text (spec 094, Honest
  limitations).

## Open decisions for Akin

1. **The ceilings.** Recommendation: `max_results` 200 and
   `max_charge_usd` 1 per run, raised after the sample run shows the real
   volume.
2. **The cadence.** Recommendation: once a week, which matches how slowly
   such postings change; the trigger above is Monday 09:30.
3. **The hold list on the academic kind.** Recommendation: not applied, as
   in the table; an institution to skip goes in `title_exclude` or the
   actor input.
4. **A run from the browser.** Recommendation: leave it to spec 095, which
   already covers the commands the browser cannot run yet.

## Proof / origin

- Spec 093, Problem and Out of scope: the academic track is by hand only,
  and sources and scheduled discovery are handed on.
- The allowlist: `services/api/src/harrier/tracks.py`
  (`NON_DEFAULT_OPERATIONS`), specs 093 and 094.
- The screening gates: `services/api/src/harrier/screening/pipeline.py`
  (`screen_jobs`); the job shape:
  `services/api/src/harrier/screening/normalized.py` (`NormalizedJob`).
- The paid source pattern: `services/api/src/harrier/sources/apify_linkedin.py`,
  spec 009.
- The schedule: `config/schedule.json`, spec 020, ADR-006.
- The classification: `config/data-classification.json`, ADR-002 as
  revised by ADR-008.

## Out of scope

- Starting this run from the browser (spec 095 or later).
- A fit score or any ranking for academic rows.
- Artifacts, outreach and contact discovery on an academic track.
- A second industry track, or industry sources on any non-default track.
- More than one source per academic track.
