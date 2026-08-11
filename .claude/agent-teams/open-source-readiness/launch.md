# open-source-readiness

A task force that sweeps the repository from five angles before it
is published or shown, and returns one severity-ranked list of gaps the
maintainer can act on. Nothing here mutates this checkout, writes a
specification, or commits: the lenses that must execute do it in a temporary
copy.

The point is to find the leaked personal detail, the document that promises
something the code does not do, the test that cannot fail, and the missing
licence, before a stranger does.

This repository is public and its subject is a real person's job search.
That is the whole reason this team exists and outranks every other concern in
it.

## Why these five lenses

Each investigator exists because this repository has already failed in that
specific way at least once:

| Investigator          | Subagent                     | Exists because |
| --------------------- | ---------------------------- | -------------- |
| Privacy               | `readiness-privacy`          | Real tracker measurements reached public specs, source comments, test docstrings and a PR body on four separate occasions, each right after the previous one was scrubbed. Names went first; counts and distributions kept going. |
| Claim auditor         | `readiness-claim-auditor`    | A spec cited a proving test that did not exist. Another declared a waiting state and a disconnect state that were never built. A third required a failed run to stay collapsed while the code opened it. |
| Fresh-clone engineer  | `readiness-fresh-clone`      | A test passed locally by reading a gitignored file that exists only on the maintainer's machine, and failed on CI. A clean-clone claim was checked with `git status` rather than a clone. |
| Test integrity        | `readiness-test-integrity`   | The suite stayed green through that same defect. A run diff called two empty objects identical, a parser skipped every row under a mistyped header and reported success, and a dry run printed "nothing was changed" while hiding the blockers. |
| Publishability        | `readiness-publishability`   | There is no LICENSE, so publishing produces a public repository that is not open source. Nothing checks fixture provenance or whether a stranger can get the demo running. |

All five are `tools: Read, Glob, Grep, Bash`. They are **not** read-only,
and calling them that would be the exact kind of claim this team exists to
catch: every one of these lenses has to execute something. The fresh-clone
engineer clones and builds, test integrity runs the suite, the claim auditor
runs the README's commands, publishability runs the demo, and privacy reads
git history. What each is instead told, in its own file under `.ai/agents/`,
is to work in a temporary copy and change nothing in the checkout it was
launched from.

The `principal-review` board is read-only in the enforceable sense: those
five have no `Bash` at all.

Three run on `opus` (privacy, claims, and test integrity) because each
requires reasoning about what is *absent* or what is quietly untrue, which is
the harder judgement.

## How to run it

Agent teams are experimental and gated behind an environment flag:

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

Then paste the prompt below into a session at the repository root. The five
run in parallel; the session that launched them is the lead and merges their
findings.

---

> Run the **open-source-readiness** task force over this repository before it
> is published or shown. Launch these five investigators **in
> parallel**, each scoped to the whole repository, and merge their findings
> into one severity-ranked checklist at the end.
>
> Context they all share: harrier is a local-first job search tool for a
> single user, public on GitHub while its subject is a real person's job
> search. Personal data lives only in a local SQLite database and gitignored
> files, never in git (ADR-008); user configuration is user data too
> (ADR-009). `.ai/` is the source of truth and `.claude/`, `AGENTS.md` and
> `CLAUDE.md` are generated from it. Every change is gated on an approved
> spec under `specs/`. A demo mode seeds synthetic fixtures and reaches no
> network, and is the only thing a stranger will run.
>
> 1. **Privacy**, subagent `readiness-privacy`. Could a stranger learn
>    anything about the maintainer's actual search? Aggregates count as much
>    as names do: a row total, a status distribution, a percentage, any "N of
>    M" phrasing. Check prose, source comments,
>    test names and docstrings, fixtures, examples, and the commit messages
>    and PR text on this branch. For every number, decide whether it is a
>    specification or an observation.
> 2. **Claim auditor**, subagent `readiness-claim-auditor`. Is anything the
>    repository says about itself untrue of the code? Resolve every spec
>    acceptance criterion against the test it names, spot-check the parity
>    matrix's keep rows against the code, and run the README's commands.
> 3. **Fresh-clone engineer**, subagent `readiness-fresh-clone`. Clone to a
>    temp directory and work there. Would `just check`, `just demo` and the
>    README's commands succeed with nothing untracked present?
> 4. **Test integrity**, subagent `readiness-test-integrity`. Would this
>    suite fail if the code were wrong? Build the executed-versus-unexecuted
>    map, then hunt guards that fail open and tests that share an assumption
>    with the code they cover.
> 5. **Publishability**, subagent `readiness-publishability`. Is there a
>    licence, is the fixture provenance clean, and would a stranger get the
>    demo running in two minutes?
>
> Every report names the file, the line, and the class of data or defect. No
> report quotes the value it found: not the name, the count, the date, the
> path, the handle, or the token. A privacy report that pastes the leak into
> itself is a second copy of the leak in a document written to be shared.
>
> Have each return its own severity-ranked report. Then produce one merged
> checklist ordered by severity across all five, deduplicated, with the one
> item you would fix first called out. Anything that would leak personal data
> on publication is P0 regardless of what else is found.

---

## Scope discipline

Each investigator has exactly one lens and should decline findings that
belong to another. Overlap between five reports is noise the lead has to
reconcile, and a finding reported five ways is not five findings.

- A document that is untrue belongs to the **claim auditor**, even when the
  untrue thing is about privacy.
- A test that cannot fail belongs to **test integrity**, even when it is a
  privacy test.
- A missing file that breaks a clone belongs to **fresh clone**; a missing
  file that breaks the licence belongs to **publishability**.
- Judgement about whether the design is any good belongs to the
  `principal-review` board, not here.

Seed tasks for each investigator are in `tasks.md`.
