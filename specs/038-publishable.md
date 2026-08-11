---
spec: 038
title: Publishable: a licence, and guardians that are what they say
status: accepted
approved: yes
milestone: M6
depends: [028]
---

# Spec 038: Publishable: a licence, and guardians that are what they say

## Problem

The repository is public, the README invites a stranger to clone it and run
the demo, and the project describes itself as a showcase. There is no LICENSE
file, so default copyright applies and nobody may legally use, modify, or
redistribute any of it. Spec 028 records this as the reason the
`readiness-publishability` lens exists, which means it is known and unfixed.

It is a five minute change that has been gated behind writing a specification,
which is itself a data point the architect lens raised about proportionality.

Two related items make the same kind of claim-versus-reality gap:

- The four standing guardians hold `Bash` while their own prose calls them
  read-only. Spec 028 fixed exactly this for the ten new agents and left the
  original four, so the repository now states the correct distinction for one
  set of agents and the incorrect one for the other.
- `contract-guardian` is instructed to run the contract generation command,
  which writes the artifact that the source-of-truth guard exists to protect.
  A read-only auditor whose instructions tell it to regenerate a checked-in
  file is not read-only in either sense.

And two duplication hazards worth closing while the file is open:
`repo_root()` is duplicated verbatim in two modules, and the second writes the
working directory into the generated scheduler jobs, so moving a file points
the scheduler at the wrong directory with no error.

Found by the `principal-review` board (spec 028), architect lens.

## Scope

**MIT, at the repository root**, with the copyright line naming the author,
referenced from the README and from the package metadata so the three cannot
drift.

**The four standing guardians get the spec 028 treatment.** Each either loses
`Bash` and becomes read-only in the enforceable sense, or keeps it and stops
being described as read-only, with an execution-limits section like the
readiness investigators have. The governance test is extended to cover them,
so the distinction is enforced for all fourteen agents rather than ten.

**`contract-guardian` audits rather than regenerates.** It compares the
checked-in artifact against what generation would produce, without writing.

**One `repo_root`.** A single definition, imported by both callers, so the
scheduler and the demo cannot disagree about where the repository is.

## Inputs, outputs, failure modes

- Inputs: the repository as published.
- Outputs: a LICENSE file, corrected agent frontmatter and prose, and one
  shared root resolver.
- Failure mode this must not introduce: a licence claim in the README that
  the LICENSE file does not match, which is the same defect one level up. A
  test reads both.
- The dependency question is real and is answered rather than assumed: every
  runtime dependency's licence must permit MIT redistribution, and the check
  is part of this change rather than a later discovery.
- Fixture provenance is checked in the same pass. A fixture recorded from a
  real service carries that service's terms; an authored one does not.

## Acceptance criteria

Proving symbols are named at implementation, in
services/api/tests/test_publishable.py and the existing governance test.

- [ ] a LICENSE file exists at the root and is MIT
- [ ] the README's licence statement and the package metadata name the same
      licence as the file, asserted by a test
- [ ] every runtime dependency's licence permits redistribution under MIT,
      with the check runnable rather than a one-time note
- [ ] no committed fixture was recorded from a third-party service, or each
      one that was is documented with its provenance
- [ ] all fourteen agents are covered by the read-only and execution-limits
      tests, not only the ten from spec 028
- [ ] `contract-guardian` has no instruction that writes a generated artifact
- [ ] `repo_root` has one definition, asserted by a test that fails if a
      second appears
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028, architect lens. The absent LICENSE,
the four guardians' frontmatter, the contract-guardian instruction, and the
duplicated resolver are verifiable in the tree.

The licence choice is MIT, decided by the author when this spec was
commissioned.

## Out of scope

A contributor guide, a code of conduct, or issue templates. Publishing the
package to any registry.
