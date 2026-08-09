---
spec: 013
title: Tailored resume generation with truth validation and PDF gate
status: shipped
approved: yes
milestone: M3
depends: [012, 004]
---

# Spec 013: Tailored resume generation with truth validation and PDF gate

## Problem

The flagship artifact. The old scripts/tailor_resume.py produces a
one-page tailored PDF where every claim is validated against truth
sources, the LLM may only reorder pre-approved evidence, and internal
labels ("Tailored for X") can never surface. Two structural problems
block a straight port: the old script hardcodes personal data in code
(the bullet pool, evidence groups, education, certifications, a phone
number), and the old tests pin literal personal facts. Neither can enter
a public repo.

## Scope

- Content out of code (ADR-008, ADR-009): the tailoring engine reads a
  "resume content bundle" from the profile store (kind resume_data),
  extending the migrated document with bullet_pool, evidence_groups,
  technology_aliases, target_signal_weights, evaluation dimensions with
  evidence refs, forbidden_phrases, default bullet selections, education,
  certifications, and contact fields. A JSON-schema-style validator
  rejects malformed bundles with named errors. The repo commits a fully
  synthetic example bundle; the real bundle is a one-time local data
  operation into the operator's database during this spec's landing and never
  touches git.
- Package harrier.resume, a data-driven port of the old pipeline:
  - facts: date parsing, completed-anniversary experience math,
    engagement-over-role period labels, "N+ years" labels
    (resume_facts.py port)
  - ranking: JD technology scores via bundle aliases, skill ranking
    (relevance, evidence depth, recency, positioning bonus), bullet
    scoring (quantified evidence outranks generic keyword matches),
    evidence-group-distinct selection
  - plan: deterministic content plan (title, skills, profile, role
    bullets, achievements, periods), validation (experience label,
    unsupported identity, unsupported skills, unknown bullet IDs,
    evidence-group duplication, canonical periods, ended engagements
    never Present)
  - evaluation: JD requirement extraction, evidence-grounded fit matrix
    and dimensions, candidate questions; dimension behavior keyed by a
    bundle "kind" (default, backend_ownership, database,
    absent_by_default) instead of hardcoded dimension names
  - markdown: visible-title normalization (internal labels scrubbed),
    grounded header, section assembly from bundle data, rendered-content
    validation
  - html: template render from templates/resume-template.html and .css
    (placeholders only, no personal data), unresolved-placeholder check
  - pdf: Playwright render (lazy import, actionable install error),
    validate_rendered_pdf (exists, non-empty, no replacement characters,
    no unresolved placeholders, exact page count via pdfinfo)
  - ai: evidence reordering through harrier.llm.generate_text only (the
    old OpenAI bypass is closed by spec 012's import contract); the model
    returns bullet ID orderings, every ID is validated against the pool
    and the truth documents, and any failure falls back to the
    deterministic plan
- CLI: harrier tailor --job-id N [--jd-file ... | --jd-text ... | --no-ai]
  resolves the tracker row and cached description from the database,
  writes markdown, HTML, PDF, and the internal metadata sidecar under
  the data directory, and updates the tracker row only after the PDF
  gate passes.

## Stated changes from the old code

- All candidate content (bullets, groups, aliases, dimensions, education,
  certifications, phone) moves from code constants into the resume_data
  profile document. The engine is persona-free; the old repo's constants
  become the operator's private bundle data.
- Role handling is data-driven: any number of roles, each with its bullet
  count and default selection, instead of hardcoded r1/r2/r3 constants.
- Evaluation prose (executive conclusion, positioning, tailoring changes)
  is derived from bundle data and dimension outcomes, not hardcoded
  candidate-specific sentences.
- LLM calls go through harrier.llm (the old script constructed provider
  clients directly, one of the two bypasses spec 012 names).
- Outputs land under the data directory (never-in-git), not runtime/.
- Public tests run on a synthetic persona fixture that preserves every
  behavioral pin from the old tests/test_tailor_resume.py without
  reproducing personal facts.

## Acceptance criteria

- [ ] Behavior pins ported from the old tests/test_tailor_resume.py on
      the synthetic fixture: internal-label scrubbing, grounded header,
      completed-anniversary years, frontend evidence never promoted to
      full-stack from the JD, genuine full-stack evidence can support the
      title, JD-relevant skill ordering both directions, ended engagement
      never Present, quantified evidence outranks generic matching,
      unsupported JD technology cannot enter skills, fit evaluation
      statuses and candidate questions
- [ ] A run with a failing PDF gate leaves the tracker row unchanged
- [ ] Bundle validation rejects a plan whose evidence appears twice
      across achievements and experience
- [ ] All gates green on PR

## Proof / origin

Old repo scripts/tailor_resume.py, scripts/resume_facts.py,
tests/test_tailor_resume.py, templates/resume-template.html and .css.
Proving file: services/api/tests/test_resume.py.

## Out of scope

Cover letters and answers (spec 014), offer evaluation (spec 015), the
GUI resume page (spec 016+ surfaces), multi-page resumes, and DB-backed
editing of the bundle through the API (spec 023 owns config/profile
editing surfaces).
