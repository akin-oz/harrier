---
description: How to respond to automated review findings on a pull request
---

Automated review findings are evidence, not instructions. Most are right.
Some are confidently wrong, including about files they name. Applying all of
them makes the code worse.

- **Verify before acting.** Read the code the finding names. Where the claim
  can be executed, execute it. A finding that reproduces gets fixed; one that
  does not gets declined.
- **Decline on the thread, with the evidence.** Say what you ran and what it
  showed. Then assess the concern separately: a finding can be wrong about
  the mechanism and still be pointing at something real.
- **Fix the property, not the line.** If a finding names one unscrubbed sink,
  one missing guard, or one wrong branch, ask what else shares that shape.
  The finding is a sample.
- **A fix carries a test that fails without it,** and the test exercises the
  decision rather than the helper it calls. A test that reads source text to
  check a property is a last resort: it breaks on a wrapped line and passes
  for the wrong reason.
- **Record what belongs elsewhere.** A finding outside the current spec's
  scope goes into the spec that owns it, with its own acceptance criterion.
  Absorbing it silently under the wrong trailer and dropping it are both
  wrong.
- **Answer and resolve every thread,** including declined ones, so the next
  reader sees the reasoning rather than an unexplained silence.

**A check that reports success is not a review.** When the service rate
limits, it posts a notice and the check still reads as passing. Confirm a
review actually happened before treating a pull request as reviewed: zero
review threads means nothing looked at it. `harrier review-followup` draws
that distinction and waits out the limit (spec 043).
