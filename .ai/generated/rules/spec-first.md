---
description: Behavior changes start from a written spec
---
<!-- generated-by: development/spec-driven@1 — edit the blueprint, not this file -->

No change to observable behavior lands without a spec describing it first.

A spec is not a design document or a plan. It states what the software will do
after the change: the inputs, the outputs, the failure modes, and how someone
would know it worked. It lives in `specs/` and is reviewed like code.

The order is: write or amend the spec, get agreement on it, then implement. When
implementation reveals the spec was wrong — which is normal and useful — amend
the spec in the same change rather than letting the code and the spec disagree.

Bug fixes need a spec delta only when the correct behavior was never written
down. If the spec already says what should happen, the fix is just the fix, and
the test is the proof.

Refactoring changes no behavior by definition, so it needs no spec. If a
"refactor" requires a spec change, it is not a refactor, and the pull request
should say so.
