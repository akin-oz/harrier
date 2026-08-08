---
description: Parallel single-lens review of the current branch diff before commit
---

Review the working changes (`git diff main...HEAD` plus uncommitted changes) with the
four review agents, then synthesize one verdict.

1. Determine the changed scope. If the diff is empty, say so and stop.
2. Spawn in parallel, each scoped to the diff:
   - contract-guardian (skip if neither `services/api` nor `apps/web` nor
     `packages/contract` changed)
   - data-integrity-reviewer (skip if `services/api` did not change)
   - privacy-reviewer (never skip)
   - fsd-reviewer (skip if `apps/web` did not change)
3. Synthesize: deduplicate overlapping findings, rank P0 (blocks commit) to P2
   (note in PR), and state one verdict: safe to commit (name the `Spec: NNN` to use)
   or fix P0/P1 first.

Do not fix anything in this command. Review only; surface, do not patch.
