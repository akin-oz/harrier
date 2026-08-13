---
spec: 046
title: The history stops holding what the tree no longer does
status: in-progress
approved: yes
approved-note: >
  Approved by Akin in session on 2026-08-13, verbally rather than by editing
  this file. Force-push authorisation was given and then withdrawn in the same
  session in favour of a pull request, so the rewrite in this spec is prepared
  and verified but NOT executed. See "Status of the rewrite" below: the two
  blobs are still reachable from the published history.
milestone: M7
depends: [044]
---

# Spec 046: The history stops holding what the tree no longer does

## Problem

Spec 044 took the maintainer's real job search out of the working tree. None
of it reached git history, and history is what a clone gets.

Two config files were untracked in a single commit and their blobs remain
reachable: `config/feeds.txt`, which held the complete real target-employer
watchlist, and `config/linkedin_search_urls.txt`, which held the real saved
searches with their annotations. Both are classified never-in-git today. They
were public for the entire period between being committed and being removed,
and removing a file from the tree does not remove it from history.

Four commit bodies state counts measured from the real tracker: total rows, a
contacts total, the status composition of a page, and the per-board fetch
volume of one live production run. Five pull request descriptions carry the
same class, the most precise giving the real row total, the rejected count and
a page's status breakdown.

Every one of those was written by honestly recording verification evidence,
which is exactly why no gate caught it: `gitleaks` matches credential shapes
and nothing inspects prose. The spec 044 aggregate check reads the working
tree, so it cannot see a commit message either.

And the pre-publish checklist in `docs/privacy-plan.md` asserts that the only
privately-classed file ever committed was an encrypted placeholder with no
personal content. History contradicts that, so a reviewer working the
checklist honestly would have signed off on a repository whose history holds
the watchlist.

## Scope

**The two blobs leave every reachable commit.** Filtered out of all refs, not
merely deleted in a new commit.

**The four commit bodies lose their counts.** Rewritten in the same pass, so
one rewrite covers both classes and there is no second force push.

**The five pull request descriptions are edited on GitHub.** They live in
GitHub's database, not in git, so no rewrite touches them.

**The checklist stops asserting something false**, and gains the one line it
was missing: that a rewrite is only complete when the remote no longer serves
the old objects.

## Inputs, outputs, failure modes

- Inputs: the repository including all refs, and the pull request bodies.
- Outputs: a rewritten history with no reachable copy of either file and no
  tracker aggregate in a commit body; edited pull request descriptions; a
  corrected checklist.
- **This is irreversible and it is a force push to a public repository.** A
  mirror clone is taken outside the repository first, and its path is recorded
  in the pull request so the previous history is recoverable by the maintainer
  even though it is gone from the remote.
- Failure mode that must not happen: a rewrite that reports success while the
  blob is still reachable. Verification greps the whole rewritten object graph
  by path and by content, not just the tip.
- **Honest limitation, and the important one.** A force push does not unring
  the bell. Anyone who cloned or forked the repository before the rewrite
  keeps the old objects, GitHub may serve a cached unreachable commit by SHA
  until it garbage-collects, and forks retain their own copies. This reduces
  exposure; it does not undo it. The watchlist and the searches should be
  treated as having been public, which is a decision about those companies and
  searches rather than about this repository.
- Second limitation: the rewrite changes every commit SHA on `main`. Any link
  to a commit from outside this repository breaks.

## Status of the rewrite

**Not done. The blobs are still public.** Recorded here rather than in a
commit message because a reader of this spec needs to know its central claim
has not been carried out.

What was done and verified on a local rewrite, which was then discarded:

- `git filter-repo` removed both paths from every ref, and the resulting
  working tree was byte-identical to the one before it (tree `53aaa763`), so
  the rewrite changed history and nothing else.
- No blob for either path remained in the local object graph, and no commit
  body retained a tracker count.
- The full gate passed on the rewritten history.

Why it stopped:

1. The force push was rejected by the repository ruleset `main-protection`,
   which carries `non_fast_forward` and `required_status_checks` and has no
   bypass actors. Relaxing that is a settings change and the maintainer's.
2. The maintainer then chose a pull request over a force push. A merge cannot
   remove an object from history: it only adds a commit on top. So this route
   closes none of P0-5.

The mirror backup of the pre-rewrite history is at
`~/harrier-history-backup-20260813-191905.git`, outside the repository.

What remains open, stated plainly: anyone who clones this repository today
gets `config/feeds.txt` and `config/linkedin_search_urls.txt` in full, holding
the real target-employer watchlist and the real saved searches with their
annotations. Publishing more widely without resolving this publishes those.

The pull request bodies were the one half of this spec that could be finished
without a rewrite, because they live in GitHub's database rather than in git,
and they are done.

## Acceptance criteria

- [ ] neither `config/feeds.txt` nor `config/linkedin_search_urls.txt` is
      reachable from any ref, verified over the full object graph
- [ ] no commit body states a count measured from the real tracker
- [ ] the working tree after the rewrite is byte-identical to the working tree
      before it, so the rewrite changed history and nothing else
- [ ] the full gate passes on the rewritten history
- [x] the pull request descriptions no longer carry aggregates (six matched,
      not five; #19's numbers count the parity matrix's own table rather than
      the search and were left)
- [x] the checklist no longer claims the only privately-classed file ever
      committed was an encrypted placeholder
- [x] the mirror backup path is recorded in this spec and the pull request
- [ ] All gates green on PR

## Proof / origin

The `open-source-readiness` agent team (spec 028), privacy lens, run
2026-08-13, findings P0-5 and P0-6. The claim-auditor lens independently found
the checklist assertion that contradicts the same history.

## Out of scope

Contacting GitHub to expire cached objects, and any attempt to reach forks.
Both are outside what this repository can do, and the limitation above says so
rather than implying the rewrite is a complete remedy.
