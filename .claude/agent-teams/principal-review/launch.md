# principal-review

A standing review board: five senior reviewers each take the whole project
through one lens, and a lead merges their findings into a single prioritised
report. This is the review a senior engineering organisation would assemble
to interrogate the design, not to check whether rules were followed.

Run it when a milestone lands or before defending the work. It is a critique,
not a gate.

## How this differs from the standing guardians

The guardians check **compliance**; this board exercises **judgement**. The
distinction matters, because a repository can be perfectly consistent with a
design that was wrong to choose, and nothing in harrier's governance is
currently allowed to say so.

| Question                                              | Who answers it            |
| ----------------------------------------------------- | ------------------------- |
| Is the generated contract in sync with the code?      | `contract-guardian`       |
| Is the contract we designed a good one to live with?  | `review-principal-architect` |
| Does any file breach the classification table?        | `privacy-reviewer`        |
| Would a stranger learn something about the maintainer? | the `open-source-readiness` team |
| Do the layer boundaries hold?                         | `fsd-reviewer`            |
| Are those the right boundaries for this problem?      | `review-principal-architect` |
| Is this good engineering?                             | this board                |

## Members

| Reviewer            | Subagent                     | Model  | Lens |
| ------------------- | ---------------------------- | ------ | ---- |
| Principal architect | `review-principal-architect` | opus   | Do the abstractions earn their keep, and is this much governance proportionate to a one-user tool? |
| Domain model        | `review-domain-model`        | sonnet | Every column is a string and statuses have no transitions. What can the model not express? |
| Screening           | `review-screening`           | opus   | Is the score discriminating, or has the cutoff stopped doing work? |
| Honesty gates       | `review-honesty-gates`       | opus   | The validator catches contradiction. Does it catch invention? |
| Operability         | `review-operability`         | opus   | A scheduled job failed silently for two months. Would this one? |

All five are read-only in the enforceable sense: `Read, Glob, Grep` and no
`Bash`, so nothing they can invoke writes, installs, or reaches the network.
They read and judge, which is all this board does. The investigators on
`open-source-readiness` do need to execute, and that team's launch document
says so rather than claiming otherwise.

They report findings as
`file:line: what. Fix:` at P0, P1, or P2. Four run on `opus` because each
is judgement about what is absent or what could happen, which is harder than
reading what is there.

## Scope discipline

Each reviewer has one lens and hands off anything outside it rather than
duplicating. The seams that will tempt them:

- The score is **screening's** call on whether it discriminates and
  **domain model's** call on whether a stored score still means anything
  after the weights change.
- The seen-state layer is **screening's** call on whether suppression is
  right and **operability's** call on what happens when it is lost.
- The PDF gate is **honesty's** call on what it proves and **operability's**
  call on what a failure leaves behind.
- Anything where a document disagrees with the code belongs to
  `readiness-claim-auditor` on the other team, not to this board.

## How to run it

Agent teams are experimental and gated behind an environment flag:

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

---

> Convene the **principal-review** board over this repository and return
> **one** prioritised report. Spawn all five read-only reviewers **in
> parallel**.
>
> Shared context: harrier is a local-first job search tool for a single
> user. It discovers postings across job boards, screens them against a
> remote-only and region policy, keeps one SQLite tracker as the source of
> truth, generates tailored application material behind correctness gates,
> drafts outreach, watches a mailbox, and sends a nightly digest. It is a
> ground-up rewrite of a working private system and is not yet the one in
> daily use: cutover is specified and unperformed. Every change is gated on
> an approved spec under `specs/`, and the repository is public while the
> data is a real person's job search.
>
> - **`review-principal-architect`**: is this the right design? Delete each
>   abstraction and see what breaks. Count `specs/*.md`, then judge whether
>   that many specs, four
>   guardians and a trailer gate are proportionate to one user, given that
>   several approved specs were corrected by their own implementation and
>   that nine CLI verbs went missing between two specs that each assumed the
>   other owned them.
> - **`review-domain-model`**: read the tracker schema and `JobOut` cold and
>   say what the model cannot express. Every column is text, statuses have no
>   transitions, and the notes key=value store was promoted to columns that
>   may now disagree with it.
> - **`review-screening`**: work out from the scoring rules what a typical
>   in-scope posting scores, and whether the cutoff sits anywhere near it.
>   Judge the gate order, the location parsing against real phrasings, and
>   the seen-state layer that makes a rule change unable to reconsider
>   anything already rejected.
> - **`review-honesty-gates`**: find the exact point a generated line is
>   accepted and state what property is verified. Then write the invented
>   line that passes: plausible, unsupported, uncontradicted.
> - **`review-operability`**: start from the job in the old system that
>   failed silently for two months. For every scheduled job here, say how a
>   silent failure would be noticed, and what an attacker on the same machine
>   reaches through an unauthenticated `POST /runs`.
>
> Findings name the file, the line, and what is wrong. They do not quote
> secret values, message contents, tokens, or anyone's identity: this
> repository is public and a report gets pasted into places the code never
> reaches.
>
> Have each return its own P0/P1/P2 report. Then merge into one list ordered
> by severity and blast radius, deduplicated across lenses, ending with the
> single change you would make first and the strongest thing about the
> design.

---

Seed tasks for each reviewer are in `tasks.md`.
