<!-- generated-by: development/spec-driven@1 — edit the blueprint, not this file -->
# Spec NNN: <what this changes>

- Status: Draft | Accepted | Shipped
- Depends on: <specs that must land first, or —>

## Problem

What breaks today, and for whom. Describe the situation, not the fix. If this
section is hard to write, the request may be a solution in search of a problem.

## Behavior

What the software does after this change. Inputs, outputs, and the observable
result. Write it so someone could disagree with it.

## Failure modes

The cases that are easy to skip and expensive to miss: empty input, the second
run, concurrent callers, a pre-existing file, a partial failure halfway through.
State what happens in each.

## Acceptance criteria

Checkable against a diff, not aspirations.

- <specific input> produces <specific output>
- <specific failure> produces <specific error and exit code>

## Out of scope

Explicitly what this change will not do. This list is what stops the change from
growing, so it is as load-bearing as the requirements above.

## Migration

What existing users must do, if anything. Write "none" when nothing changes for
them — say it rather than leaving the section empty.
