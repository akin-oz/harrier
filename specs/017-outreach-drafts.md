---
spec: 017
title: Outreach draft generation
status: shipped
approved: yes
milestone: M4
depends: [016, 012]
---

# Spec 017: Outreach draft generation

## Problem

Both draft paths (template engine and AI), and nothing sends: the
product invariant that no message ever leaves the system automatically
holds here as everywhere.

## Scope

- harrier.outreach grows the draft layer:
  - messages: the template engine ported from outreach_messages_lib.py.
    OutreachRequest validation (audiences recruiter, hiring_manager,
    peer; tones direct, warm, concise, confident), role-profile
    resolution by title match, strength phrases from the outreach
    defaults document, per-variant angle rotation, template render with
    a lint-and-repair loop: banned-language rewrite, company-mention
    repair, the 300-character LinkedIn connection-note trim (phrase
    trimming, then sentence dropping, then a hard ellipsis cut), and
    check_message scoring with flags (too long, missing company, weak
    role alignment, too generic, flattery, generic, vague, banned
    terms). Bundles carry three variants for each of the five message
    kinds; artifacts render to markdown and json under
    data/outreach/messages/; the target store upserts by identity into
    data/outreach/targets.json
  - ai drafts: the LLM path through harrier.llm producing the same
    message-kind structure (three variants per kind with distinct
    angles), strict-JSON parse validating every kind, the 280-character
    hard trim for short connection notes, ai_generated flags with a 90
    baseline score, and the legacy selected-message keys
  - orchestration: infer_audience from the contact title (recruiter,
    hiring-manager, and peer hint sets, recruiter default), the JD from
    an explicit argument or the spec 009 description cache, request
    assembly, selected_messages extraction, and write_outreach_draft
    rendering the sectioned markdown plus json under data/outreach/
- Config split (ADR-008, ADR-009): the outreach defaults document
  (candidate name, headline, strength phrases, targets) lives in the
  profile store (kind outreach_defaults, already migrated); the
  committed config/outreach/role-profiles.json, style-rules.json, and
  templates.json carry no personal identifiers (verified) and stay
  public until spec 023 moves user config into the database. A
  synthetic config/outreach-defaults.example.json documents the
  defaults shape.
- CLI: harrier outreach-draft --job-id N [--contact-linkedin URL]
  [--contact-name ... --contact-role ...] [--audience ...] [--tone ...]
  [--jd-file ...] [--ai]; with --contact-linkedin the contact fields
  resolve from the contacts store

## Inputs, outputs, failure modes

- Inputs: a tracker job (company, role, url), optional contact fields or
  a contacts-store identifier, optional JD text or file, audience, tone,
  and the ai flag. Outputs: a draft payload (messages keyed by kind with
  scored variants, selected_messages, legacy keys) and two artifacts
  (json and sectioned markdown) whose name carries the target identity
  so different contacts, audiences, tones, or modes never overwrite each
  other.
- Failure modes: an invalid audience or tone raises ValueError from
  OutreachRequest validation; a missing outreach_defaults document
  raises ValueError naming the example file; malformed AI JSON, a
  missing message kind, fewer than three variants, or blank variant text
  surface as RuntimeError ("failed to parse AI response"); an
  unavailable AI backend surfaces as RuntimeError ("AI request failed");
  artifact write failures propagate as OSError and the CLI reports them
  through its documented error path; an unknown --contact-linkedin
  identifier is a CLI error, never a silent continue.

## Stated changes from the old code

- The outreach defaults come from the profile store document, not a
  config file with the candidate's name in it.
- The AI prompt is persona-free: the candidate identity rides in the
  payload from the candidate document, never in prompt text.
- The JD comes from the description cache or an explicit argument; the
  draft path no longer performs a live enrichment fetch.
- Artifacts and the target store land under the data directory
  (never-in-git), not runtime/ and tracker/.
- The CLI is tracker-driven (--job-id) like every other spec 013+
  command.

## Acceptance criteria

- [x] Behavior pins ported from the old tests/test_outreach_messages.py
      and test_generate_outreach.py: bundle has three variants for all
      five kinds, connection-note length limit, generic and flattery
      flagging, banned-language rewrite, target-store upsert by
      identity, audience inference, payload assembly, and the template
      path's selected messages and legacy keys
- [x] AI-path pins: response validation rejects a missing kind, fewer
      than three variants, and blank variant text; short notes hard-trim
      to 280; AI errors surface as RuntimeError
- [x] The resolved JD changes the deterministic output, and drafts for
      two different contacts on one job write distinct artifacts
- [x] Nothing sends: the package exposes no transport of any kind
- [x] All gates green on PR (PR #14)

## Proof / origin

Old repo scripts/outreach_messages_lib.py, openai_outreach.py,
generate_outreach.py, outreach_lib.py (draft rendering half),
tests/test_outreach_messages.py, tests/test_generate_outreach.py.
Proving file: services/api/tests/test_outreach_drafts.py. Honest
limitation: the CLI command is a thin shell over the proven library
functions without its own subprocess test.

## Out of scope

Gmail reply watching (spec 018), the daily digest (spec 019), sending
of any kind (never in scope), and moving the committed outreach config
into the database (spec 023).
