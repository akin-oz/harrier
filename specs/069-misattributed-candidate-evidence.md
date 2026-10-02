---
spec: 069
title: Candidate evidence quoted from the posting or the application profile is named as such
status: accepted
approved: yes
milestone: M8
depends: [065, 066]
---

# Spec 069: Candidate evidence quoted from the posting or the application profile is named as such

## Problem

A cover letter run on 2026-10-02 was refused with three `unverified
evidence` violations. All three refusals were correct, and none of them
said why. Checked read-only against the live data:

| Refused candidate evidence | Where the text actually is |
|---|---|
| "architectural influence without management theatre" | the application profile only |
| "Partner with ML and backend engineers to define APIs, events, data contracts, and interaction patterns..." | the job posting |
| "Collaborate closely with Product and UX to translate customer needs..." | the job posting |

The model labelled text from the wrong source as evidence about the
candidate. The prompt already forbids this. `SYSTEM_PROMPT_BASE` in
services/api/src/harrier/apply/letters.py says "Candidate evidence comes
only from resume_truth_source_md or latest_project_achievements_md" and
"Use the application profile for positioning and safe framing only". The
answers prompt in services/api/src/harrier/apply/answers.py says the same.
The model broke a rule it was given.

Two costs follow, both to the operator:

1. **The refusal message hides the cause.** `unverified evidence` reads as
   "the model invented this". Here it quoted real text from the wrong
   document. Telling those apart meant querying the database by hand. That
   matters because the two call for different responses. Invention is a
   reason to distrust the draft. Misattribution is a labelling slip, and
   a regeneration usually fixes it.
2. **The prompt states the rule as a source list, not as a prohibition.**
   It says where candidate evidence comes from. It does not say that the
   posting and the profile are the two documents the model must never cite
   for the candidate, which are exactly the two it reached for here.

This is one observed run. The spec does not claim a rate.

## Scope

Two changes, in the letter path and the answers path alike:

1. **A sharper refusal message.** A candidate evidence fragment that fails
   C2 (spec 065) is refused exactly as today. The message names where the
   fragment was found when it was found somewhere other than the truth
   sources.
2. **An explicit prohibition in both prompts.** The claims section of each
   system prompt states that text from `job_description_text` and from the
   application profile is never candidate evidence, and that a requirement
   the posting lists is not something the candidate did.

Nothing that passes today is refused, and nothing refused today passes.

## Behavior

### Refusal message

C2 runs as today: a `candidate` evidence fragment passes only if
`TruthSources.contains` is true (operator brief evidence included, spec
066). When it fails, the message depends on where the fragment is found,
checked in this order:

| Fragment found in | Violation message |
|---|---|
| the posting (job description plus employer guidance, the same text C3 reads) | `posting text cited as candidate evidence: <fragment>` |
| the application profile (markdown or JSON document) | `application profile cited as candidate evidence: <fragment>` |
| neither | `unverified evidence: <fragment>` (unchanged) |

"Found" uses the comparison C3 already uses (`_norm` in
services/api/src/harrier/apply/claims.py, which ignores case, whitespace and
inline markup per spec 068).

A fragment present in both the truth sources and the posting passes, as
today. Truth wins: the candidate did the thing, and the posting asking for
it does not change that.

The application profile text reaches the check the same way the posting
does: the caller passes it into `ClaimContext`. Both the letter path
(`generate_cover_letter`) and the answers path (`generate_answer_set`) pass
it.

### Prompt rule

The "Claims and evidence" section of `SYSTEM_PROMPT_BASE` in letters.py,
and the matching section in answers.py, gain this rule after the existing
source sentence:

> Never cite job_description_text or the application profile as candidate
> evidence. A requirement the posting lists is something the employer wants,
> not something the candidate did. If a sentence about the candidate has no
> evidence in the truth sources, leave it out.

The existing wording stays. The rule is added, not substituted.

## Failure modes

- **Short fragment found everywhere.** A fragment like "TypeScript" is in
  the posting and the profile but not verified as a truth line. C2 refuses
  it and the message says `posting text cited as candidate evidence`. That
  is accurate: the posting is where the model could have found it.
- **Fragment in the profile and the posting.** The posting message wins,
  by the order above. One message per fragment.
- **No application profile stored.** The profile check finds nothing, and
  the message falls through to `unverified evidence`. No error.
- **Malformed profile JSON.** Searched as raw text. The check is a
  substring search, not a parse.
- **Count of violations.** Each failing fragment still yields exactly one
  violation, so the violation count of every refusal is unchanged.

## Acceptance criteria

Proof lives in services/api/tests/test_apply_claims.py. Every test runs
through `generate_cover_letter` or `generate_answer_set` with a stubbed
provider and synthetic documents. Test names are chosen when written, and
this list becomes a table naming them in the implementing change.

- A letter citing a posting sentence as candidate evidence is refused, and
  the refusal contains `posting text cited as candidate evidence` and not
  `unverified evidence` for that fragment.
- A letter citing an application profile line as candidate evidence is
  refused with `application profile cited as candidate evidence`.
- A fragment in neither document is refused with `unverified evidence`, as
  today.
- A fragment in both the truth document and the posting passes.
- A fragment in both the posting and the profile gets the posting message.
- The answers path refuses a posting sentence cited as candidate evidence
  with the posting message.
- With no application profile stored, a profile-shaped fragment gets
  `unverified evidence` and no exception.
- The prompt rule: the letter and answers system prompts both contain the
  new prohibition. This is a check on prompt text, the one place a
  source-text assertion is the decision itself.

- [ ] every criterion above has a test
- [ ] the message tests fail with the change reverted
- [ ] spec 065's existing tests pass unchanged
- [ ] no real profile, truth or posting content in any test
- [ ] `uv run ruff check` and `uv run pyright` clean
- [ ] All gates green on PR

## Proof / origin

The 2026-10-02 run above. The three fragments were located read-only
inside the container: the first in the `application_profile` document
`application-profile.md`, the other two in a stored job description file
under `data/descriptions/`. The C2 message is built in `check_claims`
(services/api/src/harrier/apply/claims.py); it does not consult the posting
or the profile when choosing the message.

## Limitations

- **The prompt rule cannot be proven to work.** A test can show the rule is
  in the prompt. It cannot show the model obeys it, and no rate is claimed.
  The refusal stays the guarantee; the rule only aims to make refusals
  rarer.
- **A paraphrased posting requirement is not caught as misattribution.**
  The message check is a substring match. A model that rewords a posting
  line still gets `unverified evidence`, as today.

## Out of scope

- **Automatic retry.** Feeding violations back to the model for one more
  attempt would make most of these refusals invisible. It changes run
  behavior and cost, and is its own spec.
- **Any change to what passes or fails.** This spec changes messages and
  prompt text only.
- **The interview story bank.** One of the refused sentences resembles a
  "JD Hook" line there. That document is not candidate evidence today and
  this spec does not change that.
- **Showing violations differently in the web UI.** The new messages appear
  wherever the current ones do.

## Migration

None. Stored documents are unchanged. Refusals that read `unverified
evidence` today may read one of the two new messages.
