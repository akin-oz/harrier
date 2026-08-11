---
spec: 028
title: Agent teams: principal review and open-source readiness
status: in-progress
approved: yes
milestone: M6
depends: [003]
---

# Spec 028: Agent teams: principal review and open-source readiness

## Problem

harrier has four standing guardians (contract, data integrity, FSD,
privacy). Every one of them checks **compliance**: is the documented rule
followed. None of them exercises **judgement**: is the documented rule the
right rule, and is the thing any good.

That gap is not theoretical here. A repository can be perfectly consistent
with a design that was wrong to choose, and this one carries a numbered spec
per change (twenty-eight in `specs/` as this one lands), a spec gate, commit
trailers and CI resolution for a tool with one user. Nothing in the governance is allowed to ask whether that is
proportionate, because every reviewer is scoped to checking conformance to
it.

The second gap is publication. The repo is public and the domain is a real
person's job search. It has no reviewer whose job is "would this survive
being read by a stranger", and its recent history says it needs one.

## Scope

Two agent teams under `.ai/agent-teams/`, each a `launch.md` (the board,
its members, and the prompt that convenes it) and a `tasks.md` (opening
assignments), mirrored to `.claude/agent-teams/` where Claude Code reads
them.

aie 0.2.0 compiles `.ai/agents/` into `.claude/agents/` and has **no notion
of agent teams**: neither this repository's target state nor the reference
project's records them, so the reference project's `.claude/agent-teams/`
is a hand-maintained copy with nothing keeping it in step. The mirror here
is therefore enforced by a test rather than by the compiler, which is the
one place this deviates from the pattern it was taken from, deliberately.

**principal-review**, five read-only reviewers plus a merging lead, run at
a milestone or before defending the work. A critique, not a gate.

**open-source-readiness**, five read-only investigators, run before the
repo is published or shown. The equivalent of a delivery sweep, scoped to
the one thing that matters here: this repository goes on the internet
attached to a real person.

Every member reports `file:line: what. Fix:` at P0/P1/P2, declines findings
belonging to another lens, and names the class of what it found rather than
the value, so a privacy report is not a second copy of the leak.

The two boards do not get the same tools, and the first draft of this spec
was wrong about that. It gave all ten `Read, Glob, Grep, Bash` and called
them read-only, which `Bash` is not: a shell can write, install, push and
reach the network. Corrected on review (PR #29):

- the five `review-*` reviewers hold `Read, Glob, Grep` and no `Bash`. They
  read code and judge it; nothing in their instructions executes anything,
  so the grant was house style rather than need. Read-only is now a property
  of the tool list, not a sentence in the prose.
- the five `readiness-*` investigators keep `Bash`, because executing is the
  lens: the fresh-clone engineer clones and builds, test integrity runs the
  suite, the claim auditor runs the README's commands, publishability runs
  the demo. Each is instructed to work in a temporary copy and change
  nothing in the checkout it was launched from.

That instruction is an instruction, not a sandbox, and the difference is
stated in both launch documents rather than papered over. Enforcing it would
need a runtime deny policy on `Bash`, which is out of scope here and noted
below.

## Why these lenses and not others

Copying another repository's board would produce reviewers for failures
this repository has never had. Each lens below exists because harrier has
already failed that way, or because the design makes a real choice that
deserves defending.

**open-source-readiness**, all five from the record:

| Investigator | Exists because |
|---|---|
| `readiness-privacy` | Real tracker measurements reached public specs, source comments, test docstrings and a PR body on four separate occasions, each immediately after the previous one was scrubbed. Board names went first; counts and status distributions kept going. |
| `readiness-claim-auditor` | A spec cited a test that did not exist. Another declared a waiting state and a disconnect state that were never built. A third said a failed run stays collapsed while the code expanded it. A proof section described the pre-change tree as current. |
| `readiness-fresh-clone` | A test passed locally by reading a gitignored file that exists only on the author's machine, and failed on CI. The demo's clean-clone claim was checked by `git status` rather than by a clone. |
| `readiness-test-integrity` | The suite stayed green through that same defect. `diff_runs({}, {})` reported clean. A parser skipped every row under a mistyped header and reported success. |
| `readiness-publishability` | There is no LICENSE, so publishing produces a public repository that is not open source. Nothing checks the fixtures' provenance, the third-party attribution, or whether a stranger can get the demo running. |

**principal-review**, five choices worth defending:

| Reviewer | The question |
|---|---|
| `review-principal-architect` | That many specs, four guardians and a trailer gate for a single-user tool: proportionate, or paperwork? Several approved specs were corrected by their own implementation. |
| `review-domain-model` | Every tracker column is a string, including scores, dates and enums. Statuses have no transitions. What can the model not express, and what does it permit that is illegal? |
| `review-screening` | Most of the tracker is rejected and scores cluster well above the cutoff. Is the score discriminating, or is the cutoff dead? |
| `review-honesty-gates` | The resume validator checks generated lines against a truth document, so it can only catch what that document contradicts. Is the honesty invariant real or nominal? |
| `review-operability` | One machine, launchd, no auth, and a scheduled job that failed silently for two months. Can the operator tell that it is working? |

## Inputs, outputs, failure modes

- Inputs: the repository, read-only. No team writes code, specs, or
  commits.
- Outputs: per-member P0/P1/P2 reports, merged by the lead into one
  deduplicated list ordered by severity.
- Failure modes: agent teams are experimental and gated behind
  `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`; without it the boards do not
  convene and the launch documents say so.

## Acceptance criteria

- [x] both teams exist under `.ai/agent-teams/` with launch and tasks, the
      ten members compile into `.claude/agents/`, and `aie check` is clean
- [x] the `.claude/agent-teams/` mirror is byte-identical to the source, and
      a test fails when it drifts, since aie will not do this
- [x] every member named in a launch document exists as an agent file, and
      every agent file is named in exactly one launch document
- [x] no member can write: `Write` and `Edit` are absent from all ten, in
      both the `.ai/` source and the compiled `.claude/` copy that Claude
      actually loads
- [x] the `review-*` board holds no `Bash`, so its read-only claim is
      enforceable rather than asserted
- [x] each readiness lens cites the failure that motivated it
- [x] a test asserts the two directions of membership, so a renamed agent
      or an orphaned one fails CI rather than being discovered when the
      board is convened
- [ ] All gates green on PR

Every criterion above maps to a test in
`services/api/tests/test_governance.py`, one per line:

| Criterion | Proof |
|---|---|
| both teams exist with launch and tasks | `test_every_team_has_a_launch_and_a_tasks_document` |
| the ten members compile into `.claude/agents/` | `test_every_team_member_compiles_into_the_claude_directory` |
| `aie check` is clean | the `Governance drift (aie check)` CI job, not a pytest case |
| the `.claude/` mirror does not drift | `test_the_compiled_copy_matches_the_source` |
| every named member exists | `test_every_member_named_in_a_launch_document_exists` |
| every agent belongs to exactly one team | `test_every_team_agent_is_claimed_by_exactly_one_team` |
| no member can write | `test_no_team_member_can_write`, over `.ai/` and `.claude/` both |
| the review board cannot execute | `test_review_board_members_cannot_execute_anything` |
| each readiness lens cites its failure | `test_every_readiness_lens_cites_the_failure_that_motivated_it` |
| a hand-off is not membership | `test_a_handoff_reference_is_not_read_as_membership` |

The final criterion, all gates green on PR, is CI and has no test of its
own.

The membership test failed on its first run for a real reason: a launch
document names the *other* team's agent when handing a lens off, and
reading that as membership made `principal-review` look six strong.
Extraction is scoped to the roster table, pinned by
`test_a_handoff_reference_is_not_read_as_membership`.

## Proof / origin

Two of the author's other repositories, read for the pattern and neither
modified: a launch document that convenes the board, seed tasks per member,
members with one lens each, and a lead that merges. They are private and are
not named or located here, because a path is an identity and this repository
is public (ADR-008).

The distinction this spec turns on comes from one of them: the guardians
check compliance, the board exercises judgement, and a repository can be
perfectly consistent with a design that was wrong to choose.

## Out of scope

Running either board and acting on its findings, which is the next session's
work and needs the experimental flag. A runtime deny policy that would make
the investigators' temporary-copy rule enforceable rather than instructed:
worth doing, and its own change. Changing any existing guardian. Any
new runtime target beyond the claude and codex ones the blueprint declares.
