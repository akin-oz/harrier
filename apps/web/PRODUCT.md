# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The operator: one person running their own job search with Harrier on their
own machine. They are comfortable with a terminal, and they want the daily
work in the browser: triaging what discovery found, moving applications
through the tracker, drafting artifacts and outreach, and checking that the
scheduled jobs still run.

A hosted version for other job seekers is a stated direction (ADR-012 names
the tenant boundary). The browser is not yet designed for a user who has
never seen a terminal, and nothing in it should assume one cannot appear
later.

## Product Purpose

Harrier is local-first job search automation. It watches job boards, screens
what it finds against a policy the operator sets, keeps one tracker as the
single source of truth, generates a tailored resume, cover letter and
application answers per job behind hard correctness gates, drafts outreach,
watches the inbox for replies, and sends a daily digest.

Success is a search the operator can run in short daily sessions without
losing track of anything, and without ever sending a claim they cannot stand
behind.

## Positioning

Harrier refuses rather than invents. Every resume and answer line comes from
the operator's verified truth sources; an unverifiable line refuses the
artifact, and the refusal is shown in the domain's own words. It drafts and
never sends. It keeps everything on one machine. It is also a public,
spec-gated codebase: every behavior is written down before it is built, and
what the product cannot do is stated rather than hidden.

## Operating Context

- Short daily triage sessions, mostly at a desktop browser and sometimes on
  a phone, against the API served
  from `http://127.0.0.1:8000` by a local container (ADR-010).
- A terminal beside it: some commands only run on the host, and the browser
  shows them with the exact command to run (spec 096).
- Long work (discovery, tailoring, evaluation) runs as a background run with
  a live event stream; short reads and writes answer at once (spec 042).
- More than one search can be tracked: the default industry track, and
  academic tracks entered by hand (specs 091 to 094).

## Capabilities and Constraints

- The browser and the command line call the same domain functions; a
  behavior that differs between them is a bug (spec 042).
- The web app speaks only in types generated from the API contract
  (ADR-005); no hand-written request or response shapes.
- Feature-Sliced Design layers in `apps/web/src` (ADR-001).
- Every state-changing request carries the local token (spec 035).
- Personal data never enters git, a fixture, a test name or a screenshot
  (ADR-008); the repository is public.
- Statuses are shared across track kinds; a kind supplies only its labels and
  next actions (spec 093).

## Brand Commitments

- The name is Harrier.
- **Never invents claims.** A refusal from an honesty gate is shown in full,
  in the domain's words, never softened or hidden.
- **Nothing auto-sends.** Outreach, applications and email are drafts. No
  control reads as "Send"; marking something sent records what the operator
  did themselves.
- **The operator's data stays local.** Nothing in the browser implies cloud
  sync, accounts or sharing.
- **Honest about gaps.** What the browser cannot do is shown with the reason
  and the command, never silently left out.
- Plain, short, factual copy: no marketing language, no em dashes.

## Evidence on Hand

- Synthetic demo data: `fixtures/`, served by `just demo` with no network.
- No testimonials, customers, usage numbers or benchmarks exist. None may be
  invented, and no real search data may appear in any screenshot or copy.

## Product Principles

1. Show the decision and the evidence behind it, not a verdict alone.
2. A refusal is information, presented as plainly as a success.
3. Nothing changes on an empty or accidental request; a write is a
   deliberate act, and an irreversible one names what it will do.
4. The browser covers exactly what it says it covers, and says what it does
   not.
5. Fast for the daily path: the common move is one keystroke or one click
   away.

## Accessibility & Inclusion

- WCAG 2.2 AA.
- Keyboard-first: every queue, status change and confirmation works from the
  keyboard, with visible focus.
- State is never carried by color alone: a passed deadline, a refusal or an
  archived track is also said in words.
- Desktop-first, and usable on a phone. At phone width every screen works:
  nothing is cut off, overlaps or needs horizontal page scrolling, every
  control is reachable and large enough to tap, and the daily path (switching
  track, reading the queue, changing a status) can be completed. The phone
  layout may be simpler than the desktop one; it may not be broken.
