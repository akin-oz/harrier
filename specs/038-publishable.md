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

Proven by services/api/tests/test_publishable.py:

| Criterion | Proof |
|---|---|
| a LICENSE exists and is MIT | `test_a_license_file_exists`, `test_the_license_is_mit`, `test_the_license_names_a_copyright_holder_and_year` |
| README and metadata agree with the file | `test_the_readme_and_the_package_metadata_name_the_same_license`, `test_the_readme_no_longer_says_a_license_is_coming` |
| the licence reaches anyone who clones | `test_the_license_file_is_tracked_by_git` |
| dependency licences permit redistribution | `test_every_runtime_dependency_permits_redistribution` |
| fixture provenance is documented and true | `test_every_fixture_is_listed_with_its_provenance`, `test_no_fixture_names_a_real_company` |
| the guardians cannot write | `test_a_guardian_cannot_write`, over `.ai/` and `.claude/` both |
| a guardian holding Bash states its limits | `test_a_guardian_holding_bash_states_its_execution_limits` |
| no agent claims read-only while holding Bash | `test_no_agent_claims_to_be_read_only_while_holding_bash` |
| contract-guardian does not regenerate | `test_the_contract_guardian_does_not_regenerate_what_it_audits` |
| one `repo_root` | `test_repo_root_has_exactly_one_definition`, `test_repo_root_resolves_to_a_directory_holding_the_repository` |

Two corrections to the board's finding, both recorded rather than quietly
absorbed. Only one of the four guardians actually claimed to be read-only in
its prose; the other three held `Bash` without making the claim. All four
were given execution limits anyway, because an auditing agent that can write
should say what it will not do.

And the fixture check was wrong on its first pass: it flagged
`jobs.ashbyhq.com` as evidence of a recording. The importers route on
hostname, so a real host is what makes a fixture exercise the real path and
is a protocol fact rather than provider content. What would reveal a
recording is the board slug after it, which names an actual company. The
check now reads the slug and ignores the API path segments.

Honest limitation on the dependency check: it reads each installed
distribution's declared metadata. A package that declares its licence
incorrectly is not caught, and no automated check would catch it.

- [x] a LICENSE file exists at the root and is MIT
- [x] the README's licence statement and the package metadata name the same
      licence as the file, asserted by a test
- [x] every runtime dependency's licence permits redistribution under MIT,
      with the check runnable rather than a one-time note
- [x] no committed fixture was recorded from a third-party service, or each
      one that was is documented with its provenance
- [x] all fourteen agents are covered by the read-only and execution-limits
      tests, not only the ten from spec 028
- [x] `contract-guardian` has no instruction that writes a generated artifact
- [x] `repo_root` has one definition, asserted by a test that fails if a
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
