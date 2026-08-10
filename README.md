# harrier

Local-first job search automation. It watches job boards, screens what it finds
against a policy you set, keeps one tracker as the source of truth, generates a
tailored resume and cover letter per application with hard correctness gates,
drafts outreach, watches your inbox for replies, and sends a nightly digest.
Python domain behind a FastAPI service, React with strict TypeScript in front,
SQLite underneath, one machine, no cloud.

```bash
git clone https://github.com/akin-oz/harrier && cd harrier && just demo
```

It seeds a throwaway database from synthetic fixtures and serves the API and
the web app from `http://127.0.0.1:8000`. No API keys, no accounts, and no
sign-up: the running demo reaches no network at all, because every job board
response comes from `fixtures/http/` and a URL with no fixture raises rather
than falling through to a request.

Two honest caveats. The first run builds the frontend, so it needs the npm
registry once like any JavaScript project, and writes `apps/web/dist` inside
the clone (gitignored). Everything the demo itself produces (the database,
discovery state, artifacts) goes to a temp directory, so your checkout stays
clean.

## What it actually does

**Discovery.** Greenhouse, Ashby, Lever, and RemoteOK are free and run four
times a day. Apify LinkedIn scraping costs money, so it runs on weekday
mornings only. Every source is ingestion only: it normalizes into one shape and
hands off. Nothing bypasses the shared path.

**Screening.** One pipeline decides. Remote-only and region policy are hard
gates, title and stack matching feed a score, and each decision records the
signals behind it. In the demo, six of the fifteen fixture postings are
rejected on title and one for being hybrid, which is the policy visible at
work rather than described. Those counts are asserted by
`test_demo_discovery_runs_offline_and_screens_the_fixture_boards`, so this
paragraph cannot drift from what the demo does.

**The tracker.** One SQLite table is the source of truth for every job, from
first sighting to offer. No spreadsheet, no second copy, no sync.

**Application artifacts.** Resume tailoring reorders and selects real evidence
against the job description; it never invents any. A truth validator checks
every generated line against a source-of-truth document and refuses to emit a
PDF when a claim cannot be traced. Cover letters and application answers work
the same way.

**Outreach and replies.** Contacts are discovered, staged for approval, and
never messaged automatically. A Gmail watch classifies incoming mail into
interview invites, assessments, rejections, and confirmations, and the nightly
digest reports new prospects, the top of the list, outreach due, applications
gone quiet for three weeks, and anything the inbox surfaced.

## Architecture

```text
apps/web          React 19, strict TypeScript, feature-sliced, generated API types only
services/api      FastAPI service + the harrier domain package + the CLI
packages/contract OpenAPI document and the TS types generated from it
specs/            the approved change log: nothing ships without one
docs/adr/         nine accepted decision records
fixtures/         synthetic demo data, doubling as public test fixtures
```

Four rules hold the shape:

1. **The domain knows nothing about the API.** `harrier` may not import
   `harrier_api` or `harrier_cli`. Enforced by import-linter in CI.
2. **Sources are ingestion only.** An importer may not reach screening,
   scoring, or the tracker. Also enforced by import-linter.
3. **The OpenAPI document is the contract.** The frontend consumes generated
   types and nothing else; CI regenerates both and fails on any diff.
4. **The tracker is written through one path.** Every insert goes through the
   same function, so deduplication and validation cannot be skipped.

The same domain code serves the API, the CLI, and the scheduled jobs. launchd
plists are generated at install time from `config/schedule.json` rather than
committed, because a committed plist carries an absolute path that drifts from
the installed copy, and a job that runs through a shell wrapper dies on a
malformed line in an env file. Both were real failures in the system this
replaces.

## Specs as the unit of change

Every change to observable behavior starts as a file in [specs/](specs/) with
`approved: no`. A human flips it to `approved: yes`; the agent never does. Each
commit then carries a `Spec: NNN` trailer, a git hook rejects commits without
one, and CI resolves every trailer to an approved spec or fails the build.

The point is not ceremony. It is that the spec states the acceptance criteria
and names the test that proves each one before the code exists, so "done" is
something you can check rather than something you are told. The specs also
record where each behavior came from and, where the port changed it, what
changed and why. The governance chain itself is compiled from [.ai/](.ai/)
sources by [@akinlabs/ai-engineering](https://www.npmjs.com/package/@akinlabs/ai-engineering).

## Running it for real

```bash
just check      # the full gate, identical to CI
just dev        # FastAPI on :8000, Vite on :5173
just demo       # the built SPA and the API on :8000, synthetic data
```

To use it on your own search, copy the `config/*.example.*` files to their real
names, fill them in, and run `harrier config import` to move them into the
database, where they are editable through `harrier config set` and the `/config`
endpoints without touching a checkout. The files keep working as a fallback if
you would rather not import them. Either way they are gitignored: your board
watchlist, your search URLs, and your hold list are your data, not the
project's (ADR-009). Personal
search data lives in `data/tracker.db`, credentials in `.env` and `secrets/`,
and backups outside the repository entirely. None of it enters git in any form
(ADR-008, docs/privacy-plan.md).

Optional and off by default: an LLM provider for drafting (Codex CLI, Claude
CLI, or the OpenAI and Anthropic APIs, selected by `AI_PROVIDER`), Apify for
LinkedIn, Gmail for the inbox watch, Telegram for the digest, and Playwright for
PDF rendering. None of them is required to run the pipeline.

## Honest limitations

- **Single user, single machine.** No auth on the API because it binds to
  localhost. Multi-tenancy is a direction (ADR-009), not a feature: the config
  store has a scope column that partitions, and that is the whole of it. There
  is no authentication, no tenant resolution, and no isolation.
- **macOS is the production platform.** Scheduling is launchd. Everything else
  is portable; the scheduler is not, and reports as much on other systems.
- **Personal data has exactly one home.** This machine, plus your own backups.
  Losing both loses the data. The repo cannot help you, by design.
- **Never-in-git protects the repository, not the machine.** Disk encryption and
  backup custody are yours to handle.
- **The generation gates reduce invention; they do not eliminate it.** The
  resume validator checks claims against a truth document, which means it can
  only catch what that document contradicts. Read what it produces.
- **A rewrite in progress.** M0 through M4 are shipped and M5 is underway. The
  private system it replaces is still the one in daily use until the parity
  cutover (spec 022) verifies this one matches it.

## License

Not yet chosen, which means default copyright applies until one is added.
A license lands before the repository goes public.
