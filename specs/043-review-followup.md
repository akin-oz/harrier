---
spec: 043
title: The review loop closes itself
status: in-progress
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
- Resolving a thread does not close it: a reply can arrive afterwards, and
  the review body is read every time because a finding can live there with
  no thread to make it visible.

**A check that can fail.** `CodeRabbit: pass` with the reason "Review rate
limited" is the defect that motivated this. The command reports a pull
request in that state as not yet reviewed, so "reviewed" and "rate limited"
stop looking identical.

**The reply is part of the loop.** Added after using this on the pull
requests it was written for, where it missed findings three separate ways.
A review is a conversation, and asking again is only half of taking part.

- **A reply inside a thread we resolved.** Resolving hides it from every
  query filtering on `isResolved`, and a reply is exactly where a reviewer
  says a fix does not do what it claimed. So the question is who spoke last,
  not what the thread says about itself.
- **A review whose findings never became threads.** A review can report
  "Actionable comments posted: 4" and then note that some are outside the
  diff and could not be posted inline. Those exist only in the review body,
  and no query over `reviewThreads` returns them however it filters. A Major
  finding on PR #37 arrived this way and this loop did not see it.
- **A review arriving after our own push.** Answering findings moves the
  head, which earns a fresh review whose findings a count taken beforehand
  cannot contain.

The command reports all three, refuses to ask for a new review while
anything is outstanding, and exits non-zero. What it must not do is decide
that a finding has been dealt with: reading and answering stay judgement,
and what has been read is recorded only when a person says so.

Related, and found while fixing the above: `last_reviewed_sha` was never
populated by `gather`, so "already reviewed at the current head" could not
fire and the loop re-requested every cycle. Its tests passed because they
built the state by hand rather than through `gather`.

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

Proven by services/api/tests/test_review_followup.py:

| Criterion | Proof |
|---|---|
| the wait parses in every shape the notice uses | `test_the_wait_is_parsed` (six cases), `test_a_notice_with_no_readable_wait_returns_none`, `test_a_zero_wait_reads_as_no_wait` |
| the newest notice wins | `test_the_newest_notice_is_the_one_used`, `test_a_notice_inside_a_longer_comment_is_found` |
| no notice means nothing is posted | `test_no_notice_means_none`, `test_a_notice_without_a_wait_is_reported_not_guessed` |
| an unchanged head is not re-requested | `test_a_reviewed_pull_request_at_the_same_head_is_left_alone`, `test_a_moved_head_is_asked_again` |
| the daily bound stops the loop | `test_the_daily_bound_stops_the_loop`, `test_the_bound_wins_over_everything_else` |
| rate limited is distinguishable from reviewed | `test_a_rate_limited_pull_request_reports_as_not_reviewed`, `test_a_reviewed_pull_request_reports_as_reviewed`, `test_a_pull_request_with_neither_is_still_not_reviewed` |
| `gh` failing is reported | `test_gh_failing_is_reported_not_swallowed`, `test_an_unreadable_thread_count_is_reported` |
| the rule compiles into both instruction files | `aie check` clean, and the rule text appears in `CLAUDE.md` and `AGENTS.md` |
| nothing about a pull request is committed | no test touches the network; the `gh` seam is injected, so no title or branch name reaches a fixture (ADR-008) |

The problem statement understated the scale, and the correction is worth
recording. It said two pull requests had reported a pass while rate limited.
Checking properly found **six of seven open pull requests with zero review
threads between them**, every check green. The monitoring that reported them
as clean asked whether any thread was unresolved, which is trivially true of
zero threads, so it was the same guard-reporting-success defect one level up
in the tooling rather than the code.

One dependency was deliberately not taken. The request counter uses a plain
read and write rather than the durable-state module spec 040 adds, because it
is a counter and not the tracker: losing it costs a few extra requests,
bounded by the daily limit. Spec 040 is unmerged, and duplicating it here to
protect a counter would have been the wrong trade.

- [x] the wait is parsed from minutes, hours, and the combined form, with one
      test per shape and one for a notice that carries no wait
- [x] the newest notice wins when a pull request carries several
- [x] a pull request with no notice reports that and posts nothing
- [x] a re-request is not sent when the head commit has not moved since the
      last completed review
- [x] the daily bound stops the loop, proven by a test that exceeds it
- [x] a rate-limited pull request is reported as not yet reviewed, so it is
      distinguishable from a reviewed one
- [x] `gh` failing is reported, not swallowed
- [x] the rule compiles into `CLAUDE.md` and `AGENTS.md`, and `aie check` is
      clean
- [x] no pull request title, branch name, or comment body is written to a
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
