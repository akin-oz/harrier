---
description: PII and secret boundaries for a public repo
---

This repo is public. Every path has exactly one class in
`config/data-classification.json`: public, encrypted-in-repo, or never-in-git
(ADR-002).

- Candidate PII (profile, resume truth sources, application narrative, interview prep)
  is SOPS-encrypted under `private/`. Never commit it in plaintext, never paste its
  contents into public files, fixtures, tests, code, or commit messages.
- Live credentials never enter git, even encrypted. `.env` and OAuth material stay
  local; committed `.example` files document shapes with placeholder values only.
- Tracker data, generated artifacts, reports, state, and logs are never-in-git.
- Fixtures must be synthetic or scrubbed. Real company names in job data are fine;
  real people, real credentials, and real application content are not.
- Logs redact candidate and contact identity values. LLM prompt logging is opt-in
  debug only and never committed.
- When adding any new file or directory, classify it first. If the classification
  table does not cover it, that is a spec-worthy gap, not a judgment call.
