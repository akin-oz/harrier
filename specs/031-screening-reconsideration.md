---
spec: 031
title: A screening decision can be reconsidered when the rules change
status: accepted
approved: yes
milestone: M6
depends: [007, 011]
---

# Spec 031: A screening decision can be reconsidered when the rules change

## Problem

services/api/src/harrier/screening/pipeline.py adds a posting's key to the
seen set before any gate has decided anything, and that set is the persisted
one: discovery loads it with `load_seen_keys` and writes it back with
`save_seen_keys`. So a rejection is permanent. The next run skips the posting
as already seen, and no change to the rules can ever reach it.

Nothing records why. The stored payload is keys and a timestamp, with nothing
about the policy that produced the decision, so it is not possible to ask
which postings were rejected under a rule that has since been corrected.
`write_rejected_debug` exists and no caller ever passes `True`, so a
rejection leaves one integer in a per-run counter.

This makes every screening fix retroactively worthless, which matters
because spec 032 is a set of screening fixes. Correcting the location gate
changes nothing for any posting already rejected by it.

Re-screening is cheap: the description cache is keyed by URL and already
populated, so reconsidering costs CPU and no network.

Found by the `principal-review` board (spec 028), screening lens, which
marked every other screening finding void until this is fixed.

## Scope

**Record the decision, not just the sighting.** The seen entry carries the
verdict, the gate that produced it, and a policy version. A rejection is a
decision with a reason attached, which is what makes it reviewable.

**A policy version that changes when the policy does.** Derived from the
screening configuration and rule set so it cannot be forgotten: any change to
the weights, the keyword lists, or the gate order produces a different
version.

**Reconsider on demand.** A command re-runs screening over postings rejected
under a policy version other than the current one, and reports what changed.
Not automatic, because re-screening after every config edit is surprising;
the operator asks for it.

**Mark seen after the decision, with the decision.** A posting suppressed
before it has been judged cannot be judged later.

## Inputs, outputs, failure modes

- Inputs: the persisted seen state, the current screening configuration, the
  cached descriptions.
- Outputs: seen entries carrying a verdict and policy version; a
  reconsideration report of what would change and what did.
- Migration: existing entries carry no verdict and no version. They are
  treated as decided under an unknown policy, which makes them eligible for
  the first reconsideration rather than invisible to it. Read as unknown, not
  as rejected, so the migration cannot resurrect a posting into the tracker
  without a decision.
- Failure mode this must not introduce: reconsideration that re-adds a
  posting the operator manually rejected. A human decision outranks a rule
  and is never overturned by a policy change.
- Failure mode this must not introduce: unbounded growth. The seen state
  gains fields, so its size bound is revisited in the same change. Eviction
  is currently by lexicographic hash order, which is stable and therefore
  evicts the same entries forever while keeping genuinely stale ones; it
  becomes age-based.

## Acceptance criteria

Proving symbols are named at implementation, in
services/api/tests/test_seen_policy.py.

- [ ] a posting is marked seen only after a verdict exists, and the verdict is
      stored with it
- [ ] changing any screening weight or keyword list changes the policy version
- [ ] reconsideration re-screens rejections from an older policy version and
      leaves current-version rejections untouched
- [ ] a manually rejected job is never resurrected by reconsideration
- [ ] entries written before this change are treated as unknown policy and
      are eligible, and none of them enters the tracker without a fresh
      decision
- [ ] eviction is age-based, proven by a test where the stable-hash rule would
      evict the wrong entry
- [ ] reconsideration performs no network request when the description cache
      covers the postings
- [ ] no company name, posting title, or URL enters any committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The premature `add`, the payload
shape, and the unreachable `write_rejected_debug` argument are verifiable in
the tree.

## Out of scope

Changing any screening rule, which is spec 032. Storing full postings for
rejected rows: the keys and the verdict are enough to reconsider, and the
posting is refetchable.
