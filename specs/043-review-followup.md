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
| a head reviewed only before it moved exits 2 and says so (amendment below) | `test_a_head_reviewed_before_it_moved_is_reported_as_not_reviewed_there` (seven cases), `tests/test_cli_decisions.py::test_a_head_reviewed_before_it_moved_exits_two`, `tests/test_cli_decisions.py::test_an_unanswered_finding_after_a_push_still_exits_three` |
| replies do not crowd a review out of the window (amendment below) | `test_replies_past_the_window_do_not_hide_the_review`, `test_an_unread_review_past_the_window_is_still_found`, `test_an_earlier_page_with_no_cursor_fails_closed`, `test_reading_back_stops_at_the_page_bound`, `test_gh_failing_on_an_earlier_page_is_reported` |
| a person records replies and reviews as read by naming their ids, and nothing else is recorded or posted (amendment below) | `tests/test_cli_decisions.py::test_marking_read_clears_what_the_run_printed`, `tests/test_cli_decisions.py::test_an_id_not_outstanding_records_nothing`, `tests/test_cli_decisions.py::test_marking_read_posts_nothing` |
| a truncated pull request says so in its report line (amendment below) | `test_a_truncated_pull_request_says_so_in_its_report_line` (three cases) |
| a dry run never waits and never asks, with `--wait` or without (amendment below) | `tests/test_cli_decisions.py::test_a_dry_run_never_waits_and_never_asks`, `tests/test_cli_decisions.py::test_waiting_without_a_dry_run_still_asks` |
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
- [x] a pull request reviewed only before its head moved reads "NOT
      REVIEWED AT THE HEAD" and exits 2, and an unanswered finding still
      exits 3 (the amendment below on the exit code;
      `tests/test_cli_decisions.py::test_a_head_reviewed_before_it_moved_exits_two`,
      `tests/test_cli_decisions.py::test_an_unanswered_finding_after_a_push_still_exits_three`,
      `test_a_head_reviewed_before_it_moved_is_reported_as_not_reviewed_there`)
- [x] replies past the newest twenty review nodes do not leave a pull
      request outstanding on every run, and a review behind them is still
      read (the amendment below on the review window;
      `test_replies_past_the_window_do_not_hide_the_review`)
- [x] a run prints the ids a person would record, and `--mark-read` records
      only the ids it is given, each checked against what the pull requests
      named hold outstanding, and posts nothing (the amendment below on
      recording replies as read;
      `tests/test_cli_decisions.py::test_marking_read_clears_what_the_run_printed`)
- [x] a truncated pull request's report line says a bounded query had
      another page, in the decision line's words, so no report line for an
      outstanding pull request ends at its colon (the amendment below on
      truncation; `test_a_truncated_pull_request_says_so_in_its_report_line`)
- [x] under `--dry-run` the command never sleeps, never posts and counts no
      request, `--wait` beside it or not, and `--wait` alone still waits and
      asks (the amendment below on dry runs;
      `tests/test_cli_decisions.py::test_a_dry_run_never_waits_and_never_asks`)
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
  replies bring it sooner. It is left to its own change: the amendment
  below on the review window.

### Limitations

- A reply posted with a body of its own would still count as a review. None
  has been seen: all four replies on PR #147 have empty bodies.

## Amendment (2026-10-06): a head reviewed only before it moved exits 2

The amendment above on replies recorded this as out of scope, and Akin asked
for it next.

That amendment made the decision right for a pull request reviewed at an
earlier head. The report line and the exit code did not follow. The command,
run at 207f850 on the shape PR #147 had, through the real `gather` with `gh`
stubbed, printed:

```
PR #147: the head has moved since the last review

PR #147: reviewed, 2 threads, nothing outstanding
```

and exited 0. The decision asks for a review, and the exit code says the
pull request is settled. A session that reads the exit code takes the push
that answered the findings as reviewed when nothing has reviewed it: the
review arriving after our own push, missed one level above the decision.
`test_a_settled_pull_request_exits_zero` pins that state as settled. Its pull
request has threads and a review, nothing outstanding, and no review naming
its head, and the decision answers that state with "the head has moved since
the last review".

### Behavior after the change

- **Reviewed at the head** means the reviewed commit, as `gather` reads it,
  is the head. That is the test behind "already reviewed at the current
  head", so the decision, the report line and the exit code read one fact.
- **Exit 2 means the head has not been reviewed.** Until now it meant that
  nothing had reviewed the pull request at all. It now also covers a pull
  request reviewed only before its head moved, when nothing is outstanding.
  A pull request is settled, and exits 0, only when it is reviewed at its
  head and nothing is outstanding.
- **An unanswered finding still exits 3.** A pull request with a moved head
  and something outstanding exits 3: the decision answers before it asks,
  and the review-response rule says the command "exits 3 while anything is
  outstanding". So the new exit 2 applies only when nothing is outstanding.
- **The report line says so.** A pull request reviewed only before its head
  moved, with nothing outstanding, reads
  `PR #N: NOT REVIEWED AT THE HEAD, last reviewed at abc1234`, with the first
  seven characters of the reviewed commit. When no review names a commit, as
  when the only threads were started by someone else and the reviewer only
  replied, `no review names a commit` takes the place of the commit. When a
  reason from the never-reviewed lines applies, the first that applies
  follows a semicolon, in the order those lines use:
  `closed, so the service will not now`, `a review is in progress`, or
  `rate limited`, each read as those lines read it.
- Everything else keeps its line and its code: a pull request reviewed at its
  head reads "reviewed, N threads, nothing outstanding" and exits 0, and
  never-reviewed and outstanding pull requests read and exit as before. The
  decision does not change, and neither does anything the command posts.

Considered and not chosen: a new exit code, 4, for a head reviewed only
before it moved, which would keep exit 2 meaning "never reviewed". Nothing
in this repository switches on the codes: the command's own tests check
them, and the review-response rule names only exit 3. A fourth code would
add a distinction nothing reads. With this change, exit 2 says a review is
owed, and exit 3 says an answer is owed.

### What changes

- `services/api/src/harrier/reviewfollowup.py`: `PullRequestState` says
  whether it is reviewed at its head, `decide` reads that in place of its own
  copy of the comparison, and `report` gives the new line.
- `services/api/src/harrier_cli/main.py`: `_cmd_review_followup` exits 2 for
  a pull request reviewed only before its head moved, when nothing is
  outstanding.
- `services/api/tests/test_cli_decisions.py`:
  `test_a_head_reviewed_before_it_moved_exits_two` drives the PR #147 shape
  through the real `gather`, with `gh` stubbed, and asserts the decision
  line, the report line and exit 2. It also gains
  `test_an_unanswered_finding_after_a_push_still_exits_three`, which gives a
  pull request a moved head and an unanswered thread, and asserts exit 3.
  `test_a_settled_pull_request_exits_zero` gains a reviewed commit equal to
  its head, because settled now means reviewed there. What it proves is
  unchanged.
- `services/api/tests/test_review_followup.py`:
  `test_a_head_reviewed_before_it_moved_is_reported_as_not_reviewed_there`
  checks the line with each reason, the order of the reasons, and with no
  commit named.
  `test_a_reviewed_pull_request_reports_as_reviewed` gains a reviewed commit
  equal to its head, for the same reason.

No new file, so `config/data-classification.json` does not change.

### How to know it worked

`test_a_head_reviewed_before_it_moved_exits_two` runs the command as above.
Before this change it failed: the report line read "reviewed, 2 threads,
nothing outstanding" and the command exited 0. After it, the line reads
"NOT REVIEWED AT THE HEAD, last reviewed at" and the first seven characters
of the commit the review named, and the command exits 2.

### Failure modes this must not introduce

- A pull request reviewed at its head stops exiting 0.
  `test_a_settled_pull_request_exits_zero`, with its reviewed commit set to
  its head, holds this.
- An unanswered finding stops exiting 3 because its head also moved.
  `test_an_unanswered_finding_after_a_push_still_exits_three` holds this.
- The decision changes. No test of `decide` changes.

### Out of scope

- **The order across several pull requests.** Codes combine as before: an
  error first, then 2, then 3. So one pull request waiting on a review hides
  another waiting on an answer from the exit code, though the report lines
  show both
  (`tests/test_cli_decisions.py::test_one_pull_request_awaiting_review_hides_another_awaiting_an_answer`).
  Changing the order changes what a run over several pull requests exits,
  so it is its own change.

### Limitations

- The head counts as reviewed only when a review with a body names it, or
  the summary comment reports a clean review ending at it. If the service
  ever passes over a head without either, for example when nothing it
  reviews has changed, the pull request exits 2 until a later review covers
  its head. That has not been observed here.
- A closed pull request reviewed only before its last push exits 2 from now
  on, as a closed one never reviewed at all already does. PR #147 is one:
  merged at 937deb9, reviewed at a855abe. The service will not review it
  now, so that exit cannot be cleared. It says the merged head was not
  reviewed, rather than calling it settled.
- `rate limited` is read as the never-reviewed line reads it: from the newest
  notice, whatever its age. A notice whose wait has passed still shows, as it
  already does on that line.

## Amendment (2026-10-06): the review window

Recorded as out of scope by the amendment on replies, and fixed here. `gather`
read `reviews(last:20)`. A reply in a review thread is a review node, ours or
the reviewer's, so a pull request with enough conversation fills those twenty
with replies. The query then reports an earlier page, `truncated` is set, and
`outstanding` is true on every run: "a bounded query had another page" and
exit 3, with every finding answered. The review the replies answer is on that
earlier page, so its body and the commit it names are not read either.

This was inferred from the query and reproduced through `gather` by the
tests below, not observed on a live pull request.

### Behavior after the change

- When the newest page of reviews reports an earlier one, `gather` asks for
  it by its `startCursor`, a hundred nodes at a time, and keeps going until
  no earlier page is left. The nodes are read oldest first, as before, so the
  newest review with a body still names the reviewed commit.
- A pull request whose review nodes all fit in what was read is not
  truncated by its reviews. Thread truncation (`reviewThreads(first:100)`)
  is unchanged and still fails closed.
- It still fails closed when reading back cannot finish: a page that reports
  an earlier one with no cursor, or more than ten earlier pages
  (`REVIEW_PAGE_LIMIT`). Those set `truncated`, and the report line and exit
  code are what spec 045 already says.
- `gh` failing on an earlier page, or returning something that is not JSON,
  is reported as it is for the first query.

Paging was chosen over filtering because the API cannot filter replies out:
a reply and a review with findings both have state COMMENTED, and the
difference the amendment on replies relies on, the body, is not a filter the
reviews connection takes.

### What changes

- `services/api/src/harrier/reviewfollowup.py`: the reviews fields move to
  `REVIEW_FIELDS` and gain `startCursor`; `_earlier_reviews` reads the earlier
  pages; `gather` prepends them and takes review truncation from it.
- `services/api/tests/test_review_followup.py`: the five tests below, and
  two helpers that build paged answers.

No new file, so `config/data-classification.json` does not change. The
command's output lines and exit codes do not change.

### How to know it worked

- `test_replies_past_the_window_do_not_hide_the_review`: a review with
  findings at commit A, then twenty five replies at B, every finding read.
  The newest page holds twenty replies and a cursor; the earlier page holds
  the review and five replies. It asserts not truncated, not outstanding,
  and reviewed at A. Before the change it failed: truncated and outstanding.
- `test_an_unread_review_past_the_window_is_still_found`: an unanswered
  review behind twenty replies is reported as unread. Before the change it
  was not read at all.
- `test_an_earlier_page_with_no_cursor_fails_closed`: an earlier page with no
  way to ask for it still reads as truncated. This held before the change
  and is kept so paging cannot turn it into a pass.

- `test_reading_back_stops_at_the_page_bound`: every page reports another;
  reading stops at the bound and reads as truncated.
- `test_gh_failing_on_an_earlier_page_is_reported`: a `gh` failure on an
  earlier page raises the same error as one on the first query.

### Failure modes this must not introduce

- A loop that never ends. The page bound stops it, and stopping fails
  closed (`test_reading_back_stops_at_the_page_bound`).
- A missed finding. Every node read is read exactly as before; paging only
  reads more of them.

### Limitations

- Each earlier page is one more `gh` call. A pull request with a hundred
  nodes beyond the newest twenty costs one; past the bound it costs ten and
  still reports truncated.

## Amendment (2026-10-07): a person records replies and reviews as read

Found on 2026-10-07 by running `harrier review-followup 147` after the
amendment above on the exit code merged. The session it ran in refuses
GitHub GraphQL at its network proxy, and `gather` needs GraphQL, so the
command exited 1 there. It was then replayed: the real command at 29055af,
with a stand-in `gh` that served what GitHub's REST API returned for PR #147
and refused anything else. It printed
`PR #147: NEEDS A REPLY: 2 thread(s) awaiting a reply; 1 unread review(s)`
and exited 3.

Nothing on PR #147 waits on an answer. Each thread ends in the reviewer's
thanks for the fix, comments 4199660098 and 4199658649, both saying "Review
thread resolved." The unread review is 5433578445, "Actionable comments
posted: 2", and its two findings are the two threads. With those three ids
recorded as read, the same replay printed
`PR #147: NOT REVIEWED AT THE HEAD, last reviewed at a855abe; closed, so the service will not now`
and exited 2, as the amendment above says it should.

The command is right to show them. A reply that ends a thread is where a
reviewer says a fix does not do what it claimed, and this spec says what has
been read "is recorded only when a person says so". What is missing is a way
to say so. The record is `review-followup-handled.json` in the data
directory, and only `record_handled` in
`services/api/src/harrier/reviewfollowup.py` writes it. Nothing outside the
tests calls it: no flag, no subcommand, no recipe. And the command does not
print the id a thread needs. It prints the thread's own id, while the record
is keyed on the thread's last comment, so that a newer reply comes back
(`test_a_further_reply_comes_back_after_being_answered`).

So a person can settle such a pull request in two ways. One is to reply again
in each thread, so the last word is theirs
(`test_a_thread_we_answered_last_is_not_outstanding`), which means answering
a thank-you to quiet a tool. The other is a Python call, with ids looked up
somewhere else. On an open pull request the cost is more than an exit code. The decision answers before it asks
(`test_answering_comes_before_asking_for_more`), so while one acknowledgement
sits unrecorded the command never asks for the review the answering push
earned, and every run exits 3.

### Behavior after the change

- **A run prints the ids to record.** Under a pull request that needs a
  reply, a thread awaiting one reads
  `thread <thread id> (resolved=<True or False>), last comment <comment id>`,
  and an unread review reads as now, by its id. When those lines name at
  least one id, one more indented line follows with all of them:
  `once read, record them with: harrier review-followup 147 --mark-read <id> <id> <id>`,
  with `--owner` and `--repo` added when the run was given values other than
  the defaults. Truncation has no id, so a pull request outstanding only
  because a bounded query had another page gets no such line.
- **`--mark-read ID [ID ...]` records what a person has read.** The command
  reads every pull request named, as a run does, then checks each id. An id
  is accepted when one of those pull requests holds it outstanding, which is
  when the run prints it in the lines above, or when the record already holds
  it. Each accepted id gets a line: `recorded as read: <id>`, or
  `already recorded: <id>`. The run then reports as `--dry-run` does, over the
  state the record now gives: its decision lines, its report lines and its
  exit code. It posts nothing and waits for nothing, so `--wait` does nothing
  beside it.
- **Only what it is given.** `--mark-read` records no id it was not given, and
  no run records anything without it. No form records everything
  outstanding.
- **All or nothing.** If any id is neither outstanding on the pull requests
  named nor already recorded, none is recorded. The command prints
  `error: nothing was recorded; not outstanding on the pull requests named:`
  and every such id to stderr, and exits 1 before any decision. The same
  holds when a pull request named cannot be read, since its ids cannot be
  checked: its error prints as now, then `error: nothing was recorded`, and
  the command exits 1.
- **`--dry-run` beside `--mark-read`** checks the ids the same way, prints
  `would record as read: <id>` for each, and writes nothing. The rest of its
  output is a plain dry run's, over the record as it stands.
- **Exit codes keep their meanings.** 1 is an error, now including a refused
  id. Otherwise 0, 2 or 3 as before, read after the record.
- **The record keeps its shape and its place.** It is the same file,
  `review-followup-handled.json` in the data directory: `data/` by default,
  `HARRIER_DATA_DIR` when set. `config/data-classification.json` classes it
  never-in-git under `data/**`. It holds ids only, sorted, and is written as
  `record_handled` writes it now: to a scratch file, then renamed over the
  record.

Considered and not chosen:

- **A form with no ids, recording everything outstanding.** A reply that
  arrives between reading and recording would be recorded unread: the reply
  inside a resolved thread that the review-response rule lists as missed
  before. An id pins what was read. A newer reply has a new id and comes
  back.
- **Recording, then asking for a review in the same run.** One command would
  do both, but saying what was read would spend the hour's one review as a
  side effect, even when the person meant to push again first. A plain run
  after the record asks, as it always has.
- **A subcommand of its own.** An id can only be checked against the pull
  request it came from, so a subcommand would take the same numbers and the
  same `--owner` and `--repo`. A flag on the command that prints the ids keeps
  them together.
- **Recording without checking GitHub.** A mistyped id would be recorded,
  match nothing, and report success: a guard reporting success while doing
  nothing. The check costs only the reads a run already makes.

### Failure modes

- **An id that is not outstanding.** Mistyped, from a pull request not named,
  or a reply that a newer one has since replaced: nothing is recorded, the
  error names it, and the command exits 1. In the last case the newer reply
  stays outstanding, and the next run prints its id.
- **A pull request named that cannot be read.** Nothing is recorded, and the
  command exits 1 with the error it already prints for that pull request.
- **The second run with the same ids.** Each reads `already recorded`, the
  record does not change, and the run reports as the first did.
- **A truncated pull request.** It stays outstanding after every printed id
  is recorded, and exits 3, because what was not read cannot have been read.
- **A record that exists but cannot be read.** `--mark-read` records nothing,
  prints `error: nothing was recorded; the record at <path> cannot be read`,
  and exits 1, rather than writing over it and losing every id it held. A
  plain run still reads it as empty, as `load_handled` does now, which shows
  too much rather than too little.
- **A record that cannot be written**, for example when the data directory's
  path is a file: `error: nothing was recorded; could not write <path>`, exit
  1, and the record as it was. The rename means a half-written record never
  replaces it.

### Acceptance criteria

All in `services/api/tests/test_cli_decisions.py`, through the real `gather`
with `gh` stubbed, as
`tests/test_cli_decisions.py::test_a_head_reviewed_before_it_moved_exits_two`
runs, and in the empty data directory `services/api/tests/conftest.py` gives
every test.

- PR #147's shape, with nothing recorded: a plain run prints each thread's
  last comment id, the review's id, and the `once read` line holding all
  three. It writes no record and exits 3. Then `--mark-read` with those three
  prints `recorded as read:` for each, then
  `PR #147: NOT REVIEWED AT THE HEAD, last reviewed at` and the reviewed
  commit, and exits 2. Two cases: the default repository, where the
  `once read` line carries no `--owner` or `--repo`, and another one, where
  it carries both
  (`tests/test_cli_decisions.py::test_marking_read_clears_what_the_run_printed`).
- An id not outstanding, in each of the three ways above: the error names
  it, exit 1, the record unchanged, and no `gh pr comment`
  (`tests/test_cli_decisions.py::test_an_id_not_outstanding_records_nothing`).
- A pull request named that cannot be read: exit 1, the record unchanged
  (`tests/test_cli_decisions.py::test_a_pull_request_that_cannot_be_read_records_nothing`).
- The same `--mark-read` twice: the second prints `already recorded:` for
  each id, the record is unchanged, and the exit code matches the first
  (`tests/test_cli_decisions.py::test_marking_read_twice_changes_nothing`).
- An open pull request whose head has moved, with no notice: once
  `--mark-read` leaves nothing outstanding, the decision line reads "the head
  has moved since the last review", the command exits 2, and no
  `gh pr comment` runs, with `--wait` or without. With a rate-limit notice
  whose wait has not passed, and `--wait`, the decision line reads "rate
  limited", nothing sleeps, and nothing is posted
  (`tests/test_cli_decisions.py::test_marking_read_posts_nothing`).
- `--dry-run` beside `--mark-read`: `would record as read:` for each id, and
  the record unchanged (`tests/test_cli_decisions.py::test_a_dry_run_records_nothing`).
- A truncated pull request: after `--mark-read` with every printed id, it
  still needs a reply, prints no `once read` line, and exits 3
  (`tests/test_cli_decisions.py::test_marking_read_leaves_a_truncated_pull_request_outstanding`).
- A record that exists but cannot be read, and one that cannot be written:
  the error for each, exit 1, and the file as it was
  (`tests/test_cli_decisions.py::test_a_record_it_cannot_use_is_left_as_it_was`).

### What changes

- `services/api/src/harrier_cli/main.py`: `review-followup` gains
  `--mark-read`. A thread line names its last comment, and the `once read`
  line follows. With `--mark-read`, every pull request named is read and
  every id checked before anything is recorded, and the run then reports as
  a dry run.
- `services/api/src/harrier/reviewfollowup.py`: `ids_not_outstanding` says
  which of the given ids the pull requests read do not hold outstanding and
  the record does not hold, so the command and its tests read one rule.
  `read_handled` tells a damaged record from a missing one, for
  `--mark-read`, and `load_handled` reads through it as before.
  `PullRequestState.after_recording` gives the state the record now gives,
  so the run decides on it without reading GitHub twice. `record_handled`
  and the record's format do not change.
- `services/api/tests/test_cli_decisions.py`: the tests above.
- `specs/043-review-followup.md`: this amendment, its row in the criteria
  table, and its checklist item, ticked when the tests exist.

No new file, so `config/data-classification.json` does not change.

### Out of scope

- **Answering or resolving from the command.** Replies and resolutions stay
  on GitHub.
- **Forgetting an id.** Nothing un-records one. An id recorded by mistake
  hides that reply until its thread gains another. Until then, removing it
  means editing the record by hand.
- **Links to the replies.** The lines name ids, not addresses. Printing where
  to read each reply needs a field the query does not fetch, so it is its own
  change.
- **Deciding what is read.** Nothing recognises an acknowledgement by its
  wording. Reading stays judgement, as this spec says.
- **Where GitHub GraphQL is refused.** `gather` needs it, so the command,
  `--mark-read` included, exits 1 there. That is the environment, not this
  spec.

### Migration

None. Ids already in the record stay recorded, however they got there. No
script, hook, workflow or recipe in the repository runs the command or reads
its output, so the longer thread line and the new `once read` line need
nothing else changed.

### Limitations

- Two `--mark-read` runs at the same moment can lose one run's ids: each
  reads the record, adds its own, and renames over the other's. The lost ids
  show again on the next run, which fails towards showing too much.
- A usage error, such as `--mark-read` with no ids, exits 2, as every usage
  error of this command already does through `argparse`. That reads like
  "not reviewed at the head". It is older than this amendment and left to its
  own change.

## Amendment (2026-10-07): the report line names a truncated query

Found on 2026-10-07 while writing the tests for PR #165, and listed there as
found, not fixed.

A bounded query that has another page makes a pull request outstanding, so
the command exits 3 rather than call it settled
(`tests/test_cli_decisions.py::test_a_truncated_page_of_findings_still_exits_three`).
The decision line says why. The report line does not. At 29055af, through
`decide` and `report` in `services/api/src/harrier/reviewfollowup.py`, a pull
request truncated and with nothing else outstanding printed the decision
line `PR #9: a bounded query had another page, so this is not a full picture`
and the report line `PR #9: NEEDS A REPLY:` with nothing after the colon. One
truncated with a thread awaiting a reply printed:

```
PR #9: 1 thread(s) awaiting a reply; a bounded query had another page, so this is not a full picture
PR #9: NEEDS A REPLY: 1 thread(s) awaiting a reply
```

The same empty line was found in the decision during the review of PR #50,
and the decision was fixed; the comment in `decide` records it. `report`
builds its own list of parts and was left as it was.

The report lines are the summary a run ends with, and in a run over several
pull requests the only place their states sit together. There a truncated
pull request reads as needing a reply with nothing named, or as needing only
the replies it names, when the run could not see all of it. Truncation was
not rare. Every reply in a thread is a review node, and `gather` read the
newest twenty, so a pull request with a long conversation was truncated on
every run (the amendment above on replies, under Out of scope). The amendment
above on the review window reads back past the replies, so a long
conversation alone no longer truncates
(`test_replies_past_the_window_do_not_hide_the_review`).

### Behavior after the change

- When a bounded query had another page, the report line of the outstanding
  pull request ends with
  `a bounded query had another page, so this is not a full picture`, after
  any parts it already gives, separated by `; `. These are the decision
  line's words, so the two lines read one fact.
  - Truncated, with nothing else outstanding:
    `PR #N: NEEDS A REPLY: a bounded query had another page, so this is not a full picture`.
  - Truncated, with a thread awaiting a reply:
    `PR #N: NEEDS A REPLY: 1 thread(s) awaiting a reply; a bounded query had another page, so this is not a full picture`.
- So no report line for an outstanding pull request ends at its colon. Each
  of the three things that make one outstanding (a thread awaiting a reply, an
  unread review, truncation) now adds a part.
- Nothing else changes: the decision, its line, the exit code, and the report
  line of an outstanding pull request that is not truncated.

Considered and not chosen: a label of its own for truncation alone, such as
`MAY NEED A REPLY`. Truncation fails closed because the command cannot show
that nothing waits, and exit 3 already says so. A second label would say the
same thing another way, and a reader would have to learn both.

### Failure modes this must not introduce

- The report line of an outstanding pull request that is not truncated
  changes.
  `tests/test_cli_decisions.py::test_one_pull_request_awaiting_review_hides_another_awaiting_an_answer`
  pins one, `PR #8: NEEDS A REPLY: 1 thread(s) awaiting a reply`, and holds
  this.
- The two lines drift apart again. One string serves both, as What changes
  says.
- The decision or the exit code changes. No test of `decide` changes, and
  `tests/test_cli_decisions.py::test_a_truncated_page_of_findings_still_exits_three`
  still holds.

### Acceptance criteria

- `test_a_truncated_pull_request_says_so_in_its_report_line`, in
  `services/api/tests/test_review_followup.py`, with three cases: truncated
  with nothing else outstanding, truncated with a thread awaiting a reply, and
  truncated with an unread review whose findings are outside the diff. Each
  asserts the whole report line, and that the decision line ends with the
  same words. Before the change, all three fail.

### What changes

- `services/api/src/harrier/reviewfollowup.py`: `report` adds the part when a
  query was truncated. Its words become one module constant that `decide`
  and `report` both use, so they cannot drift.
- `services/api/tests/test_review_followup.py`: the test above.
- `specs/043-review-followup.md`: this amendment, its row in the criteria
  table, and its checklist item.

No new file, so `config/data-classification.json` does not change.

### Out of scope

- **Reading past the bound.** A truncated pull request still exits 3 on every
  run, as the amendment above on replies records. Reading further pages, so
  the line has less cause to appear, is its own change.

### Migration

None. No script, hook, workflow or recipe in the repository runs the command
or reads its output, so only the people who read the report lines see the
change.

### Limitations

- The line says the picture is partial. It cannot say what the unread page
  holds, or whether anything there waits on an answer.

## Amendment (2026-10-07): a dry run never waits and never asks

Found while writing the tests for PR #165, by reading `_cmd_review_followup`
in `services/api/src/harrier_cli/main.py`, and listed there as found, not
fixed. Then run at 2ea1e10, with `gh` and `time.sleep` stood in so that
nothing was sent and nothing waited, on an open pull request reviewed only
before its head moved and carrying a rate-limit notice of 38 minutes:

| Flags | Slept | Posted | Exit |
|---|---|---|---|
| `--dry-run` | nothing | nothing | 2 |
| `--wait` | 2340 seconds | `@coderabbitai review` | 2 |
| `--dry-run --wait` | 2340 seconds | `@coderabbitai review` | 2 |

`--dry-run` is documented in its help as "report what it would do and comment
nothing". Beside `--wait` it did neither. The run waited out the limit,
posted the request, printed `PR #147: review requested`, and counted the
request against the daily bound. The branch that waits tests `--wait` and
not `--dry-run`, while the request branch below it tests `--dry-run`. No test
reaches the branch that waits. The only one that passes `--wait`,
`tests/test_cli_decisions.py::test_marking_read_posts_nothing`, passes it
beside `--mark-read`, which never waits.

Whoever adds `--dry-run` to see what a waiting run would do gets the wait
itself, as long as the notice says, with the terminal or session held for it.
Then a comment goes up on the pull request and asks for the review. That
comment is the outward action `--dry-run` exists to prevent. This spec's
line on dry runs covers only the request: "a request, reported and not posted
under `--dry-run`" (the amendment above on replies). It never said what a dry
run does with a wait.

### Behavior after the change

- Under `--dry-run` the command never sleeps and never posts, whatever else it
  is given. With `--wait` beside it, a decision to wait prints its decision
  line, `PR #N: rate limited, M minutes to wait`, as a dry run without
  `--wait` does, and the run goes on to the next pull request.
- Under `--dry-run` no request is counted against the daily bound. The count
  is written only when a request is posted, as now.
- Without `--dry-run`, `--wait` keeps what it does: it sleeps out the wait,
  posts `@coderabbitai review`, prints `PR #N: review requested`, and counts
  the request.
- The decision, the report lines and the exit codes do not change.

Considered and not chosen:

- **Refusing the two flags together.** `argparse` exits 2 for a usage error,
  and in this command 2 reads as "not reviewed at the head". The pair also
  has an honest meaning: show what a waiting run would decide.
- **A line of its own, such as "would wait M minutes, then ask".** The
  decision line already states the wait, and a dry run's request decision
  prints no "would ask" line either.

### Failure modes this must not introduce

- `--wait` without `--dry-run` stops waiting or asking.
  `tests/test_cli_decisions.py::test_waiting_without_a_dry_run_still_asks`
  holds this. It passes before the change and after it, so it pins what the
  change keeps.
- A dry run's request decision starts posting. The tests that run
  `--dry-run` through `_install` in `services/api/tests/test_cli_decisions.py`
  replace the request with one that fails the test, and hold this.

### Acceptance criteria

Both in `services/api/tests/test_cli_decisions.py`, through the real `gather`
with `gh` stubbed and `time.sleep` replaced, on the pull request in the table
above:

- `--dry-run --wait`: nothing sleeps, no `gh pr comment` runs, the daily count
  is not written, and the decision line starts
  `PR #147: rate limited, `. Before the change it fails: the run sleeps and
  posts
  (`tests/test_cli_decisions.py::test_a_dry_run_never_waits_and_never_asks`).
- `--wait` alone: one sleep of the wait the notice leaves, one
  `gh pr comment` with `@coderabbitai review`, `PR #147: review requested`,
  and a daily count of 1
  (`tests/test_cli_decisions.py::test_waiting_without_a_dry_run_still_asks`).

### What changes

- `services/api/src/harrier_cli/main.py`: the branch that waits tests
  `--dry-run` as well. Nothing else in the command changes.
- `services/api/tests/test_cli_decisions.py`: the two tests above.
- `specs/043-review-followup.md`: this amendment, its row in the criteria
  table, and its checklist item.

No new file, so `config/data-classification.json` does not change.

### Out of scope

- **Waiting across several pull requests.** `--wait` sleeps for each in turn,
  and each request after the first earns a notice of its own, as the
  amendment above on the reworded notice records. That is unchanged.

### Migration

None. A run that used `--dry-run --wait` to post a request was relying on the
defect, and gets the same request by leaving out `--dry-run`.

### Limitations

- A dry run shows the decision at the moment it runs. It cannot show what the
  service will say when the wait ends.
