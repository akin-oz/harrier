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

Two more, found by running the command against the pull requests it was
written for rather than against its own fixtures.

`last_reviewed_sha` was never populated by `gather`, so "already reviewed at
the current head" could not fire and the loop re-requested every cycle. Its
tests passed because they built the state by hand rather than through
`gather`.

And the mechanical half, the entire point of this spec, did nothing.
`gather` asked `gh` for `.[].body` and split the output by line, calling each
line a comment body. A comment body is multi-line, so every notice arrived in
pieces, and the last piece carrying the marker was the closing
`<!-- end of auto-generated comment: rate limited ... -->`, which holds no
wait. Every genuinely rate-limited pull request reported "a rate-limit notice
carried no readable wait" and was skipped. Six of them did, on the day this
was written, while the tests passed: they handed the parser one whole notice
directly, which is the shape production never produces. Comments are now
parsed as JSON, one body per element, and
`tests/test_review_followup.py::test_a_real_notice_survives_the_trip_through_gather`
drives a real multi-line notice through `gather` because that is the only
shape that can tell the two apart.

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
| the reworded notice is read (amendment below) | `test_the_current_notice_wording_is_parsed` (three cases), `test_the_current_notice_survives_gather_with_its_time` |
| the wait counts from when the notice was posted (amendment below) | `test_a_notice_waits_only_what_is_left_of_it`, `test_an_expired_notice_asks_again`, `test_an_expired_notice_on_a_reviewed_head_is_left_alone` |
| a closed pull request is never asked, and is still owed its answers (amendment below) | `test_a_closed_pull_request_is_never_asked`, `test_a_closed_pull_request_is_still_owed_its_answers`, `test_gather_reads_whether_the_pull_request_is_closed` |
| a clean review counts, and a review in progress is not asked again (amendment below) | `test_a_clean_review_counts_as_reviewed`, `test_a_notice_carrying_a_commit_range_is_not_a_review`, `test_a_review_in_progress_is_not_asked_again` |
| no notice means nothing is posted | `test_no_notice_means_none`, `test_a_notice_without_a_wait_is_reported_not_guessed` |
| an unchanged head is not re-requested | `test_a_reviewed_pull_request_at_the_same_head_is_left_alone`, `test_a_moved_head_is_asked_again` |
| a reply in a thread is not a review of the commit it names (amendment below) | `test_a_reply_in_a_thread_is_not_a_review`, `test_the_reviewed_sha_is_read_from_the_reviews` |
| the daily bound stops the loop | `test_the_daily_bound_stops_the_loop`, `test_the_bound_wins_over_everything_else` |
| rate limited is distinguishable from reviewed | `test_a_rate_limited_pull_request_reports_as_not_reviewed`, `test_a_reviewed_pull_request_reports_as_reviewed`, `test_a_pull_request_with_neither_is_still_not_reviewed` |
| `gh` failing is reported | `test_gh_failing_is_reported_not_swallowed`, `test_an_unreadable_payload_is_reported` |
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
- [x] the reworded notice ("Next included review available in 52 minutes.")
      is read, through `gather` as the API returns it
- [x] the wait counts from when the notice was posted, and a notice whose
      wait has passed decides nothing
- [x] a merged or closed pull request is never asked for a review, and a
      finding on one is still reported as owed an answer
- [x] a review that found nothing counts as a review at the commits it
      covered, and a review still being written is not asked for again
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
- [x] a reply in a thread is not a review of the commit it names, so a head
      the reviewer has only replied on is asked for its review (the
      amendment below on replies;
      `test_a_reply_in_a_thread_is_not_a_review`)
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

## Amendment (2026-10-06, the notice was reworded)

The service changed the wording of its notice. It now reads:

```
<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->
> [!WARNING]
> ## Review limit reached
> You've used all free OSS reviews for now.
> **Next included review available in 52 minutes.**
```

The pattern read only the wording quoted under Problem, so every
rate-limited pull request reported "a rate-limit notice carried no readable
wait" and was skipped: the failure mode this spec names for a changed format,
reported rather than absorbed, as intended. The phrase is now read in either
wording, and the wait is what follows it on its line.
`test_the_current_notice_wording_is_parsed`,
`test_the_current_notice_survives_gather_with_its_time`.

Reading the live notices also showed that the wait was counted from the
wrong moment. A notice states its wait from when it was posted, and Proof /
origin asks for "the time the notice actually states", but the command
waited the full stated time from whenever it ran. A notice three hours old
cost another full wait before the review it cut short was asked for. The
wait now counts from the update time of the comment holding the notice,
which the service edits in place, and a notice whose wait has passed
decides nothing: the pull request is then judged as if it had none.
`test_a_notice_waits_only_what_is_left_of_it`,
`test_an_expired_notice_asks_again`,
`test_an_expired_notice_on_a_reviewed_head_is_left_alone`.

Asking for reviews of merged pull requests found the third. The scope was
always "every open one", but the command never read a pull request's state,
and the service answers a request on a closed one with "Action not
completed. Pull request is closed.": a comment that buys nothing. The
command now reads the state and never asks on a closed pull request. It
still reports one whose findings are unanswered, before anything else,
because a finding on a merged pull request is owed an answer like any other.
`test_a_closed_pull_request_is_never_asked`,
`test_a_closed_pull_request_is_still_owed_its_answers`,
`test_gather_reads_whether_the_pull_request_is_closed`.

So a pull request merged before its review is not reviewed by this service
at all. Waiting for the review before merging is the only way to get one.

Two more came from the first review this amendment earned. A review that
finds nothing creates no review object, only a line in the service's
summary comment ("No actionable comments were generated") beside the
commits it covered. The command counted reviews by their objects, so a
cleanly reviewed pull request read as unreviewed and would have been asked
again, spending the hour's one review on commits already covered. The
summary line now counts, at the commits it names; a rate-limit notice names
the same commit range, so the range alone does not. And a review still
being written carries its own marker, which the command did not read, so it
would have asked again mid-review. It now reports the review in progress
and waits for it. `test_a_clean_review_counts_as_reviewed`,
`test_a_notice_carrying_a_commit_range_is_not_a_review`,
`test_a_review_in_progress_is_not_asked_again`.

Limitation, recorded rather than fixed here: the limit belongs to the
repository, but notices are read per pull request. Asked about several pull
requests at once, the command requests each that needs it, and every request
after the one the limit allows earns a notice of its own, which the next run
then waits out. Treating the limit as the repository's would change what the
command does across pull requests, so it needs its own amendment.

## Amendment (2026-10-06): a reply in a thread is not a review

Found by running the command on PR #147.
`harrier review-followup 147 --dry-run` printed "PR #147: already reviewed at
the current head" and "PR #147: reviewed, 2 threads, nothing outstanding",
and exited 0. The service had reviewed only the previous head, a855abe. The
current head, 937deb9, was pushed to answer that review, and the service's
status on it read "Review rate limited".

The cause is in `gather`. It took the reviewed commit from the newest review
node the reviewer wrote. A reply posted in a review thread is a review node
of its own, and on PR #147 each one has state COMMENTED, an empty body, and
the head at that moment as its commit. The service replied in both threads
after the push, so its two newest nodes were replies at 937deb9, and the
decision found the head reviewed. The review that carried the findings,
5433578445, has a body and names a855abe. The service's replies, 5433666186
and 5433667882, have empty bodies and name 937deb9. Our own two replies are
nodes as well, empty and at 937deb9, and never counted because the reviewer
did not write them.

So one reply from the reviewer after a push was enough to skip the review the
push earned. That is the case the head-commit check exists for: answering
findings moves the head, and the move earns a fresh review.

### Behavior after the change

- A review node from the reviewer sets the reviewed commit only when its body
  is not empty. A node with an empty body is a reply in a thread and sets
  nothing. Among the nodes with a body, the newest names the commit, as
  before.
- In the shape PR #147 had, with every finding answered, the reviewed commit
  is a855abe and the head is 937deb9, so the decision is "the head has moved
  since the last review": a request, reported and not posted under
  `--dry-run`. A rate-limit notice whose wait has not passed still comes
  first and makes it a wait.
- A clean review is still read from the summary comment, as the amendment
  above describes. When the summary reports one, the commit that ends the
  range it names is the reviewed one, whatever the review nodes say, so
  replies change nothing there.
- Nothing else reads review nodes differently. `reviews_seen` still counts
  replies: a reply always sits in a thread, and a thread already makes the
  pull request read as reviewed, so counting it changes nothing. Finding a
  reply that waits on us, by who spoke last in its thread, is unchanged, and
  so is reading review bodies for findings.

The body is the criterion because it is the difference the evidence shows:
on PR #147 the review with findings has one, and all four replies have none.
`gather` already reads the body, so the query does not change. The other
criterion, whether every comment in a node replies to an earlier one, would
mean reading every review's comments as well.

### What changes

- `services/api/src/harrier/reviewfollowup.py`: `gather` applies the rule
  where it sets the reviewed commit. Nothing else in the module changes.
- `services/api/tests/test_review_followup.py`:
  `test_a_reply_in_a_thread_is_not_a_review` drives the PR #147 shape through
  `gather`. `test_the_reviewed_sha_is_read_from_the_reviews` built its review
  with an empty body, which this amendment reads as a reply, so its review
  gains a body. What it proves is unchanged.

No new file, so `config/data-classification.json` does not change. The
command's output lines and exit codes do not change.

### How to know it worked

`test_a_reply_in_a_thread_is_not_a_review` builds what PR #147 held and runs
it through `gather`, because this spec has twice recorded a defect that tests
bypassing `gather` could not see. It holds a review with findings at commit
A, recorded as read; two threads, each ending in the reviewer's
acknowledgement, also recorded as read; then four review nodes with empty
bodies at commit B, two ours and two the reviewer's, with B as the head. It
asserts that the reviewed commit is A and that the decision is a request
because the head has moved. Before this change it fails: the reviewed commit
reads as B and the decision as "already reviewed at the current head". That
failure was reproduced through `gather` before this amendment was written,
and again by this test before the fix.

### Failure modes this must not introduce

- A review with a body at the head stops reading as reviewed there.
  `test_the_reviewed_sha_is_read_from_the_reviews`, with its review given a
  body, holds this.
- A clean review read from the summary comment stops counting.
  `test_a_clean_review_counts_as_reviewed` holds this.
- A reply hides a finding. Replies are still read for who spoke last:
  `test_a_reply_in_a_resolved_thread_still_needs_an_answer`,
  `test_a_further_reply_comes_back_after_being_answered`.

### Out of scope

- **The report line and the exit code.** A pull request reviewed at an
  earlier head and not at its current one still reads "reviewed, N threads,
  nothing outstanding" and exits 0, while the decision line says the head has
  moved, or that it is rate limited. That was already so for any moved head.
  Exiting non-zero there would change what exit 2 means for every pull
  request whose head has moved since its last review, so it needs its own
  amendment.
- **The review window.** `gather` reads the newest twenty review nodes, and
  every reply, ours or the reviewer's, is one. Past twenty, the query has
  another page, and the command reports "a bounded query had another page"
  and exits 3 on every run. That fails closed, so nothing is missed, but
  replies bring it sooner. It is left to its own change.

### Limitations

- A reply posted with a body of its own would still count as a review. None
  has been seen: all four replies on PR #147 have empty bodies.
