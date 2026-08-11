# principal-review: seed tasks

Opening assignments per reviewer. These are the questions worth asking first,
not the whole review. Follow what you find. Each is a place where the design
made a real choice that deserves to be defended or overturned.

## review-principal-architect

1. Delete each abstraction mentally and record what breaks: the `harrier.llm`
   facade, the sources/screening split, the run manager and its SSE channel,
   the profile document store, the config store's `scope` column, demo mode's
   fixture layer, the parity tooling. Any that break nothing are the finding.
2. Judge whether `specs/`, `.ai/`, four guardians, the commit-trailer gate and
   CI trailer resolution are proportionate to a tool with one user, and
   whether the README justifies that machinery where a reader first meets it.
3. Several approved specs were corrected by their own implementation, and one
   acceptance criterion was written, approved, then judged wrong and rewritten
   during the work it governed. Decide whether the gate catches design errors
   before they land or mostly generates paperwork afterwards, and say which.
4. Nine tracker CLI verbs were missing for the project's whole life because
   they fell between two specs that each assumed the other owned them. Judge
   what that says about specifications as a coverage mechanism.
5. The `scope` column exists to make multi-tenancy possible later, with no
   tenancy anywhere. Judge speculative seams generally, and this one
   specifically.
6. Ask what a second user, a hosted deployment, or a fifth job board would
   force. Which extends cleanly and which forces a rewrite?

## review-domain-model

1. Read `harrier/tracker/schema.py`, `store.py` and the API's `JobOut` cold and
   write down what you think each field means before reading anything that
   explains it.
2. Every column is `TEXT` and every `JobOut` field is `str`, including scores,
   dates and counts. For each, say what an empty string means: unknown, not
   applicable, or zero. Name the ones where those are genuinely different.
3. `notes` was a key=value store whose keys became columns. Check whether both
   are still written and whether they can disagree.
4. Statuses have no transitions, deliberately. Name the sequences that are
   nonsense and say whether the absence has cost anything yet.
5. The outreach fields are a second state machine on the same row. Judge
   whether the two axes are independent in practice.
6. Name every illegal state the schema permits, and which are prevented only
   by application code being careful.

## review-screening

1. Read the gate order and say what each gate removes that the next would not
   have. A gate that never fires is a finding.
2. From the scoring rules alone, work out what a typical in-scope posting
   scores, and whether the cutoff sits anywhere near that. If nothing that
   reaches the cutoff is rejected by it, say what the real filter is.
3. Judge the remote-only and EMEA gates against messy real location strings.
   Which correct postings would be rejected?
4. Check that no rule silently re-rejects the EU-permit phrasings that are
   deliberately positive signals.
5. Judge the enrichment fetch: does it change outcomes often enough to justify
   the requests, and what happens when it fails?
6. The seen-state layer means a posting rejected under old rules is never
   reconsidered after the rules change. Decide whether that is right.

## review-honesty-gates

1. Find the exact line where a generated resume bullet is accepted and state
   precisely what property is verified there.
2. Write an invented bullet that passes: plausible, unsupported, and not
   contradicted by the truth document. Judge how hard that was.
3. Confirm a PDF-gate failure leaves no artefact and no tracker mutation, and
   that the tracker advances only after the artefact exists.
4. Do the same for cover letters and application answers.
5. Judge the deterministic no-LLM fallback: what does it produce, and is it
   honest about being a template?
6. Find where internal metadata could reach a recruiter-facing document.

## review-operability

1. For every scheduled job, say how a silent failure would be noticed. An
   absence of output is not a signal.
2. Could an operator reconstruct why a discovery run produced nothing, or only
   observe that it did?
3. Enumerate the partial-work windows: fetch then die before persisting, PDF
   written then tracker update fails, outreach drafted twice.
4. Say exactly what an attacker on the same machine, or a malicious page in
   the browser, reaches through an unauthenticated `POST /runs`.
5. Judge what is actually recoverable after a disk failure against what the
   documentation promises.
6. The cutover rollback re-loads what it unloaded. Say what state the operator
   is in if the rollback itself fails partway.
7. Find every place a secret could reach a log, a summary artefact, or a
   Telegram message. Report the sink and the kind of secret, never the value:
   the finding is "provider key reaches the run summary", and a report that
   proves it by quoting the key has moved the key somewhere new.
