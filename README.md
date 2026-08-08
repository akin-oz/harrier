# harrier

Local-first job search automation: discovery across job boards, a tracker as the
single source of truth, tailored resume and cover letter generation with hard
correctness gates, outreach drafting, and a daily digest. Python domain behind a
FastAPI service, React frontend with strict TypeScript, one machine, no cloud.

**Status: ground-up rewrite in progress, built in the open.** This repo replaces
a working private system and reaches feature parity milestone by milestone. The
architecture and every decision are documented before the code exists:

- [Target architecture](docs/architecture.md)
- [Decision records](docs/adr/) (ADR-001 through ADR-008, all accepted)
- [Spec backlog](specs/) : every change is gated on an approved spec, and every
  commit carries a `Spec: NNN` trailer that CI resolves or blocks
- [Parity matrix](docs/parity-matrix.md) : what the old system does and what
  happens to each capability
- [Privacy plan](docs/privacy-plan.md) : this is a public repo about a real
  person's job search; nothing personal enters git in any form, and tests
  enforce that

The governance chain (spec gating, commit trailers, turn-end verification
hooks, CI resolution) is compiled from [.ai/](.ai/) sources by
[@akinlabs/ai-engineering](https://www.npmjs.com/package/@akinlabs/ai-engineering)
and is a first-class feature of the project, not scaffolding.

## Current state

Milestone M0 (toolchain, CI, privacy enforcement) is complete. M1 (the walking
skeleton: tracker store, API contract seam, live run streaming) is next. The
full sequence lives in [specs/README.md](specs/README.md).

Until milestone M5, the demo mode described in the docs does not exist yet and
this repo is not runnable in any interesting way. Watching the commit history
is the current demo.

## Limitations

Single user, single machine, macOS as the production platform. No auth on the
API (localhost only). Personal data lives exclusively in a local database with
local backups; the repo cannot restore it by design.
