---
spec: 093
title: An academic track can be added and its positions tracked by hand
status: accepted
approved: yes
milestone: M9
depends: [010, 027, 074, 075, 092]
---

# Spec 093: An academic track can be added and its positions tracked by hand

## Problem

After spec 092 every reader reads one track, and there is still only one.
No command creates a second track, and no command can say which track it
means, so the column spec 091 added holds one value and the predicates
spec 092 added are exercised only by tests. Spec 041's bar ("a column that
is always `default` blocks nothing and proves nothing") is met only when a
second track exists and a person can work in it.

The second track kind this milestone adds is an academic one (for example
university or research vacancies), tracked by hand. "By hand" is the whole
of it: positions are entered with `harrier add`, moved with the status
verbs, listed with `next` and `review`, and exported. There is no
discovery, no scoring, no artifact generation and no outreach on it, because
each of those is industry code that reads the industry profile, and none of
it applies. What an academic position needs that an industry posting does
not is a deadline: the queue is ordered by it, not by a fit score, and a
passed deadline is the fact that matters most about a row.

The industry track must not change. Every command without a `--track` flag
behaves as it does today, and the schedule never names a track.

## Scope

- `services/api/src/harrier_cli/main.py`: the global `--track` flag, the
  `tracks add` and `tracks archive` commands and their `COMMAND_CLASSES`
  entries, the allowlist, and `--deadline` on `add`.
- `services/api/src/harrier/tracks.py`: `add_track`, `archive_track`, the
  per-kind rules table (`KIND_RULES`: status labels, next-action defaults,
  whether `applied` seeds a follow-up, the queue order).
- `services/api/src/harrier/tracker/schema.py`: migration 9 (a guarded
  path; approved under this spec).
- `services/api/src/harrier/tracker/store.py`: `set_status` and `add_job`
  consult the scope's kind through `KIND_RULES`.
- `services/api/src/harrier/tracker/queue.py`: the academic kind's order.
- `services/api/src/harrier/capture.py`: the academic `add` path.
- `services/api/src/harrier/tracker/export.py`: the destination for a
  non-default track.
- `services/api/src/harrier_api/runs.py`: `RunParams.track` and its place
  in `build_command`.
- `.ai/rules/product-invariants.md`: the status-lifecycle sentence, quoted
  under Behavior; edited on the implementing branch followed by
  `npx aie sync`, which regenerates `CLAUDE.md`, `AGENTS.md` and
  `.claude/` (generated files are never hand-edited).
- Tests under `services/api/tests/` (`test_tracks_cli.py`, new, plus
  additions to `test_delegation.py` and `test_runs.py`).
- README: the operator section gains the `--track` flag and the allowlist.

Not touched: the API contract and every route's shape (no route takes a
track yet; `RunParams.track` is reachable only from code until the API spec
lands), `apps/web`, the schedule and plists, the classification table.

## Behavior

### The flag

`harrier --track <slug> <subcommand> ...`. The flag belongs to the top-level
parser and sits before the subcommand. Its value is checked against the
slug rule by the parser (`validate_slug`, spec 091), so a malformed slug is
a usage error with exit 2 before anything else runs. After the connection
is opened, the slug is resolved with `resolve_scope`; an unknown slug exits
2 with a message naming the slug and `harrier tracks list`. `--track job`,
the default track's slug, is the same as no flag.

**Delegation forwards it unchanged.** The host CLI hands the whole argument
vector to the container (`services/api/src/harrier/delegate.py:108-134`,
spec 074), and the flag is part of it. `subcommand_name` is built from the
parser's destinations and does not include the flag's value, so the lease
(spec 075), the delegation line and every refusal printed by the host
process stay free of it, as they are of every argument value.

**The run manager places it before the verb.** `RunParams` gains
`track: str | None`, validated by the slug rule in `__post_init__`;
`build_command` emits `--track <slug>` between the interpreter's module
and the verb (`services/api/src/harrier_api/runs.py:159-193`). The API
route that will pass it (a later spec) checks the slug exists against
`tracks` before building the params. In this spec nothing passes it; the
plumbing is proven by its own test so the later spec is a route change and
not a run-manager change.

### `tracks add` and `tracks archive`

- `harrier tracks add <slug> --kind academic --label <label>` inserts a
  track and prints its line as `tracks list` would. `--kind industry` is
  refused with exit 2: a second industry track would share the one
  candidate configuration and the one watchlist, which is the same search
  twice (see Open decisions). A slug already in use is refused with exit 1
  naming the slug. `--label` is required and is display text.
- `harrier tracks archive <slug>` sets `archived_at`. The default track is
  refused (exit 2); an archived track is refused (exit 1). An archived
  track still lists, still reads, and refuses writes: `add` and the status
  verbs on it exit 2 naming the track as archived. Nothing unarchives;
  that is a later verb if anyone needs it.
- Both are `database` class commands (`tracks add`, `tracks archive` in
  `COMMAND_CLASSES`).

### The allowlist

With a non-default `--track`, only these commands run:

`add`, `tracks list`, `tracks add`, `tracks archive`, `next`, `review`,
`shortlist`, `track`, `applied`, `interviewing`, `reject`, `events show`,
`export`.

Every other command refuses with exit 2 and a message naming the
subcommand and the track slug, including `discover`, `reevaluate`,
`evaluate-prospects`, `scoring export`, `scoring train`, `reconsider`,
`find-contacts`, and the artifact commands `tailor`, `cover-letter`,
`answers`, `evaluate`, `outreach-draft`. The refusal is printed by the
process that would run the command (the child, when delegated), never by
the delegating host process, which prints the subcommand name only (spec
074). The refusal is decided after the scope is resolved and before
anything reads a row.

Without the flag every command behaves as today.

### Migration 9

Applied by spec 090's runner:

```sql
ALTER TABLE jobs ADD COLUMN deadline TEXT NOT NULL DEFAULT ''
    CHECK (
        deadline = ''
        OR (length(deadline) = 10
            AND deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
    );
```

An empty string means no deadline. SQLite tests an added CHECK against
every existing row; every existing row has the default, so the migration
passes on a populated database. `deadline` is not in `TRACKER_FIELDS` and
not in the CSV export (see Open decisions); it is a column the queue and
the two read verbs show.

### `add` on an academic track

`harrier --track <slug> add --company ... --title ... [--url ...]
[--location ...] [--description ...] [--deadline YYYY-MM-DD]`.

- `--deadline` is parsed by the same `_iso_date` type `--applied-date`
  uses, so a malformed date is a usage error with exit 2
  (`services/api/tests/test_tracker_cli.py::test_a_malformed_applied_date_is_refused_by_the_parser`
  is the shape). It is accepted on any track; on the industry track it is
  stored and shown and does not reorder the queue.
- On an academic track `add_captured_job` does not load the candidate
  configuration, does not call `fit_score_for`, does not enrich the
  description over the network, and stores no `fit_score`, `score`,
  `signals`, `scoring_version` or `remote_filter`. The row is a `prospect`
  with the given fields, `source` `manual` unless given, the
  `manual_added` note as today, and the deadline. The pasted description
  is cached as today, so the `created` event can pin its hash (spec 079);
  that event carries an empty score and version.
- Dedupe is spec 092's: a url already in any track is refused naming it.

### Per-kind labels, next actions and the follow-up

`KIND_RULES` in `harrier.tracks` holds, per kind: a display label for each
of the six statuses, a next-action default for each, whether `applied`
seeds the follow-up date and the outreach block, and the queue order. The
industry entry reproduces today's `NEXT_ACTION_DEFAULTS` and today's
seeding (`services/api/src/harrier/tracker/store.py:276-285`), so the
industry track is unchanged by construction and
`services/api/tests/test_tracker_cli.py::test_applied_seeds_the_outreach_block_and_the_follow_up`
keeps passing.

The academic entry: labels that read as an application to a call rather
than to a job posting (`tailored_cv_requested` reads as "preparing
documents", `applied` as "submitted", `rejected` as "closed"); next-action
defaults to match; `applied` sets `applied_date` and nothing else: no
follow-up date, no outreach block, because outreach is not a thing this
track does. The raw status in the row, in the API and in the CSV is the
same six-value lifecycle; the label is display text in the CLI.

`set_status` reads the kind from the scope (spec 092 gives it one) and
applies that kind's entry. The transitions (spec 036) are shared.

### The academic queue

For an academic track, `next` and `review` order rows by nearest open
deadline first (deadline on or after today, ascending), then rows with no
deadline, then rows whose deadline has passed, in that order; within a
group by stage priority and then id. A passed deadline is shown with a
`deadline passed` flag and sunk to the bottom, never hidden: a row the
operator forgot about is the row that most needs to be seen once. "Today"
is a parameter of the ranking function so a test can pin it.

The industry queue is unchanged.

### No profile read on an academic track

No command in the allowlist, run on an academic track, reads
`profile_documents` or `user_config`. `add` skips the candidate
configuration; the status verbs, the queue, `events show`, `export` and
the `tracks` commands never read them. Proven with a trace callback on the
connection that fails the test on any statement naming either table.

One read stays, and it is not a command's: logging setup loads the
candidate's identity values from the one `resume_data` document to build
the redaction filter (`services/api/src/harrier/logredact.py:48-70`),
before any command runs, on every track. That is redaction, it is the one
profile read shared by design, and it is named here rather than hidden
behind the sentence above.

### Export

`harrier --track <slug> export [--dest D]` writes `D/<slug>/jobs.csv` in
the twenty-column shape and writes no contacts file, because contacts are
the person's and not a track's. Without the flag the export is unchanged:
`D/jobs.csv` and `D/contacts.csv`. `export` stays a `host-path` command.

### Browser capture

`POST /capture/add` and `POST /tracker` (the browser's manual add) land in
the default track until the API and web spec gives them a track. They
resolve `default_scope` through `get_scope` (spec 092).

### The invariant

`.ai/rules/product-invariants.md`, lines 8 to 10 at cd5665d, reads:

> - **One tracker, one write path.** The tracker is the single source of truth for
>   application state. Status lifecycle: prospect, shortlisted, tailored_cv_requested,
>   applied, interviewing, rejected. The outreach status axis is orthogonal.

The implementing branch changes it to:

> - **One tracker, one write path.** The tracker is the single source of truth for
>   application state. Every row belongs to one search track (spec 091, ADR-012).
>   The status lifecycle is prospect, shortlisted, tailored_cv_requested, applied,
>   interviewing, rejected, shared by every track kind; a kind supplies its own
>   labels and next-action defaults (spec 093), never its own statuses. A kind that
>   needs a stage the six cannot express changes this invariant by spec. The
>   outreach status axis is orthogonal.

Then `npx aie sync`, and `npx aie check` proves no drift.

## Failure modes

- **A malformed `--track` value.** Exit 2 from the parser.
- **An unknown slug.** Exit 2, naming the slug and `harrier tracks list`.
- **A command outside the allowlist on a non-default track.** Exit 2,
  naming the subcommand and the slug. Nothing is read or written.
- **`tracks add --kind industry`.** Exit 2 naming this spec's reason.
- **A slug already in use, or archiving an archived or default track.**
  Exit 1 or 2 as above, nothing written.
- **A write on an archived track.** Exit 2 naming the track as archived.
- **A malformed `--deadline`.** Exit 2 from the parser. A well-formed
  date that is not a real date (the thirtieth of February) is refused by
  `_iso_date` the same way.
- **A deadline written by a path other than the parser** (a hand edit, a
  restore). The CHECK refuses anything but an empty string or a ten
  character ISO date; it does not check that the date exists.
- **A container running an image from before this spec.** The delegated
  child's parser does not know `--track` and exits 2 with its usage
  error. `just container-up` is the fix, as for any stale image (spec
  074).
- **The schedule.** Never names a track; every scheduled command resolves
  the default scope and is outside the allowlist anyway.

Must not introduce: a change to the industry track's behavior without the
flag; a status added, renamed or removed; a profile or configuration read
on an academic track by any allowed command; a score, model or cutoff on
an academic track; a second industry track; an argument value in the
delegating host process's output or in the lease; a route or contract
change; a `deadline` column in the CSV export; a track named by the
schedule.

## Acceptance criteria

Tests in `services/api/tests/test_tracks_cli.py` unless named otherwise.
Every database is built under `tmp_path`; every row and every track is
synthetic.

- [ ] `harrier --track x shortlist 1` with the container owning the
      database is delegated as the identical vector, flag included, and
      the host process's output and the lease name neither `x` nor the id
      (`services/api/tests/test_delegation.py`,
      planned test_track_flag_survives_delegation)
- [ ] `RunParams(track="x")` makes `build_command` emit `--track x` before
      the verb; a malformed slug is refused by `RunParams`
      (`services/api/tests/test_runs.py`,
      planned test_run_manager_places_a_validated_track_before_the_verb)
- [ ] `tracks add` creates an academic track that `tracks list` shows, and
      `add` with its slug lands the row in it
      (planned test_add_lands_in_the_named_track)
- [ ] An academic `add` stores no score, signals, version or remote
      filter, loads no candidate configuration and makes no network
      request; the same `add` on the industry track is scored as today
      (planned test_an_academic_add_stores_no_industry_score)
- [ ] `applied` on an academic row sets `applied_date` and no follow-up
      date, next action or outreach block; on an industry row it seeds
      all of them
      (planned test_marking_an_academic_row_applied_seeds_no_follow_up;
      `services/api/tests/test_tracker_cli.py::test_applied_seeds_the_outreach_block_and_the_follow_up`)
- [ ] The academic queue orders by nearest open deadline, then no
      deadline, then passed deadlines flagged and last, and hides none of
      them; the industry queue is unchanged
      (planned test_academic_queue_orders_by_nearest_open_deadline;
      `services/api/tests/test_tracker_cli.py::test_rank_puts_the_nearest_to_sending_first`)
- [ ] Every command outside the allowlist, parametrized over the list
      under Behavior, exits 2 naming the subcommand and the slug and reads
      no row; every command inside it runs
      (planned test_commands_outside_the_allowlist_refuse_a_non_default_track)
- [ ] `tracks add --kind industry` exits 2 and writes nothing
      (planned test_tracks_add_refuses_a_second_industry_track)
- [ ] A malformed or impossible `--deadline` exits 2, and the CHECK refuses
      a raw write of a malformed deadline
      (planned test_a_malformed_deadline_is_refused)
- [ ] A duplicate slug, archiving the default track, archiving twice, and
      a write on an archived track are each refused and write nothing
      (planned test_track_lifecycle_refusals)
- [ ] `--track job` behaves as no flag
      (planned test_the_default_tracks_slug_is_the_same_as_no_flag)
- [ ] Status labels and next-action defaults follow the kind, and the raw
      status is the same six-value lifecycle in both
      (planned test_status_labels_follow_the_track_kind)
- [ ] No allowed command on an academic track issues a statement naming
      `profile_documents` or `user_config`, proven with a trace callback
      (planned test_academic_commands_read_no_profile_document)
- [ ] `export` on a non-default track writes `D/<slug>/jobs.csv` and no
      contacts file; without the flag it writes the two files it writes
      today (planned test_export_on_a_non_default_track_writes_under_its_slug)
- [ ] Browser capture and the API manual add land in the default track
      (planned test_browser_capture_lands_in_the_default_track)
- [ ] `tracks add` and `tracks archive` are `database` class
      (`services/api/tests/test_delegation.py::test_every_subcommand_has_exactly_one_class`)
- [ ] `services/api/tests/test_scoring.py::test_a_migrated_database_matches_a_fresh_one`
      covers migration 9
- [ ] The invariant text is amended as quoted and `npx aie check` passes
- [ ] The contract is regenerated by `just contract` and shows no diff
- [ ] Each test above fails with its behavior removed, checked by removing
      each behavior in turn and recorded in the pull request
- [ ] No real position, institution, person, deadline or tracker count
      appears in a fixture, this spec or a commit message (ADR-008)
- [ ] All gates green on the pull request

## Honest limitations

- **By hand only.** No source, no discovery, no scoring, no artifacts, no
  outreach on an academic track. Each is a later spec, and each has to
  pass the truth and claims gates before it writes anything a committee
  reads.
- **No profile at all on an academic track.** Until per-track documents
  land, nothing on it reads a profile document, which is the right
  default and also means nothing on it can generate anything.
- **The deadline is one date.** Calls have several (expression of
  interest, references, the application itself); this spec stores the one
  the operator chose to enter. Opportunity modelling is spec 091's out of
  scope list.
- **Labels are display text in the CLI.** The API and the browser show
  the raw status until the web spec.
- **Archiving is one-way** here.
- **The run manager's track is plumbing without a caller.** It is tested
  and unused until the API spec.
- **Redaction reads one profile document on every track** (see "No
  profile read").
- **The export destination is resolved as today**, relative to the working
  directory (`Path(args.dest)`, default `tracker`, at
  `services/api/src/harrier_cli/main.py:198` and `:1815`). Run from
  `services/api`, that is an unignored path, and this spec adds a
  slug-named folder under it. Found in review of this spec; anchoring the
  destination to the repository root is a pre-existing defect with its own
  spec, not this one.

## Migration

For the operator: `just container-up`, then any command applies migration
9. `harrier tracks add <slug> --kind academic --label <label>` creates the
track; `harrier --track <slug> add ...` enters the first position. Nothing
else changes; the schedule does not need reinstalling.

## Options weighed

- **A per-track subcommand group** (`harrier tracks run <slug> add ...`)
  instead of a global flag. Rejected: every verb would need a second
  spelling, and the run manager's closed argv sets would double.
- **An environment variable for the track.** Rejected: launchd plists set
  no environment (`services/api/src/harrier/schedule.py:255-284`), the
  container does, and a value that travels invisibly is how a scheduled
  run would one day land in the wrong track.
- **Refusing commands by a denylist** instead of an allowlist. Rejected: a
  new command would be allowed on an academic track by default, and the
  profile-read guarantee would be a promise about code not yet written.
- **Per-kind statuses now.** Rejected for this milestone: the six statuses
  describe an application on any track, and a kind-specific lifecycle is
  an invariant change with no row yet that needs it. The amended invariant
  says how one would be made when it does (Open decisions, item 7).
- **Scoring academic rows with the rules.** Rejected: the rules are the
  industry candidate configuration's, and a score with no basis is worse
  than no score.

## Open decisions for Akin

1. **A rules-only academic track.** Unconfirmed. Recommendation: the
   academic track stores no score and uses no model or cutoff (as written
   here), and the industry model never trains on its labels (spec 092). A
   rules-only academic score, if one is ever wanted, is a later spec with
   its own rules.
2. **`company-outcome` on a non-default track.** Not in the allowlist as
   given. Recommendation: add it, since it is how a committee's reply is
   recorded and it reads no profile.
3. **`events backfill` on a non-default track.** Recommendation: refuse
   it here; the reconstruction rules are the industry kind's (spec 092),
   and an academic track created after spec 079 has no eventless rows.
4. **The export layout** for a non-default track, `D/<slug>/jobs.csv`
   with no contacts file. Recommendation: as written.
5. **`deadline` in the CSV export.** Recommendation: not in this spec; the
   twenty-column shape is ADR-003's contract and a per-kind export shape
   is its own change.
6. **Lifting the second-industry-track refusal.** Recommendation: a later
   spec, after per-track profile documents and configuration land, when a
   second industry track would have its own watchlist and candidate
   configuration rather than the first track's.
7. **A lifecycle per kind, long term.** Recommendation: keep the six
   statuses shared until a kind has a row the six cannot describe; then a
   per-kind lifecycle table in code, the `jobs.status` CHECK widened by
   migration, and the invariant amended again by that spec. The amended
   text above names that path so the next change is a spec and not an
   argument.
8. **Archived tracks refuse writes and allow reads.** Recommendation: as
   written.

## Proof / origin

- The tracker verbs, `add`, `next` and `review`: spec 027;
  `services/api/src/harrier_cli/main.py` (`add` at lines 2129 to 2136,
  the verbs at 2059 to 2067, `_cmd_tracker_verb` at 902); the existing
  `track` verb at `harrier/tracker/actions.py:51` and
  `apps/web/src/features/tracker/JobActions.tsx:21`, which stays as it
  is: the new commands use the group name `tracks`.
- Manual entry's scoring path: `services/api/src/harrier/capture.py:58`
  (`load_candidate_config`) and `:80` (`fit_score_for`); spec 010.
- The follow-up seeding: `services/api/src/harrier/tracker/store.py:276-285`;
  `NEXT_ACTION_DEFAULTS` at `tracker/schema.py:64`.
- The queue: `services/api/src/harrier/tracker/queue.py` (`rank_active`).
- Delegation of the whole vector: spec 074,
  `services/api/src/harrier/delegate.py:108-134`,
  `harrier_cli/main.py` around lines 2323 to 2367; the lease and its
  subcommand name: spec 075.
- The run manager's closed argv: `services/api/src/harrier_api/runs.py:159-193`
  (`build_command`), `:392-396` (the spawn).
- The plists set no environment: `services/api/src/harrier/schedule.py:255-284`.
- The invariant text: `.ai/rules/product-invariants.md:8-10`.
- Redaction's profile read: `services/api/src/harrier/logredact.py:48-70`.
- `ALTER TABLE ... ADD COLUMN` with a CHECK is tested against existing
  rows: SQLite's `lang_altertable` documentation.
- The write path and scoped readers: specs 091 and 092; ADR-003; ADR-012.

## Out of scope

- The API, the contract and the web track switcher: a track parameter on
  routes, the switcher, labels in the browser.
- Per-track profile documents and configuration.
- Sources, discovery and importers for an academic track; scheduled
  discovery across tracks.
- Academic artifacts, outreach and contact discovery.
- A rules-only or learned academic score.
- Opportunity modelling beyond one deadline.
- Unarchiving, renaming, or changing a track's kind.
- Per-kind statuses (Open decisions, item 7).
