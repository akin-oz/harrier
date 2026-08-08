---
description: Approval gating and the Spec trailer, the mechanical side of spec-driven work
---

You operate under spec-gated execution.

- Specs live in `specs/NNN-name.md`. A spec gates implementation only when its
  frontmatter says `approved: yes`. Only Akin flips approval. You never set
  `approved: yes`, and you never implement against a spec that is not approved.
- Every commit carries a `Spec: NNN` trailer referencing the spec that covers the work.
  A local guard blocks commits without one (`.claude/hooks/guard-commit.sh`); CI
  resolves every trailer to an approved spec and blocks the merge otherwise.
- If no approved spec covers the work you are asked to do, stop and write or amend a
  spec first (use `/spec`), then wait for approval.
- A gap found mid-implementation is a spec amendment in the same change, stated
  explicitly, never a silent scope expansion.
