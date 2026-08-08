# ADR-001: Frontend framework

- Status: accepted
- Date: 2026-08-08

## Context

The frontend is a localhost, single-user GUI for a job search pipeline. FastAPI owns all
data and serves the generated PDFs. The GUI needs: a tracker table with heavy filtering,
artifact workflows (resume, cover letter, answers) with PDF-ready state, an outreach queue
with staged approval, and live progress for long-running runs (discovery, Apify scrapes,
PDF renders) over the push channel chosen in ADR-004. Feature-Sliced Design is the default
architecture. The repo is public and doubles as a showcase.

## Which axes matter here

Axes that matter:

- Process count and startup time. This is a daily driver started by hand or by `just dev`.
- Push-channel client ergonomics (SSE per ADR-004) without a server runtime in between.
- Serving generated PDFs. FastAPI already does this; the frontend only links and embeds.
- Codegen fit: the TS client is generated from OpenAPI (ADR-005) and used everywhere.
- FSD fit and the showcase story.

Axes that are noise for this project:

- SEO, SSR, ISR, edge rendering. There is no public traffic and no crawler.
- Image optimization, font pipelines, marketing-page performance scores.
- Server Actions and RSC data fetching. FastAPI is the only data owner; adding a second
  server-side data layer would duplicate the contract seam ADR-005 exists to protect.

## Options

### Option A: Next.js App Router

Pros: familiar to reviewers, batteries included, the Sorrel-era patterns (App Router,
RSC boundaries) are proven in the reference repo. Cons: a Node server process next to
FastAPI for zero benefit (no SSR need); RSC/client boundary discipline is real ongoing
cost; SSE consumption and client-only state push most code into `"use client"` anyway,
at which point the App Router is routing overhead; FSD guidance for App Router requires
an adapter layer (`app/` as a thin re-export shell) because file-system routing and FSD
layers fight over ownership of pages.

### Option B: Vite + React SPA (recommended)

React 19, Vite, TanStack Router (typed file-less routes) and TanStack Query over the
generated client. Pros: one static bundle served by anything (FastAPI itself in demo
mode, `vite dev` in development); no second server process; startup is Vite dev-server
fast; SSE via native `EventSource` plus a thin Query integration, no framework
indirection; FSD maps cleanly (app, pages, widgets, features, entities, shared with the
generated client living under `shared/api`). Cons: no SSR ever (irrelevant here); fewer
conventions out of the box, which FSD supplies instead; reviewers may ask "why not
Next", which this ADR answers.

## Decision

Option B: Vite + React SPA, strict TypeScript, TanStack Router, TanStack Query,
Feature-Sliced Design. The showcase story is the architecture (FSD boundaries, generated
contract client, spec-gated governance), not the metaframework. A localhost tool with an
API-owning backend is the textbook case where a SPA is the honest choice, and defending
that honestly is a better showcase than defaulting to Next.js.

## Consequences

- Dev up runs two processes: FastAPI and Vite. Demo mode runs one: FastAPI serving the
  built bundle.
- FSD layer boundaries are enforced by lint (`eslint-plugin-boundaries` or steiger) and
  reviewed by the frontend architecture review agent.
- No RSC means all data access goes through the generated client, which is exactly what
  ADR-005 wants: one seam, type errors on drift.
- If a public deployment ever becomes a goal, SSR can be revisited in a new ADR; nothing
  in the FSD structure blocks it.
