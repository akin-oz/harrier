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
against the job description; it never invents any. Every resume bullet is
checked against a source-of-truth document, and an unverifiable line refuses
the artifact rather than being dropped from it
(`services/api/tests/test_honesty.py`, spec 034). The check understands
document structure and negation, so a section headed "claims I must not make"
does not verify the claims it lists and "I did not own X" does not verify
"own X". The candidate's own forbidden-phrase list refuses an artifact that
contains one.

Cover letters get the same PDF validation and phrase scrubbing as the resume.
They do not get the line-by-line truth check: a letter is prose rather than a
list of claims, and nothing here verifies it sentence by sentence. Application
answers are in the same position. That limitation is stated because the
previous version of this paragraph said all three worked the same way, and
they did not.

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

Four standing guardians check that the rules were followed. Two review
boards under [.ai/agent-teams/](.ai/agent-teams/) ask the questions the
guardians cannot, because compliance and judgement are different things and
a repository can be perfectly consistent with a design that was wrong to
choose:

- **principal-review** interrogates the design, starting with whether this
  much governance is proportionate to a tool with one user.
- **open-source-readiness** sweeps before publication. Every one of its five
  lenses exists because this repository has already failed that way, which
  is written down next to each of them.

The design is [specs/028-agent-teams.md](specs/028-agent-teams.md), and
`services/api/tests/test_governance.py` holds the boards to it: membership
resolves in both directions
(`test_every_member_named_in_a_launch_document_exists`,
`test_every_team_agent_is_claimed_by_exactly_one_team`), the `.claude/`
mirror cannot drift (`test_the_compiled_copy_matches_the_source`), and the
tool grants match what each board claims
(`test_review_board_members_cannot_execute_anything`,
`test_no_team_member_can_write`).

The five `principal-review` reviewers are read-only in the enforceable
sense: they hold `Read, Glob, Grep` and no `Bash`. The five
`open-source-readiness` investigators do hold `Bash`, because cloning,
running the suite and running the demo is their lens; they are instructed to
work in a temporary copy, which is an instruction and not a sandbox.

Running either board needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Nothing
tests that flag: it belongs to the runtime, not to this repository.

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
you would rather not import them (all of it proven by
`services/api/tests/test_userconfig.py`). Either way they are gitignored: your board
watchlist, your search URLs, and your hold list are your data, not the
project's (ADR-009).

A watchlist goes stale: companies close a board, move provider, or rename it,
and the entry answers 404 forever while every run pays for it.
`harrier config check-feeds` probes each configured board once and reports it
as live, dead, or unreachable, and `--prune` removes the dead ones. Only a 404
or a 410 counts as dead: a 403, a 500, a timeout and an unparseable response
are all unreachable and are never pruned, because an outage must not delete
your watchlist (`services/api/tests/test_feed_health.py`, spec 025). Personal
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

MIT. See [LICENSE](LICENSE).

The previous text here said a license would land before the repository went
public. The repository was already public, so for its whole life until this
change nobody could legally use, modify, or redistribute any of it. Found by
the `open-source-readiness` board (spec 028), fixed under spec 038, and
recorded rather than quietly corrected because the sentence being wrong is
more instructive than the gap itself.
