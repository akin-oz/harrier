# Contributing

Harrier is one person's job-search automation, published as a working example
of spec-gated development. That shapes what contribution means here: the
project is not looking for feature parity with anyone else's search, and it is
not a general-purpose job board tool.

What is genuinely useful: bug reports with a reproduction, corrections where a
document claims something the code does not do, and portability fixes for
platforms other than macOS.

## Before you start

Read [`CLAUDE.md`](CLAUDE.md). It is compiled from `.ai/` and holds the rules
this repository actually enforces, including the ones below.

## The one rule that surprises people

**No change to observable behavior lands without a spec describing it first.**

Specs live in `specs/NNN-name.md` and gate implementation only when their
frontmatter says `approved: yes`. Only the maintainer flips approval. Every
commit carries a `Spec: NNN` trailer, a local hook blocks commits without one,
and CI resolves every trailer to a spec approved on the base branch.

This means a pull request cannot approve the spec it is implementing. If your
change needs a new spec, propose the spec on its own first.

Bug fixes need a spec only when the correct behavior was never written down.
If the spec already says what should happen, the fix is just the fix, and the
test is the proof. Refactoring changes no behavior by definition and needs no
spec; if a "refactor" requires a spec change, it is not a refactor.

## Setting up

You need [`just`](https://github.com/casey/just),
[`uv`](https://docs.astral.sh/uv/), and [`pnpm`](https://pnpm.io/).

```bash
just check
```

That is the full local gate and it is the same set of recipes CI runs. `just
gate` is the faster subset. `just demo` runs the whole thing offline against
synthetic fixtures, with no API keys.

## What a good change looks like

- **It does what its spec says and nothing else.** Improvements you notice
  along the way are real and belong in their own change. If explaining the
  diff needs the word "also", it should be split.
- **It carries a test that fails without it,** and the test exercises the
  decision rather than the helper the decision calls.
- **It does not add personal data.** This repository is public and its subject
  is a person. Fixtures are authored, not recorded: no real people, no real
  credentials, no real application content, and no counts measured from a real
  tracker. Real company names in job data are fine; real employers in the test
  suite are not. `services/api/tests/test_demo.py` enforces most of this
  mechanically, and `config/data-classification.json` classifies every path.
- **Its documents are true.** Every doc that claims a behavior names the file
  or test that proves it, and limitations sections say what the thing does not
  do.

## Style

Short sentences, plain claims, no marketing language, and no em dashes or
double dashes as punctuation anywhere, including commit messages. Use colons,
parentheses, or another sentence. CLI flags like `--dry-run` are syntax, not
punctuation.

## Reporting a security issue

See [`SECURITY.md`](SECURITY.md). Do not open a public issue for one.
