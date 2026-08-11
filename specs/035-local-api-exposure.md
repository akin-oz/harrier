---
spec: 035
title: The local API is not an open door
status: accepted
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

**Secrets never travel in a URL.** Where a provider requires it, the URL is
redacted before it can reach a log, a summary, an exception message, or the
event stream, and the redaction is applied at the boundary rather than at
each call site.

**Exception text crossing the boundary is scrubbed**, so a new exception
type cannot become a new leak.

## Inputs, outputs, failure modes

- Inputs: HTTP requests from the local browser and the bookmarklet.
- Outputs: unchanged behaviour for legitimate callers, refusals for the rest.
- Failure mode this must not introduce: breaking the capture bookmarklet,
  which is a real daily path.
- Failure mode this must not introduce: a redaction that hides which board
  failed. The host and path are what makes an error useful; the credential is
  what must go.
- The bound on paid spend is defence in depth: validating the write does not
  help if a value stored before this change is executed after it, so the
  limit is applied where the run reads it.

## Acceptance criteria

Proving symbols are named at implementation, in
services/api/tests/test_api_exposure.py.

- [ ] a cross-origin page cannot trigger a run, a capture, or a config write
- [ ] a request presenting a foreign Host header is refused
- [ ] a config write whose shape its reader cannot use is rejected with a
      message naming the field
- [ ] a stored discovery count above the bound is clamped at use, proven by a
      test that writes the value directly to the store
- [ ] a provider URL containing a credential is redacted in logs, summaries,
      exception messages, and the event stream, with one test per sink
- [ ] `InvalidURL` and any other exception raised by a malformed credential
      does not carry the credential across the boundary
- [ ] the bookmarklet capture path still works, proven end to end
- [ ] no credential, host name, or account identifier is written to a
      committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The allowlist, the object-only
validation, the GET capture route, the absent trusted-host middleware, and
the three credential-in-URL call sites are verifiable in the tree.

## Out of scope

User accounts, multi-tenancy, or remote exposure of the API. Encryption of
the credential store. Rate limiting.
