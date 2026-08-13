# Harrier target architecture

Ground-up rewrite of `~/job-hunt-local`: local-first job search automation as a public
monorepo. This doc is the map; each firm decision cites its ADR. Requirements baseline:
the old repo's `CLAUDE.md` and `OPERATIONS.md`, inventoried in
`docs/parity-matrix.md`.

## System shape

Three execution surfaces over one domain core, one store, one contract:

```
launchd (schedule)          browser (GUI)              terminal
      |                          |                         |
      v                          v                         v
 harrier CLI  ----shares---> FastAPI service <---- generated TS client
 (harrier_cli)   domain code  (harrier_api)         (packages/contract)
      |                          |                        |
      +---------> harrier domain package <----------------+
                  (tracker, screening, scoring,        (types only)
                   artifacts, outreach, mail)
                         |
                  SQLite (data/tracker.db)  +  file stores (state/, runtime/)
```

- The **domain package** (`services/api/src/harrier/`) owns all behavior: the tracker
  write path, the screening pipeline, scoring, artifact generation, outreach, mail
  classification. It imports nothing from the API layer (lint-enforced, ADR-007).
- The **CLI** (`harrier_cli`) and the **API** (`harrier_api`) are thin shells over the
  same domain calls. Scheduled runs use the CLI (ADR-006); interactive runs go through
  the API's run manager, which executes the same CLI entry points as subprocesses
  (ADR-004). Behavior cannot fork between surfaces because there is one code path.
- The **web app** (`apps/web`, Vite + React, strict TS, Feature-Sliced Design, ADR-001)
  talks only to the API through the generated client (ADR-005).

## Module boundaries inside the domain

Mirrors the old system's four planes plus generation and outreach, with the boundaries
the old code implies but does not enforce:

| Module | Owns | Old-repo origin |
|---|---|---|
| `harrier.tracker` | The single write path, status lifecycle, selectors, dedupe indexes, CSV export | `scripts/jobs.py`, tracker parts of `job_sources.py` |
| `harrier.screening` | Normalized job shape, gate order, remote/EMEA policy, scoring, archetypes | `scripts/job_sources.py` |
| `harrier.sources` | One ingestion module per source, each returning normalized jobs only | `scripts/import_*.py` |
| `harrier.resume`, `harrier.apply` | Resume, cover letter, answers; truth validation; PDF gates; label scrubbing | `tailor_resume.py`, `openai_cover_letters.py`, `application_answers_lib.py` |
| `harrier.offers` | Offer evaluation blocks, verdict contract, batch prospect evaluation | `evaluate_offer.py`, `evaluate_prospects.py` |
| `harrier.outreach` | Contacts store, staged discovery, fit scoring, drafts, queue state machine | `outreach_lib.py`, `find_contacts.py`, message generators |
| `harrier.mail` | Gmail poll, classification cascade, event log | `gmail_watch_lib.py` |
| `harrier.notify` | Telegram sending, digest assembly | `send_telegram.py`, `send_daily_digest.py` |
| `harrier.llm` | The provider seam: codex-cli, claude-cli, openai-api, anthropic-api, auto | `llm_client.py` |
| `harrier.profile` | Candidate config, application profile, resume facts, truth sources | `application_profile.py`, `resume_facts.py` |

Boundary rules carried from the product invariants:

- `harrier.sources` modules are ingestion only. They may not import scoring, filtering,
  or tracker code; they return normalized jobs and nothing else. The shared path in
  `harrier.screening` does the rest, once. (Old rule: OPERATIONS.md "Design rule".)
- All tracker mutation goes through `harrier.tracker`. The API, CLI, capture endpoints,
  and the one automated mutator (batch evaluation auto-reject) call it; nothing touches
  SQLite directly. (Old rule: "tracker is the only source of truth".)
- All LLM calls go through `harrier.llm`. The old repo has two bypasses
  (`evaluate_offer.py`, `tailor_resume.py` own OpenAI paths); the rewrite closes them.
- Draft, never send: `harrier.outreach` and `harrier.mail` have no send capability for
  outreach or email. Telegram notifications are the only outbound messages.

## Contract seams

Three seams, each with a generated artifact, a drift gate, and a pre-edit guard:

1. **API contract** (ADR-005): FastAPI exports `packages/contract/openapi.json`
   deterministically; `openapi-typescript` generates the TS types; CI regenerates and
   fails on diff; the web app can only speak in generated types.
2. **Tracker schema** (ADR-003): one schema definition with migrations in
   `harrier.tracker`; the CSV export shape is part of the contract and tested. The old
   repo's triplicated column list collapses to this single point.
3. **Governance sources** (`.ai/`): `aie sync` generates `CLAUDE.md`, `AGENTS.md`,
   `.claude/**`; `aie check` in CI proves no drift; generated files are never
   hand-edited (gaps go to `docs/aie-feedback.md`).

## Data flow

**Discovery (scheduled or interactive)**

```
feeds.txt / linkedin_search_urls.txt
  -> harrier.sources.<source>.fetch()        (ingestion only)
  -> normalize to the shared job shape
  -> harrier.screening.screen():             seen-state -> hold list -> title
                                             -> remote/EMEA -> tracker dedupe
                                             -> enrich -> score (ranks; spec 033
                                                removed the cutoff)
  -> harrier.tracker.add_prospects()         (one write path, one transaction)
  -> per-source summary + run summary        (incoming/ shape preserved)
  -> one aggregated Telegram notify
```

Interactive runs wrap this in the run manager: `POST /runs` spawns the CLI as a
subprocess; stdout's structured progress lines become SSE events
(`log_line`, `progress`, `state_change`, `result`) consumed live by the GUI (ADR-004).

**Application workflow**

```
GUI/CLI: shortlist -> tailor -> applied -> interviewing | rejected
tailor: harrier.resume.tailor_resume()
  -> content plan (LLM selects bullet IDs; require_truth validates against truth sources)
  -> markdown -> HTML template -> Playwright PDF -> validate_rendered_pdf
  -> success only if the PDF exists; tracker row updated only on success
applied: seeds follow-up date + outreach block (needs_contacts, find contacts, high)
```

**Outreach**

```
applied row -> staged contact discovery (Apify profile search)
  -> runtime/outreach/<slug>-candidates.json   (staged, never direct)
  -> human approves in GUI -> harrier.outreach.add_contact()
  -> drafts (AI or template path) -> human sends manually -> mark sent/replied
```

**Mail and digest**

```
launchd (5 min) -> harrier.mail.poll(): Gmail readonly -> classification cascade
  -> event log -> Telegram for actionable kinds -> tracker row matching
20:30 -> harrier.notify.digest(): new prospects, top prospects, outreach due,
  ghosted (>21 days), mail events
```

**Capture**

```
bookmarklet -> GET/POST /capture/add on the API (mixed-content-safe HTML response kept)
  -> harrier.tracker.add_manual() -> 200/400/409/500 contract preserved
```

## Data at rest

Per ADR-002 as revised by ADR-008: source and fixtures public; everything
personal (candidate and application content, tracker, contacts, artifacts,
state, logs) lives in the local database and local files, never in git in any
form; credentials never in git. Demo mode boots from `fixtures/` with zero
secrets. gitleaks guards every commit and CI run; a coverage test binds the
classification table to `.gitignore`.

## Governance chain

The Sorrel chain (a private sibling project of the author's, README
"Spec-gated quality enforcement"), expressed through `.ai/` sources and compiled by
`aie`, with hooks the compiler cannot express wired directly into
`.claude/settings.json` (the compiler preserves entries it does not own):

1. Specs in `specs/`, `approved: yes` gates implementation, only Akin flips approval.
2. Turn-end gate: pyright + tsc + unit tests; red fails the turn (exit 2 Stop hook).
3. Pre-edit guard pauses on: `packages/contract/**`, `harrier/tracker/` schema and
   migrations, the classification config and `.gitignore`, `.github/workflows/**`, `.ai/**`
   generated outputs.
4. Every commit carries `Spec: NNN`; CI resolves trailers to approved specs.

Details and the review-agent set land with deliverable 4 (checkpoint B).

## Honest limitations

- Single user, single machine. No auth on the API; it binds to localhost.
- The run registry is not a durable queue; scheduled work bypasses it by design.
- macOS is the production platform (launchd); other platforms get manual CLI or cron.
- SQLite truth trades direct greppability for transactions; `just export` restores it.
- The demo shows the pipeline on synthetic data; Apify, Gmail, and Telegram paths need
  real keys and are stubbed in demo mode.
