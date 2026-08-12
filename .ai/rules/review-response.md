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

**Resolving a thread is not the end of it.** A review is a conversation, and
three kinds of reply are invisible to the check most people run. Each of
these has been missed on this repository, which is why they are listed rather
than left to judgement:

- **A reply inside a thread you resolved.** Resolving hides it from every
  query that filters on `isResolved`, and a reply is where the reviewer says
  your fix does not do what you claimed.
- **A new review after you push.** Answering findings changes the head, which
  earns a fresh review. Its findings are new threads that a count taken
  before the push cannot contain.
- **Findings that never became threads.** A review can say "Some comments are
  outside the diff and can't be posted inline". Those findings exist only in
  the review body, so no query over `reviewThreads` returns them however it
  filters. A Major one was missed this way.

So the question is never "are there unresolved threads". It is **"has the
reviewer said anything I have not answered"**: threads whose last comment is
theirs whatever their resolved state, and review bodies with actionable
comments. `harrier review-followup` reports both and exits 3 while anything
is outstanding, and it will not ask for a new review while findings sit
unanswered, because a rate-limited request spent mid-conversation buys
nothing.

**Read the review body, not only the threads.** Every time. It is the one
place a finding can hide with no thread to make it visible.
