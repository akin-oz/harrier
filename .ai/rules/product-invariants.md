---
description: Product invariants carried from the old system; requirements, not suggestions
---

These invariants come from the old repo's OPERATIONS.md and CLAUDE.md and survive the
rewrite. Changing any of them requires an explicit spec.

- **One tracker, one write path.** The tracker is the single source of truth for
  application state. Every row belongs to one search track (spec 091, ADR-012).
  The status lifecycle is prospect, shortlisted, tailored_cv_requested, applied,
  interviewing, rejected, shared by every track kind; a kind supplies its own
  labels and next-action defaults (spec 093), never its own statuses. A kind that
  needs a stage the six cannot express changes this invariant by spec. The
  outreach status axis is orthogonal.
- **Ingestion only.** Each job source module normalizes into the shared job shape and
  returns it. Filtering, scoring, remote-only enforcement, EMEA scoping, dedupe, and
  tracker append happen once, in the shared screening path. No per-source scoring
  logic, ever.
- **Remote-only and EMEA scope are enforced on the industry kind.** Hybrid
  and onsite are rejected on location signals; the deliberate scoping of
  negative hints to the location field (not descriptions) is documented in
  harrier.screening.rules and pinned by
  services/api/tests/test_screening.py. EU-permit and EU-entity phrases
  ("EU work permit required", "must be based in EU", "EU-based contractor")
  are positive scoring signals, never filters: the candidate can contract
  through an EU legal entity. An academic track's region scope is the
  `countries` of its search entry, applied by its source (spec 097).
- **Nothing auto-sends.** Outreach, applications, and emails are drafted; a human
  sends them. Telegram notifications are the only outbound messages. Contact discovery
  stages candidates for approval; nothing writes contacts directly.
- **Artifact gates.** Resume and cover letter generation succeed only if the PDF
  exists and validates. Internal tailoring labels never appear in recruiter-facing
  output; internal metadata lives in sidecar files only.
  Generated resume text carries no em dash, en dash or double hyphen, and no
  hyphen with spaces on both sides, as punctuation (spec 071).
- **Verified content only.** Resume and answer content comes from the verified truth
  sources. AI selects and orders; it never invents claims.
- **Local-first.** No cloud dependencies beyond Apify, the AI providers, Telegram,
  Gmail API, and Hunter. All LLM calls go through the provider seam in
  `services/api/src/harrier/llm/`, selected by env (`AI_PROVIDER`), pluggable across
  codex-cli, claude-cli, openai-api, anthropic-api.
