# Privacy plan

The repo is public; the domain is one real person's job search plus third-party
contacts. This plan operationalizes ADR-002 as revised by ADR-008: no personal
data enters git in any form, encrypted or not. Spec 003 (amended) implements it;
spec 021 proves it before publish.

## 1. Classification

Two classes, exactly one per path, machine-readable in
`config/data-classification.json`:

- **public**: source, docs, specs, governance, synthetic fixtures, PII-free config.
- **never-in-git**: all personal data (in the local SQLite database per ADR-008,
  or local files), credentials, artifacts, state, logs, backups.

The standing rule (`.ai/rules/privacy.md`): a new path gets classified before it
gets content; an uncovered path is a spec-worthy gap, not a judgment call.

## 2. Where personal data lives

- Candidate content (profile, resume truth sources, achievement blocks, bullet
  pool, application narrative, outreach defaults, interview prep, story bank)
  lives in database tables owned by `harrier.profile` (spec 004).
  `harrier profile import` migrates it once from the old repo;
  `harrier profile export` supports editing round-trips.
- Tracker rows and contacts live in the same database (ADR-003).
- Credentials live in `.env` and `secrets/`, both gitignored; `.env.example`
  documents every key with placeholders.
- Backup is entirely local: `just backup` writes a timestamped archive of the
  data directories to `~/Backups/harrier/`, outside the repo. Time Machine
  covers the machine. The repo cannot restore personal data, by design.
- Migration hygiene: plaintext originals stay in the old repo. The old repo's
  git history already contains PII (its `config/` was committed in plaintext);
  that history is never imported and the old repo stays private forever.

## 3. Scanning

- gitleaks in pre-commit (lefthook) and in CI on every PR and push to main,
  plus a weekly full-history scan.
- Custom rules for this project's shapes: Apify tokens, Telegram bot tokens,
  age secret keys, Google OAuth client fields (`.gitleaks.toml`).
- The commit guard (`.claude/hooks/guard-commit.sh`) independently refuses to
  stage or commit `.env*` files.

## 4. Coverage test

`tests/test_classification_coverage.py` (spec 003 as amended), run locally and in CI:

1. Every never-in-git pattern is matched by `.gitignore` (probe check).
2. No tracked file matches a never-in-git pattern.
3. No encrypted-layer artifact returns (`.sops.yaml`, `private/`, `.enc.` files):
   ADR-008 removed the class and the test pins the removal.

## 5. Log redaction

- The logging setup loads candidate and contact identity values from the
  database at startup and installs a redaction filter for them
  (`harrier.logredact`, installed by `configure_logging`, proven by
  `services/api/tests/test_logging.py`).
- LLM prompts and artifact contents log at debug only; debug logs are
  never-in-git.

This sentence was false from spec 029 until spec 045: there was no
`logging.Filter` anywhere in the tree, and `configure_logging` was called only
by the CLI, so the process serving the browser had neither. It is written here
because a public repository that claims a privacy control it never built is
worse than one that admits the gap, and the record of having done it once is
the cheapest defence against doing it again.

What redaction does not do: it matches literal values read once at startup, so
a paraphrase, a different spelling, or a contact added later in the same
process is not caught, and values shorter than four characters are ignored
deliberately. Logs are never-in-git regardless, so this defends the log that
gets pasted into an issue or shared on a call, not the repository.

## 6. Fixture policy

- Fixtures are authored, not recorded (revised by spec 021). Scrubbing a real
  importer response makes privacy a review step that can be skipped; authoring
  means nothing real was ever in the file. The cost is that fixtures pin the
  shapes the importers parse rather than whatever a live board returns today.
- Real people are never acceptable. Real company names are no longer used
  either: a fixture board slug is the one field where a real employer can ride
  into a public file unnoticed, so every host is a reserved example name or an
  ATS API endpoint, and every board slug begins with `example`.
- Those three rules are tests, not intentions
  (`services/api/tests/test_demo.py`, the privacy pass at the bottom): hosts,
  board slugs, and email addresses across every file under `fixtures/` and
  every `config/*.example.*`.
- The tests cannot judge whether a synthetic persona resembles a real person.
  Every fixture addition still gets a privacy-reviewer pass
  (`.ai/agents/privacy-reviewer.md`).

## 7. Pre-publish checklist

Run in full before the repo goes public (spec 021 acceptance), and again after
any history rewrite:

- [ ] `gitleaks detect` over the full history, zero findings.
- [ ] Classification coverage test green.
- [ ] `git ls-files` reviewed file by file; every file's class confirmed.
- [ ] `git log --stat` for the whole history reviewed for accidental adds
      (including the pre-ADR-008 commits: the only ever-committed private file
      was an encrypted placeholder with no personal content; verify nothing
      else slipped in).
- [ ] Fixtures re-checked by privacy-reviewer; no real person recoverable.
- [ ] `.env.example` contains placeholders only.
- [ ] README limitations section states what privacy this design does and does
      not provide.
- [ ] A license file exists. Without one, default copyright applies and
      "open source" is not what publishing produces.
- [ ] Second pass by Akin, not the agent, before flipping visibility.

The mechanical items above are covered by the suite and re-run on every commit:
the classification coverage test, gitleaks (lefthook pre-commit and CI), and
the fixture privacy pass. The rest are human steps by construction. The last
two lines are Akin's alone; an agent cannot sign off on either.

## Honest limitations

- Everything personal has exactly one home: this machine plus local backups.
  Losing both loses the data; the repo cannot help, by design (ADR-008).
- Never-in-git protects the repo, not the machine. Local disk encryption
  (FileVault) and backup custody are outside this plan's control and are the
  user's responsibility.
