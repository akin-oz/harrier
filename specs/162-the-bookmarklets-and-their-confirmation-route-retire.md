---
spec: 162
title: The bookmarklets and their confirmation route retire
status: accepted
approved: yes
milestone: M10
depends: [119, 161]
---

# Spec 162: The bookmarklets and their confirmation route retire

## Problem

Once spec 161's extension and spec 119's capture page ship, the browser
has two capture paths, and the older one carries most of the code.

The bookmarklets exist only as untested one-line scripts in
`docs/bookmarklets.md`. Two API routes exist only for them. A bookmarklet
can only navigate, so `GET /capture/add` renders an HTML confirmation page
holding the captured fields and the local token, and `POST
/capture/add-form` takes the click as a form post with the token in a form
field (spec 035). The hosted API has to list both as not served
(`services/api/src/harrier_api/hostedauth.py`). Spec 035 records the
design as a constraint the bookmarklet imposed, not one anyone wanted.

On 2026-10-18 Akin chose to retire the bookmarklets rather than keep both.

## Scope

Remove the bookmarklets, the two routes that serve only them, and the
references that describe them. `POST /capture/add`, with JSON and the
token, stays: scripts, curl and spec 119's capture page use it.

## Behavior

- `docs/bookmarklets.md` is deleted. Where other docs pointed to it
  (`docs/architecture.md`, `docs/parity-matrix.md`,
  `docs/parity-checklist.md`), they point to docs/extension.md (spec 161)
  and say the bookmarklets were retired by this spec.
- `GET /capture/add` (`captureJobConfirm`) and `POST /capture/add-form`
  (`captureJobFromForm`) are removed from
  `services/api/src/harrier_api/capture_routes.py`, with the HTML result
  and confirmation page builders that only they use.
- A saved bookmarklet clicked after this change opens a tab to `GET
  /capture/add`, which now answers 405 Method Not Allowed, since `POST`
  remains on that path. `POST /capture/add-form` answers 404. Neither
  writes anything.
- `services/api/src/harrier_api/hostedauth.py` drops `/capture/add-form`
  from `NOT_SERVED_PATHS` and `("GET", "/capture/add")` from
  `NOT_SERVED_ROUTES`: neither route exists to be refused.
- The module docstring of `capture_routes.py` and the comment in
  `services/api/src/harrier_api/deps.py` stop describing the bookmarklet
  and describe the capture page and the JSON route.
- `packages/contract` is regenerated with `just contract`. The two
  operations disappear from `openapi.json` and `schema.d.ts`; nothing in
  `apps/web` used them.

## Failure modes

- **This lands before spec 119 and 161 ship.** The owner is left with no
  browser capture path. Spec 162 depends on both and is implemented only
  after both have merged.
- **A test still names a removed route.** The tests below are removed or
  rewritten in the same change; the suite fails otherwise.
- **The local web app still links to the confirmation page.** It does not:
  `apps/web` never referenced either operation (checked with `git grep` on
  `72066ab`). The contract drift check catches any that appear.

## Acceptance criteria

- [ ] `docs/bookmarklets.md` does not exist and `git grep -i bookmarklet`
      outside `specs/` finds only the retirement notes this spec adds
- [ ] `GET /capture/add` answers 405 and `POST /capture/add-form` answers
      404 on the local API, and neither writes a row
      (planned test_the_retired_capture_routes_write_nothing)
- [ ] `POST /capture/add` with the token still adds a job
      (`services/api/tests/test_batch_and_capture.py` keeps its JSON post
      tests unchanged)
- [ ] These tests, which exercise only the removed routes, are removed:
      `test_the_capture_get_changes_nothing`,
      `test_the_confirmation_page_carries_the_fields_and_the_token`,
      `test_the_confirmation_page_escapes_what_it_was_given`,
      `test_submitting_the_confirmation_form_adds_the_job`,
      `test_the_form_without_the_token_is_refused` in
      `services/api/tests/test_batch_and_capture.py`;
      `test_a_get_to_capture_changes_nothing`,
      `test_the_capture_form_requires_the_token`,
      `test_the_bookmarklet_path_still_reaches_the_tracker` in
      `services/api/tests/test_api_exposure.py`
- [ ] `test_every_scoped_route_declares_its_operation` in
      `services/api/tests/test_api_tracks.py` and
      `test_every_hosted_route_but_health_needs_a_token` in
      `services/api/tests/test_hosted_auth.py` no longer list
      `/capture/add-form`, and pass
- [ ] `just contract` leaves no diff after the regenerated files are
      committed
- [ ] `just check` passes

## Proof / origin

- `specs/035-local-api-exposure.md`: why the GET confirmation route and
  the form post exist.
- `specs/010-batch-imports-and-capture.md`: the bookmarklets' port.
- `specs/104-the-hosted-api-authenticates-every-request.md`: Open decision
  6, the not-served routes.
- `services/api/src/harrier_api/capture_routes.py`,
  `services/api/src/harrier_api/hostedauth.py`, read on `main` at
  `72066ab`.
- Akin's choice in the session of 2026-10-18 to retire the bookmarklets.

## Out of scope

- The extension (spec 161) and the capture page (spec 119).
- Any change to `POST /capture/add`, `CaptureIn` or `add_captured_job`.
- Removing the `python-multipart` dependency from
  `services/api/pyproject.toml`. Its comment says it is there for the
  confirmation page's form. Whether anything else still parses a form is
  checked, and the removal made, in its own change.
- A friendlier answer than 405 for an old bookmarklet. The owner is the
  only user and knows the bookmarklets were retired.

## Changes to approved specs

- **Spec 035.** Its acceptance table cites
  `test_the_bookmarklet_path_still_reaches_the_tracker` and
  `test_submitting_the_confirmation_form_adds_the_job` as proof that the
  capture path survives the token. Those routes are gone. The
  implementation change adds a note to spec 035 that the bookmarklet path
  was retired by spec 162 and that the token still guards `POST
  /capture/add` (`test_a_state_changing_request_without_the_token_is_refused`).
- **Spec 010.** Its Bookmarklets section is marked retired by spec 162.

## Migration

Delete the `+ Job` bookmarks from the browser toolbar. Use the extension
(spec 161) instead, pointed at the hosted web app, or at the local web app
address for local mode.
