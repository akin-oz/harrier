---
spec: 035
title: The local API is not an open door
status: in-progress
approved: yes
milestone: M6
depends: [006, 023]
---

# Spec 035: The local API is not an open door

## Problem

The API binds locally and has no authentication, which is a reasonable
starting position for a single-user tool. What makes it a finding is what an
unauthenticated caller reaches, and that two of the paths are reachable from
a web page rather than only from a local process.

`POST /runs` is allowlisted to two run kinds, so there is no command
injection. It does reach a subprocess that inherits the server environment
and therefore every credential; a billed Apify actor run whose count is
unbounded and settable through `PUT /config/discovery`, which validates only
that the body is an object; and `PUT /config/linkedin_searches`, whose
strings reach the actor unvalidated. Those writes persist, so a request made
once is executed again by the next scheduled run.

Two properties turn that from local-process-only into browser-reachable: a
state-changing capture endpoint answers GET with no token, which any page can
fire with an image tag, and no trusted-host middleware is installed, so DNS
rebinding lets a remote page reach the local origin.

Separately, credentials can escape through exception strings. The Apify token
rides in a query string and the retry loop catches three exception types;
`http.client.InvalidURL`, which a token pasted with a stray character raises,
is not among them. It escapes to a bare `except Exception`, and its message
embeds the request path. From there the string is written to a summary file,
printed, and relayed over the unauthenticated event stream. The same shape
exists for the Telegram token in a URL path and for the Hunter key in a query
string. The trigger is a malformed environment value, which is the same class
of cause as the original outage.

Found by the `principal-review` board (spec 028), operability lens.

## Scope

**State-changing requests are not GETs**, and carry a token bound to the
local session. The capture path is the one that must keep working from a
bookmarklet, so its design is the constraint rather than an afterthought.

**Trusted hosts are enforced**, so a DNS-rebinding page cannot present itself
as the local origin.

**Config writes are validated against the shape their readers require**, not
merely as objects. The Apify count is bounded at the point of use as well as
at the point of write, because the stored value outlives the request.

**A provider credential never leaves this process in readable form.**
Apify, Telegram and Hunter all require the credential in the URL, so the
guarantee is not that it is absent from the request: it is that the URL is
redacted before it can reach a log, a summary, an exception message, or the
event stream. The redaction is applied at the boundary rather than at each
call site, because the call sites that leak are the ones nobody thought of.

**Exception text crossing the boundary is scrubbed**, so a new exception
type cannot become a new leak.

## Inputs, outputs, failure modes

- Inputs: HTTP requests from the local browser and the bookmarklet.
- Outputs: reads are unchanged for every caller. The capture path changes
  deliberately and visibly: the GET renders a confirmation page and the click
  posts (`tests/test_batch_and_capture.py::test_submitting_the_confirmation_form_adds_the_job`,
  `tests/test_api_exposure.py::test_the_bookmarklet_path_still_reaches_the_tracker`).
  Everything else refuses rather than changing shape.
- Failure mode this must not introduce: breaking the capture bookmarklet,
  which is a real daily path.
- Failure mode this must not introduce: a redaction that hides which board
  failed. The host and path are what makes an error useful; the credential is
  what must go.
- The bound on paid spend is defence in depth: validating the write does not
  help if a value stored before this change is executed after it, so the
  limit is applied where the run reads it.

## Acceptance criteria

Proven by services/api/tests/test_api_exposure.py:

| Criterion | Proof |
|---|---|
| a cross-origin page cannot trigger a run, capture, or config write | `test_a_state_changing_request_without_the_token_is_refused` (five routes), `test_a_wrong_token_is_refused` |
| a foreign Host header is refused | `test_a_request_with_a_foreign_host_is_refused`, `test_the_rebinding_check_applies_to_writes_too`, `test_a_local_host_is_allowed` |
| a stored count above the bound is clamped at use | `test_a_stored_count_above_the_bound_is_clamped`, written directly to the store |
| a credential is redacted in every sink | `test_a_credential_is_scrubbed_from_text` (five shapes), `test_every_event_is_scrubbed_at_the_choke_point`, `test_every_exception_sink_in_discovery_is_scrubbed` |
| an unexpected exception cannot carry a credential across | the scrub is at the boundary rather than the call site, asserted by the two sink tests above, which fail if a new relay is added without it |
| the bookmarklet capture path still works | `test_the_bookmarklet_path_still_reaches_the_tracker`, `test_the_ui_can_still_start_a_run` |
| nothing identifying is committed | `test_the_token_file_is_created_readable_only_by_its_owner` asserts the 0600 mode, `test_a_second_call_returns_the_token_already_in_circulation` the exclusive create, and `tests/test_classification_coverage.py::test_never_in_git_paths_are_gitignored` keeps the data directory out of git (ADR-008) |

The capture route needed a design rather than a rule, and the shape is worth
recording. A bookmarklet can only navigate a top-level GET: a plain
navigation from an HTTPS page to localhost is never blocked by
mixed-content policy, unlike fetch. That is why the route answered GET, and
also why any page could fire it with an image tag and write a tracker row
with no interaction.

So the GET now renders a confirmation page holding the captured fields and
the token, and a click posts them. The bookmarklet still works and takes one
visible step; an image tag renders a page nobody asked for and changes
nothing. A form post cannot set a header, so the token travels as a field: a
cross-origin page can post that form but cannot read the token, so it cannot
fill it.

The trusted-host middleware is the load-bearing half of the pair, not the
token. A page that rebinds its own hostname to 127.0.0.1 becomes same-origin
and can then read the token from `/session` like the app does. Rejecting the
Host header is what stops it reaching a route at all.

Review on PR #39 found three real gaps in the first pass, all the same
shape: I scrubbed the sinks I had just changed rather than the property. The
structured-event branch of the run stream reached the unauthenticated stream
unscrubbed, two logger calls in discovery handed the raw exception to the
log, and the board-error message carried the exception text past
`redact_url`. Both tests I wrote were scoped to the branches I had fixed, so
neither could have caught any of it.

Scrubbing now happens in `RunManager._append`, the single place an event
reaches the stream, so a future caller cannot forget. The tests are
behavioural rather than source scans: an earlier version matched literal
call-site text and broke on a wrapped line, which is the wrong tool for the
question.

The token file also needed creating with its mode rather than chmodded
afterwards, and a mutation of that passed until a test for the mode existed.

Honest limitation: none of this defends against a process already running as
the operator. It reads the token file the same way the app does. The threat
this closes is a web page in the operator's browser, which is the one the
board identified and the one that needs no compromise to reach.

- [x] a cross-origin page cannot trigger a run, a capture, or a config write
- [x] a request presenting a foreign Host header is refused
- [x] a config write whose shape its reader cannot use is rejected with a
      message naming the field
- [x] a stored discovery count above the bound is clamped at use, proven by a
      test that writes the value directly to the store
- [x] a provider URL containing a credential is redacted in logs, summaries,
      exception messages, and the event stream, with one test per sink
- [x] `InvalidURL` and any other exception raised by a malformed credential
      does not carry the credential across the boundary
- [x] the bookmarklet capture path still works, proven end to end
- [x] no credential, host name, or account identifier is written to a
      committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The allowlist, the object-only
validation, the GET capture route, the absent trusted-host middleware, and
the three credential-in-URL call sites are verifiable in the tree.

## Out of scope

User accounts, multi-tenancy, or remote exposure of the API. Encryption of
the credential store. Rate limiting.
