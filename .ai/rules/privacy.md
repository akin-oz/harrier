---
description: PII and secret boundaries for a public repo
---

This repo is public. Every path has exactly one class in
`config/data-classification.json`: public or never-in-git (ADR-002 as revised by
ADR-008). There is no encrypted-in-repo class.

- All personal data (candidate profile, resume truth sources, bullet pool,
  application narrative, interview prep, tracker rows, contacts) lives in the
  local database or local files, never in git in any form. Never paste its
  contents into public files, fixtures, tests, code, or commit messages.
- Live credentials never enter git. `.env` and OAuth material stay local;
  committed `.example` files document shapes with placeholder values only.
- Generated artifacts, reports, state, logs, and backups are never-in-git.
- Fixtures must be synthetic or scrubbed. Real company names in job data are
  fine; real people, real credentials, and real application content are not.
- Logs redact candidate and contact identity values. LLM prompt logging is
  opt-in debug only and never committed.
- When adding any new file or directory, classify it first. If the
  classification table does not cover it, that is a spec-worthy gap, not a
  judgment call.
