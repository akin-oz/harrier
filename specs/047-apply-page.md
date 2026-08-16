---
spec: 047
title: The Apply page, and runs that take an argument
status: accepted
approved: yes
milestone: M8
depends: [013, 014, 015, 034, 035, 042]
---

# Spec 047: The Apply page, and runs that take an argument

## Problem

Spec 042 phase 1 delivered the Tracker page. Everything it left for phase 2 is
still terminal-only: tailoring a resume, drafting a cover letter, drafting
answers to application questions, and evaluating an offer. The operator can
shortlist a job in the browser and must then open a terminal to act on it.

The domain code is not missing. `harrier.resume.tailor.run_tailor`,
`harrier.apply.generate_cover_letter`, `harrier.apply.generate_answer_set` and
`harrier.offers.evaluate_offer` all exist and all work. What is missing is an
HTTP route in front of each, and a page that calls them.

There is a second problem underneath, and it is why this spec is larger than
"add four routes".

**The run machinery cannot express a per-job operation.** Spec 042 ruled that
long operations are runs rather than requests, and every operation here
qualifies: each makes at least one LLM call, and tailoring also boots Chromium
to render and validate a PDF. But the run manager takes no arguments:

- `KIND_COMMANDS` in `services/api/src/harrier_api/runs.py` maps a kind to a
  fixed, parameterless argv. There is no way to say which job.
- `RunManager.start(kind)` allows one active run per kind, so tailoring one job
  would block tailoring another.

ADR-004 anticipated exactly this ("artifact renders are per-slug locked") and
the implementation never reached it. So phase 2 cannot be built on the run
machinery as it stands, and phases 3 and 5 inherit the same obstacle. This spec
owns the fix because it is the first to need it.

## Scope

### Parameterized runs

`start` takes a kind and a set of validated parameters. Each kind owns a
builder that turns parameters into argv. Callers never assemble argv.

Three properties, each of which is a defect if absent:

**Parameters are typed per kind, not free-form strings.** A job selector is an
integer or a stored selector, validated before it reaches argv. The subprocess
is spawned with `create_subprocess_exec`, so no shell parses the result and
shell injection is not the risk. Argument injection is: a value that begins
with a dash becomes a flag rather than a value. Builders pass values in
`--flag=value` form so a leading dash cannot split into a new argument.

**Operator free text never enters argv.** Cover-letter notes and application
questions are free text supplied by the operator. Instead of argv, the route
writes those inputs to a run-scoped file under the data directory, which is
never-in-git, and the builder passes only that path. The file is removed when
the run reaches a terminal state.

Amended during implementation. This rule was first written on the grounds that
argv is journaled and returned by `GET /runs/{id}`, and that is not true:
`RunManager._journal` records id, kind, state, timestamps and exit code, and
`RunOut` declares the same set. Neither carries `command`. The reason the rule
survives its original justification is a different one, and it holds today:

- **argv is world-readable in the process table.** Any process on the machine
  can read it from `ps`. An application answer or a note about why the
  candidate wants the job is exactly the content ADR-008 keeps out of reach,
  and a subprocess argument is the one place in this system that publishes a
  string to every other local process.
- Argument lists are length-bounded, and a pasted set of application questions
  is the input most likely to find that bound.
- It keeps the journal and `RunOut` free to carry `command` later, which is a
  reasonable debugging addition that would otherwise become a leak.

This rule needed one CLI addition, stated here rather than left in the diff.
`answers` already took `--questions-file` and `tailor` and `evaluate` already
took `--jd-file`, but `cover-letter` accepted its notes only as `--notes`, on
argv. It gains `--notes-file`, mutually exclusive with `--notes`, so the route
has a file to point at. Nothing about the existing flag changes.

**The lock is per target, not per kind.** Two jobs tailor concurrently. The
same job tailored twice returns the already-active run, which is the existing
behavior of `start` widened from kind to (kind, target). A global cap still
bounds concurrent Chromium instances, as ADR-004 requires.

### Routes

One route per operation, each calling the same domain function the
corresponding CLI verb calls, which is the rule spec 042 established and its
tests pin.

| Route | CLI verb | Domain function | Shape |
|---|---|---|---|
| `POST /apply/{selector}/resume` | `tailor` | `run_tailor` | run |
| `POST /apply/{selector}/cover-letter` | `cover-letter` | `generate_cover_letter`, `write_cover_letter_artifacts` | run |
| `POST /apply/{selector}/answers` | `answers` | `generate_answer_set`, `render_markdown`, `write_output` | run |
| `POST /apply/{selector}/evaluate` | `evaluate` | `evaluate_offer` | run |
| `GET /apply/{selector}/artifacts` | none | the artifact index below | request |
| `GET /apply/{selector}/artifacts/{kind}` | none | the artifact index below | request |

### Reading artifacts back

The page must show what was produced, and for the resume that means a PDF the
browser renders.

Generated artifacts live under `data/`, which `config/data-classification.json`
classes never-in-git. Serving them over HTTP does not change that
classification: the file stays on the machine and out of the index. It does add
a read surface for personal data where none existed, which forces two rules.

**The route never accepts a path.** It takes a job selector and an artifact
kind from a closed set. Resolution goes through the same helpers that wrote the
file (`resumes_dir`, `cover_letters_dir`, and the answers and evaluation
equivalents). A caller cannot express a path, so there is no traversal to
defend against, rather than a traversal that is defended against correctly
today and incorrectly after the next edit.

**Artifact reads require the token, and other reads still do not.** Phase 1
settled that writes carry the token and reads do not, and this departs from it
deliberately. `GET /jobs` returns tracker rows the operator already sees in the
UI; an artifact is a generated resume, a cover letter, and an offer evaluation,
which is the densest personal content the system holds. The asymmetry is
stated here so a later reader finds a reason rather than an inconsistency.

An artifact that does not exist yet is a 404 carrying which operation would
produce it, not an empty body.

### The page

One page, one job at a time, reached from the Tracker table. It shows the four
operations, the state of any run in flight with its event stream, the
artifacts that exist, and the refusals below in the words the API used.

## Inputs, outputs, failure modes

- Inputs: HTTP requests from the local browser, carrying a job selector and,
  for answers and cover letters, operator free text.
- Outputs: the same artifacts the CLI writes, in the same directories, by the
  same functions.

Failure modes that must reach the operator rather than a log:

- **The truth gate refuses** (spec 034). The resume claims something the truth
  sources do not support. This is the gate working, and the page shows the
  refusal and what it objected to. It is not an error state.
- **The PDF gate refuses** (spec 013). The render produced no file, or one that
  does not validate. The tracker row is not updated, which is existing behavior
  and stays.
- **The LLM provider refuses or is unreachable.** The provider seam reports it
  and the run fails with the provider's own message.
- **The application profile is missing** (`ApplicationProfileError`). Cover
  letters and answers need it. The page says which document is missing.
- **No job description is stored.** Tailoring falls back to the cached
  description; with neither, the operation proceeds on less input and the page
  says so, matching what the CLI prints.

Failure modes this must not introduce:

- A second implementation. A route that reimplements a CLI verb drifts from it,
  and both test suites stay green because each covers its own copy.
- Personal data in argv, the run journal, or the run listing.
- An operation that is a run in the UI and a request in the CLI without the
  difference being visible.
- A partially written artifact presented as complete. A run that fails halfway
  leaves no artifact the index will list.

## Acceptance criteria

Proving symbols are named at implementation.

- [ ] `start` accepts parameters, and a test asserts two different jobs tailor
      concurrently while the same job twice returns the active run
- [ ] a parameter whose value begins with a dash reaches the domain function as
      a value, proven by a test that passes one
- [ ] no operator free text appears in a run's argv, its journal record, or the
      response of `GET /runs/{id}`, proven by a test that submits a question
      containing a recognizable token and asserts its absence in all three
- [ ] the run-scoped input file is owner-readable from the moment it exists,
      and is removed when the run reaches a terminal state, including when it
      fails and when it is cancelled
- [ ] each of the four operations has a route, and a test asserts the route and
      the CLI verb call the same domain function
- [ ] a truth-gate refusal surfaces in the UI with the gate's own words, not a
      generic failure
- [ ] a PDF-gate refusal leaves the tracker row unchanged, and the page says
      the artifact was not produced
- [ ] the artifact route resolves through the writing helpers and accepts no
      caller-supplied path, proven by a test that a traversal-shaped kind is
      refused as an unknown kind rather than read
- [ ] an artifact read without the token is refused, and a tracker read without
      the token still succeeds
- [ ] a missing artifact returns 404 naming the operation that produces it
- [ ] the generated client carries every new route and no hand-written request
      or response shape appears in `apps/web`, enforced by the existing
      contract drift gate
- [ ] no personal data enters a committed fixture, a test name, or a
      screenshot. Every fixture is an invented company. Limitation: this is a
      property of the diff, and no test asserts it
- [ ] all gates green on PR

## Proof / origin

The route and CLI inventory is spec 042's table, which was counted rather than
estimated. The run-machinery gap was read from `KIND_COMMANDS` and
`RunManager.start` in `services/api/src/harrier_api/runs.py`. The per-slug lock
this spec builds is named in ADR-004 under "Concurrency policy" and was never
implemented. The artifact directories are `resumes_dir` in
`harrier/resume/tailor.py` and `cover_letters_dir` in `harrier/apply/letters.py`,
both under `data_dir()`.

The request comes from the operator: everything available in the CLI should be
available in the UI.

## Out of scope

The Outreach, Inbox and Operations pages, which are specs 048, 049 and 050.
Any change to what a domain function does: this spec exposes existing behavior
and fixes none of it. Authentication design, which is spec 035. Mobile layout.
Sending anything to an employer, which no part of this system does. Editing a
generated artifact in the browser: the page shows what was produced and
regenerates it, and nothing more.

## Migration

None for stored data. Runs started before this change carry no parameters and
their journal records stay readable; the loader treats a missing parameter set
as empty. Milestone M8 does not appear in `specs/README.md`, which lists
milestones through M7. That line is a documentation change this spec does not
make, and it should land with whichever of these four specs is approved first.
