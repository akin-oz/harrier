---
spec: 014
title: Cover letters and application answers
status: in-progress
approved: yes
milestone: M3
depends: [013]
---

# Spec 014: Cover letters and application answers

## Problem

The remaining recruiter-facing artifacts with their style and PDF gates.
The old modules (openai_cover_letters.py, application_answers_lib.py,
openai_answers.py, application_profile.py) carry the same structural
blocker spec 013 hit: candidate-specific prose lives in code (the
deterministic answer builders, salary defaults, prompt lines naming the
candidate and his stack), and the old tests pin personal profile content.

## Scope

- Package harrier.apply:
  - profile: application profile loaded from the profile store (kinds
    application_profile, markdown and json), validation of required
    sections and keys (core_positioning, ai_tooling_workflow,
    professional_stories, engineering_principles, safe_framing,
    question_mapping, style_guidance), and question_guidance lookup
    (exact, substring, AI-tools fallback)
  - answers: LLM path through harrier.llm (payload carries the truth
    sources, candidate document, application profile, and per-question
    guidance; strict-JSON parse; RuntimeError on AI failure), banned
    phrase sanitation on every answer and note, question parsing
    (one-off, questions file with bullet and number stripping, default
    list), markdown rendering (Short and Medium draft sections), outputs
    under data/answers/
  - deterministic answers: the old build_* builders become data-driven
    templates read from the application profile json
    (deterministic_answers keyed by question kind with {company} and
    {product_signal} placeholders; salary values from the candidate
    document's compensation block), with the generic question classifier
    and JD product-signal heuristics staying in code
  - letters: cover letter generation through harrier.llm, response
    normalization (banned phrases stripped, bullet voice and paragraph
    labels removed, stub paragraphs dropped, three paragraphs kept, 240
    word cap), validation (banned phrasing, no bullet voice, three
    paragraphs), markdown with Short and Full sections, HTML from
    templates/cover-letter-template.html and .css (placeholders only;
    the HTML and PDF contain only the full letter, no internal section
    labels), PDF gate via the shared renderer with 16mm margins,
    artifacts under data/cover-letters/
- CLI: harrier cover-letter --job-id N [--jd-file ... | --notes ...] and
  harrier answers --job-id N [--question ... | --questions-file ...]
  [--jd-file ...]; both resolve the tracker row and cached description
  from the database
- The shared PDF renderer (harrier.resume.pdf) gains a margin parameter

## Stated changes from the old code

- Prompts are persona-free: the candidate's name comes from the
  candidate document, and candidate-specific selection guidance moves
  into the application profile's style_guidance, appended to the base
  prompt as data.
- The deterministic answer builders' prose moves from code into the
  application profile json; the engine keeps only the classifier, the
  product-signal heuristic, and template filling.
- Outputs land under the data directory (never-in-git), not runtime/.
- LLM calls go through harrier.llm (the old modules imported llm_client
  directly, which harrier's import contract already permits only via the
  facade).
- Public tests run on a synthetic application profile
  (config/application-profile.example.json and .md, committed) that
  preserves the old pins' mechanics without personal content.

## Acceptance criteria

- [ ] Behavior pins ported from the old tests on the synthetic profile:
      payload context keys, strict-JSON response parsing for letters and
      answers, internal-dump-language normalization, artifacts write
      markdown, HTML, and PDF with the PDF gate failing when no PDF
      appears, HTML contains only the full letter, AI errors surface as
      RuntimeError, questions-file parsing strips bullets and numbers,
      banned phrases never survive sanitation, profile validation passes
      on the committed example, question guidance resolves story IDs
- [ ] Deterministic answers fill their templates from profile data
      (salary from the candidate document, availability, interest with
      the product signal)
- [ ] All gates green on PR

## Proof / origin

Old repo scripts/openai_cover_letters.py, application_answers_lib.py,
openai_answers.py, application_profile.py and their four test files;
templates/cover-letter-template.html and .css. Proving file:
services/api/tests/test_apply.py.

## Out of scope

Outreach drafts (spec 017), the GUI pages (later specs), offer
evaluation (spec 015), and profile editing surfaces (spec 023).
