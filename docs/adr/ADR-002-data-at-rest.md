# ADR-002: Data at rest in one public repo

- Status: partially superseded by ADR-008 (2026-08-08): the encrypted-in-repo
  class and the SOPS mechanics are dropped; personal data lives in the local
  database instead. The classification reasoning and the credentials-never-in-git
  refinement stand.
- Date: 2026-08-08

## Context

Harrier is one public repo. The system handles three kinds of sensitive data today
(paths cite the old repo):

1. Candidate PII: name, email, LinkedIn, employment history, verified resume content,
   application narrative (`config/candidate.json`, `config/resume-candidate-data.json`,
   `config/resume-truth-source.md`, `config/application-profile.{md,json}`,
   `config/outreach/defaults.json`).
2. Third-party PII: recruiter and contact names, emails, LinkedIn URLs
   (`tracker/contacts.csv`, `runtime/outreach/**`), plus mailbox-derived events
   (`gmail_handler.log`).
3. Live credentials: the live API keys in `.env` (OpenAI, Anthropic, Apify, Telegram, Hunter,
   Gmail OAuth paths) and Google OAuth client and token JSON in `secrets/`.

A stranger must be able to clone and run demo mode with none of it. In the old repo,
`config/` is committed unencrypted, and `tailor_resume.py` embeds verbatim resume
bullets in source code. Both patterns end here.

## Data classification

Exactly one class per path. Classes:

- **public**: committed plaintext.
- **encrypted-in-repo**: committed, encrypted, decryptable only by Akin's key.
- **never-in-git**: exists only on the machine (or OS keychain); gitignored; a
  committed `.example` template documents its shape.

| New-repo path (old-repo origin) | Class |
|---|---|
| All source code, docs, specs, `.ai/`, CI | public |
| Templates (`templates/resume-template.html` etc.) with placeholder content only | public |
| Demo fixtures: fake candidate, synthetic jobs, scrubbed importer recordings | public |
| `config/feeds.txt`, `config/linkedin_search_urls.txt` | public (board URLs and search queries carry no PII; review before first publish) |
| `config/companies-hold.csv`, `config/reapply-hold-companies.md` | public (company names and policy only) |
| Scoring config structure with default weights (`config/candidate.json` shape) | public as `candidate.example.json`; the real one is encrypted-in-repo |
| `private/candidate.json` (real profile, salary targets) | encrypted-in-repo |
| `private/resume-candidate-data.json`, resume truth sources, achievements | encrypted-in-repo |
| `private/application-profile.{md,json}`, `private/outreach-defaults.json` | encrypted-in-repo |
| Resume bullet pool (extracted from `tailor_resume.py` source) | encrypted-in-repo |
| Interview prep, story bank (`interview-prep/`) | encrypted-in-repo |
| Tracker data (`tracker/jobs.csv`, `contacts.csv`, the SQLite file per ADR-003) | never-in-git |
| Generated artifacts (`runtime/**`: resumes, letters, answers, outreach drafts) | never-in-git |
| Evaluation reports (`reports/`) | never-in-git |
| Discovery state, description cache, gmail state (`state/**`) | never-in-git |
| Run outputs (`incoming/**`), logs (`logs/**`, `gmail_handler.log`) | never-in-git |
| `.env` and every API key | never-in-git (`.env.example` committed) |
| Gmail OAuth client secret and token (`secrets/`) | never-in-git |

Rationale for the tracker being never-in-git rather than encrypted-in-repo: it mutates
many times daily (four scheduled runs plus manual actions), so committing it means either
constant encrypted-blob churn in history or a stale copy; and it contains third-party
PII whose only consumer is this machine. Encrypted backup happens outside git.

## Constraint refinement (requires explicit approval)

The stated constraint is "PII and secrets are stored encrypted" in the repo. I recommend
refining it: **PII is encrypted-in-repo; live credentials are never in git, even
encrypted.**

Argument: git history is forever. An encrypted blob is one key-compromise, one tool CVE,
or one misconfigured `.gitattributes` away from being a permanent public leak, and
credentials are the one class where the damage is immediate and external (mailbox access,
paid API spend). Tokens also rotate: Gmail OAuth tokens refresh constantly, so the
committed copy is stale within hours, which means the repo carries the risk without even
the benefit. Encrypting the candidate profile in-repo is defensible because it changes
rarely and syncing it has real value; encrypting `.env` in-repo is risk without value.

This refinement is not applied silently. If rejected, the mechanics below extend to a
`secrets.enc` file with the same tooling; the classification table rows for `.env` and
`secrets/` flip to encrypted-in-repo.

## Options for the encryption mechanics

### git-crypt

Transparent GPG/symmetric-key filter; files decrypt on checkout. Cons: smudge/clean
filters fail silently when the filter is not installed (a fresh clone shows binary
blobs, and a misconfigured `.gitattributes` line commits plaintext with no error);
all-or-nothing per file; GPG dependency; effectively unmaintained pace; no partial-file
encryption, so JSON diffs are opaque.

### transcrypt

Same smudge/clean model with OpenSSL; simpler than git-crypt but shares the failure
mode: correctness depends on client-side git config being present, and the history
contains ciphertext that silently becomes the working copy on any un-configured clone.
Salted deterministic encryption also leaks when a file changed and how much.

### SOPS with age (recommended)

Files are encrypted at rest in the working tree, not just in git; encryption is an
explicit `sops` operation, so there is no silent-plaintext failure mode from a missing
filter. age keys are small, modern, and CI-friendly (one env var). SOPS encrypts values
but keeps structure for JSON/YAML, so diffs show which keys changed without exposing
content. Markdown and other opaque files are handled as whole-file encryption. The
`.sops.yaml` creation rules are testable: a unit test asserts every path classified
encrypted-in-repo matches a rule (see below). Cons: an explicit encrypt/edit step
(`sops edit`) instead of transparent checkout; contributors see ciphertext, which for
this repo is the point.

## Decision

SOPS with age. One age keypair held by Akin (and a backup), public key in `.sops.yaml`,
private key never in the repo. Encrypted files live under `private/` with an `.enc.`
name segment so classification is visible in the path itself. The application loads
decrypted copies from a gitignored `private/decrypted/` produced by `just decrypt`;
demo mode never calls it.

## Required regardless of tool

- **Secret scanning**: gitleaks in pre-commit (via the hook chain) and as a CI job on
  every PR and push to main. The gitleaks config adds patterns for this project's shapes
  (Apify tokens, Telegram bot tokens, age private keys).
- **Coverage test**: an automated test (`tests/test_classification_coverage.py` in the new
  repo) that walks the classification table (kept machine-readable in
  `config/data-classification.json`), asserts every encrypted-in-repo path matches a
  `.sops.yaml` rule and is actually encrypted in HEAD, and asserts every never-in-git
  path is matched by `.gitignore` and absent from the index.
- **Log redaction**: the logging setup redacts values loaded from the private config
  (candidate name, email, contact emails) and never logs LLM prompts at info level;
  prompt logs (the old `logs/<provider>.log` behavior) are opt-in debug and classified
  never-in-git.
- **Pre-publish checklist**: run before the repo ever goes public, documented in
  `docs/privacy-plan.md`: full-history gitleaks scan, coverage test green, manual review
  of every committed file, fixture scrub verification, and a second pair of eyes on
  `git log --stat` for the whole history.

## Consequences

- Clone-and-run demo works with zero keys: demo fixtures are public, the app boots in
  demo mode when `private/decrypted/` is absent.
- Losing the age key means re-encrypting from local plaintext; the key gets an offline
  backup at creation time.
- Real tracker data never gains git history; its backup story is local (Time Machine
  plus the CSV export from ADR-003), stated honestly in the README limitations.
