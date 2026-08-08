# ADR-008: Personal data lives in the local database, not in the repo

- Status: accepted
- Date: 2026-08-08
- Supersedes: the encrypted-in-repo class of ADR-002

## Context

ADR-002 defined three classes: public, encrypted-in-repo (SOPS with age), and
never-in-git. Akin challenged the middle class: personal data should not sit in a
public repo at all, encrypted or not. Job tracking was already never-in-git; this
decision extends the same treatment to all candidate content.

## Decision

There is no encrypted-in-repo class. Two classes remain:

- **public**: source, docs, specs, governance, synthetic fixtures, PII-free config.
- **never-in-git**: everything personal, in the local SQLite database (ADR-003)
  or local files; gitignored; committed `.example` files document shapes.

Candidate content moves into the database as first-class tables (owned by
`harrier.profile`, migrated in spec 004): candidate profile, resume truth sources
and achievement blocks, the resume bullet pool, application profile and narrative,
outreach defaults, interview prep and story bank. The CLI gains
`harrier profile import` (one-shot from the old repo's config files) and
`harrier profile export` for editing round-trips. Live credentials stay in `.env`
and `secrets/`, never in git (unchanged from the approved ADR-002 refinement).

SOPS, age, the `.sops.yaml` rules, the `private/` directory, and `just decrypt`
are removed. gitleaks, the classification table, the coverage test (reduced to
its never-in-git assertions), log redaction, and the pre-publish checklist all
remain: they were required regardless of tool and still are.

## What this buys and what it costs

Buys: no key custody (nothing to lose that unlocks personal data in a public
history), no ciphertext blobs in a public repo, one store for all personal data
with one backup story, simpler onboarding for strangers (nothing to explain about
encryption), a smaller privacy attack surface.

Costs, stated honestly:

- Profile content has no git history. Edits to resume truth sources are not
  versioned by the repo. Mitigation: the database keeps an `updated_at` per row,
  and `harrier profile export` allows keeping a private local copy under any
  versioning the user wants, outside this repo.
- No multi-machine sync via the repo. This is a single-machine tool (stated in
  the architecture limitations already); a new machine needs a database restore.
- Backup is now entirely local: `just backup` snapshots `data/` (database and
  local state) to a timestamped archive outside the repo. Time Machine covers
  the rest. Losing the machine and the backups means losing the data; the repo
  cannot help, by design.

## Consequences

- `docs/adr/ADR-002-data-at-rest.md` remains as the record of the classification
  reasoning and the credentials-never-in-git refinement; its encrypted-in-repo
  mechanics section is superseded by this ADR.
- Spec 003 is amended: its encryption scope is replaced by classification,
  scanning, and coverage; already-shipped gitleaks and coverage work stands.
- Spec 004 grows the profile tables and import. Specs 013, 016, 017 read
  candidate content from the database instead of a decrypted file layer.
- The age keypair generated during the first version of spec 003 protects
  nothing once this lands and may be deleted.
