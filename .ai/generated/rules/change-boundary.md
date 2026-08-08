---
description: Each change stays inside the boundary its spec describes
---
<!-- generated-by: development/spec-driven@1 — edit the blueprint, not this file -->

A change does what its spec says and nothing else.

Improvements you notice along the way — a badly named variable, a missing test
elsewhere, a dependency worth removing — are real, and they belong in their own
change. Mixing them in makes the diff impossible to review against the spec,
because the reviewer can no longer tell which lines are the feature and which
are opinion.

When you find something out of scope, note it explicitly in the pull request
rather than fixing it silently or staying quiet about it.

Two signals that a boundary has been crossed: the diff touches files the spec
never mentions, or explaining the change requires the word "also". Both mean the
change should be split.

If the spec turns out to be too large to implement in one reviewable change,
split the spec, not just the branch.
