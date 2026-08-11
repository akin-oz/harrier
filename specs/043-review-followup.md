---
spec: 043
title: The review loop closes itself
status: accepted
approved: yes
milestone: M7
depends: [028]
---

# Spec 043: The review loop closes itself

## Problem

CodeRabbit rate-limits this repository. When it does, it posts:

```
<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->
> **Next review available in:** **38 minutes**
> You've used all free OSS reviews for now.
```

and the pull request keeps its green checks. Nothing has reviewed the change,
and nothing says so in a way a check can fail on. Twice in the current
session a PR reported `CodeRabbit: pass` with the reason "Review rate
limited", which is a guard reporting success while doing nothing: the class
of defect this repository keeps finding in itself.

The waiting is mechanical and nobody should be doing it by hand. Reading what
comes back is not.

## Two halves, and they are not the same kind of thing

**Mechanical.** Read the newest rate-limit notice on a pull request, work out
the wait, wait it out, and post `@coderabbitai review`. Deterministic, and
safe to run unattended: the only side effect is a comment on the author's own
pull request.

**Judgement.** Verifying a finding before acting on it, fixing it, or
declining it with evidence. That is not scriptable and this spec does not
pretend otherwise. In the session that prompted this, roughly forty findings
arrived and two were declined because their premise was false, one of them
confidently wrong about a file it named. A loop that applied every finding
would have made the code worse.

So the mechanical half becomes a command, and the judgement half becomes a
rule in `.ai/` that every session compiles into its own instructions.

## Scope

**`harrier review-followup`**, over one pull request or every open one:

- Find the newest rate-limit notice. Newest matters: an older notice has
  usually expired and acting on it re-requests immediately for no reason.
- Parse the wait. Handles minutes, hours, and the combined form, because the
  notice uses whichever fits.
- Wait, then post `@coderabbitai review`.
- Report what it did, so a run that found no notice says that rather than
  exiting silently.

**A bound on the loop.** At most a configured number of re-requests per pull
request per day. Without one, a repository that stays rate-limited produces a
comment every hour forever, which is noise on the author's own pull request
and load on somebody else's service.

**Only when there is something to review.** A re-request on a pull request
whose head has not moved since the last completed review asks for the same
review again. The command re-requests when the last review was cut short by
the rate limit, or when the head commit has changed since the last review,
and otherwise reports that there is nothing new.

**A governance rule for the half that needs judgement.**
`.ai/rules/review-response.md`, compiled into `CLAUDE.md` and `AGENTS.md` by
`aie`, so it applies in every session without anyone remembering to ask:

- Verify a finding against the code before acting on it. Run it where running
  it is possible.
- A finding whose premise is false is declined on the thread, with the
  evidence, and the underlying concern assessed separately in case it points
  at something real that the finding described wrongly.
- A fix gets a test that fails without it, and the test exercises the
  decision rather than the helper.
- Findings out of the current spec's scope are recorded in the spec that owns
  them rather than absorbed silently or dropped.
- Every thread is answered and resolved, including the declined ones.

**A check that can fail.** `CodeRabbit: pass` with the reason "Review rate
limited" is the defect that motivated this. The command reports a pull
request in that state as not yet reviewed, so "reviewed" and "rate limited"
stop looking identical.

## Inputs, outputs, failure modes

- Inputs: the pull request's comments, through `gh`.
- Outputs: at most one `@coderabbitai review` comment per wait, and a report.
- Failure modes: `gh` unauthenticated, the notice format changing, the API
  unreachable. Each is reported rather than absorbed, and a parse that finds
  no wait reports that rather than defaulting to a guess.
- Failure mode this must not introduce: a comment loop. The daily bound and
  the head-commit check are both load-bearing.
- Failure mode this must not introduce: acting on findings unattended. The
  command posts a request and reports; it never edits code, and it is not
  wired to anything that does.

## The honest limitation on "all the time"

A command runs while something runs it. Continuous operation across sessions
means a scheduled job, which this repository already generates (spec 020,
ADR-006), and that is the right home for the mechanical half.

The judgement half cannot be scheduled. It needs a session, and the rule
above is what makes that session behave the same way every time rather than
depending on who is asking. This spec makes the waiting automatic and the
reading consistent, which is as far as honesty allows.

## Acceptance criteria

Proving symbols are named at implementation, in
services/api/tests/test_review_followup.py.

- [ ] the wait is parsed from minutes, hours, and the combined form, with one
      test per shape and one for a notice that carries no wait
- [ ] the newest notice wins when a pull request carries several
- [ ] a pull request with no notice reports that and posts nothing
- [ ] a re-request is not sent when the head commit has not moved since the
      last completed review
- [ ] the daily bound stops the loop, proven by a test that exceeds it
- [ ] a rate-limited pull request is reported as not yet reviewed, so it is
      distinguishable from a reviewed one
- [ ] `gh` failing is reported, not swallowed
- [ ] the rule compiles into `CLAUDE.md` and `AGENTS.md`, and `aie check` is
      clean
- [ ] no pull request title, branch name, or comment body is written to a
      committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The notice format above was read from a live pull request, not guessed, and a
parser for it was verified against that text plus the hour and combined forms
before this spec was written.

The request came from the operator: the waiting should be automatic, the
re-request should use the time the notice actually states, and the whole
thing should live in the governance rather than in one session's habits.

## Out of scope

Acting on findings automatically. Any integration with a review service other
than the one in use. Changing what the four standing guardians or the two
boards do.
