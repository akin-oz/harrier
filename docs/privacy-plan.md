# Privacy plan

The repo is public; the domain is one real person's job search plus third-party
contacts. This plan operationalizes ADR-002. Spec 003 implements it; spec 021 proves
it before publish.

## 1. Classification

The full table lives in `docs/adr/ADR-002-data-at-rest.md` and, machine-readable, in
`config/data-classification.json` (spec 003). Three classes, exactly one per path:
public, encrypted-in-repo, never-in-git. The standing rule (`.ai/rules/privacy.md`):
a new path gets classified before it gets content; an uncovered path is a spec-worthy
gap, not a judgment call.

Approved refinement: PII is encrypted-in-repo; live credentials are never in git,
even encrypted.

## 2. Mechanics

- SOPS with age. Public key in `.sops.yaml`; private key local plus one offline
  backup, never committed. Encrypted files under `private/` with `.enc.` in the name.
- The app reads `private/decrypted/` (gitignored), produced by `just decrypt`.
  Demo mode never decrypts; it boots from `fixtures/`.
- Credentials live in `.env` (gitignored) and `secrets/` (gitignored);
  `.env.example` documents every key with placeholder values.
- Migration hygiene: when porting content from the old repo, the plaintext originals
  stay in the old repo. Nothing from `~/job-hunt-local/config/`,
  `tracker/`, `runtime/`, `reports/`, or `interview-prep/` enters harrier except
  through SOPS encryption or fixture synthesis. The old repo's git history already
  contains PII (its `config/` was committed in plaintext); that history must never be
  imported, so harrier starts from an empty history, which it does.

## 3. Scanning

- gitleaks in pre-commit (lefthook) and in CI on every PR and push to main.
- Custom rules for this project's shapes: Apify tokens, Telegram bot tokens, Hunter
  keys, age secret keys (`AGE-SECRET-KEY-`), Google OAuth client JSON fields.
- The commit guard (`.claude/hooks/guard-commit.sh`) independently refuses to stage
  or commit `.env*` files.

## 4. Coverage test

`tests/test_encryption_coverage.py` (spec 003), run locally and in CI:

1. Every encrypted-in-repo path in the classification table matches a `.sops.yaml`
   creation rule.
2. Every such file tracked in HEAD is actually ciphertext (has the `sops` metadata
   envelope; plaintext fails).
3. Every never-in-git path is matched by `.gitignore` and absent from the index.
4. Every tracked file maps to some class (no unclassified tracked paths).

## 5. Log redaction

- The logging setup loads candidate and contact identity values from the private
  config at startup and installs a redaction filter for them.
- LLM prompts and artifact contents log at debug only; debug logs are never-in-git.
- The old repo's per-provider prompt logs (`logs/<provider>.log`) carry over as
  opt-in debug with the same never-in-git class.

## 6. Fixture policy

- Fixtures are synthetic (invented candidate, invented contacts) or scrubbed
  recordings (real importer responses with every person name, email, profile URL,
  and free-text-about-a-person field replaced).
- Real company names and public job titles are acceptable; real people are not.
- Every fixture addition gets a privacy-reviewer pass (`.ai/agents/privacy-reviewer.md`).

## 7. Pre-publish checklist

Run in full before the repo goes public (spec 021 acceptance), and again after any
history rewrite:

- [ ] `gitleaks detect` over the full history, zero findings.
- [ ] Encryption coverage test green.
- [ ] `git ls-files` reviewed file by file; every file's class confirmed.
- [ ] `git log --stat` for the whole history reviewed for accidental adds.
- [ ] Fixtures re-checked by privacy-reviewer; no real person recoverable.
- [ ] `.env.example` contains placeholders only.
- [ ] README limitations section states what privacy this design does and does not
      provide.
- [ ] Second pass by Akin, not the agent, before flipping visibility.

## Honest limitations

- Encrypted-in-repo protects against repo readers, not against a compromised local
  machine or a leaked age key. Key compromise means rotating the key and re-encrypting
  going forward; already-public history with the old ciphertext remains decryptable
  by the leaked key. Mitigation is key custody, not cryptography.
- Third-party PII (contacts) never enters git at all; its only store is the local
  tracker, backed up outside git.
