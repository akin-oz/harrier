# open-source-readiness: seed tasks

Opening assignments per investigator. Follow what you find; these are the
places this repository has already failed.

## readiness-privacy

1. Sweep every committed file for aggregates about the maintainer's tracker: a
   row total, a status distribution, a percentage, "N of M". This is the exact
   pattern that recurred four times after names were scrubbed.
2. Do the same sweep over source comments, docstrings, and test names and
   docstrings, which are the least-reviewed prose here.
3. Check the commit messages and PR description on the current branch. A PR
   body has leaked before.
4. For every number you find anywhere, decide: specification or observation?
5. Verify `config/data-classification.json` covers every tracked path and that
   nothing never-in-git is tracked.
6. Check the fixtures and `config/*.example.*` for any host, slug, or address
   that is not reserved or synthetic.

## readiness-claim-auditor

1. For every acceptance criterion in every shipped spec, find the named test.
   A criterion naming no test is a finding; naming one that does not exist is
   a P0.
2. Spot-check the parity matrix's keep rows against the code that should
   implement them. The matrix has already been found missing a capability.
3. Run the README's commands and compare the output to what it claims.
4. Check each ADR against what the code now does.
5. Look for documents describing the state *before* a change as though it were
   current, which has happened in a proof section here.
6. Check module docstrings, which make behavioural claims and are rarely
   reviewed.

## readiness-fresh-clone

1. Clone to a temp directory and do everything there.
2. Run every README command in order, plus `just check`, `just demo` and
   `just demo-discover`.
3. Find every read of a path under `config/` and check whether the code has a
   fallback that only appears to work because the real file is present locally.
4. Copy each `.example` file to its real name and check that produces a working
   config.
5. Find tests that touch `HARRIER_DATA_DIR`, the home directory, or any
   machine path, and ask whether they would pass with nothing untracked.
6. Check the pinned toolchain against what CI uses.

## readiness-test-integrity

1. Build the executed-versus-unexecuted map before judging anything.
2. Apply the mutation question to the highest-risk assertions: make the code
   wrong in the obvious way and ask whether any test notices.
3. Hunt tests that share an assumption with the code they cover. That is the
   failure that already happened.
4. Hunt guards that fail open: a validator that skips what it cannot parse, a
   comparison treating missing data as agreement, an exit code reporting
   success when the work was refused.
5. Hunt assertions that cannot fail.
6. Check anything gated on an environment variable CI may not supply.

## readiness-publishability

1. Is there a LICENSE? Does the README's claim match it? Do the dependencies
   permit what it grants?
2. Find anything vendored, adapted or copied, and check its licence is
   honoured.
3. Judge fixture provenance: authored, or recorded from a real service?
4. Read the README cold. Is what this is, who it is for, and how to try it
   findable in two minutes?
5. Run the demo and judge what a stranger sees.
6. Is there anything telling a contributor how the spec gate works before they
   open a PR that fails it?
7. Check nothing committed authenticates as anyone.
