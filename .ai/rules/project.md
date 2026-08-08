---
description: What harrier is and where things live
---

Harrier is local-first job search automation: discovery across job sources, a tracker
as single source of truth, tailored artifact generation (resume, cover letter, answers),
outreach drafting, Gmail watch, and a daily digest. Python domain and FastAPI service in
`services/api`, React SPA in `apps/web`, generated API contract in `packages/contract`.
It is a public repo that doubles as an engineering showcase.

Ground rules:

- Read `docs/architecture.md` before structural work. Decisions live in `docs/adr/`;
  do not relitigate an accepted ADR in code review or implementation.
- The old system at `~/job-hunt-local` is read-only reference. Never modify it. Port
  behavior deliberately; cite the old file path when porting a rule or invariant.
- `CLAUDE.md`, `AGENTS.md`, and generated files under `.claude/` and `.ai/generated/`
  are compiled by `aie sync`. Never hand-edit them. Hand-edited sources live in `.ai/`.
  When the compiler cannot express something, wire it in `.claude/settings.json` or
  `.claude/hooks/` (which the compiler does not own) and record the gap in
  `docs/aie-feedback.md`.
- Commands: `just dev` (run), `just demo` (demo mode), `just check` (full local gate,
  same recipes CI runs), `just gate` (fast turn-end gate).
