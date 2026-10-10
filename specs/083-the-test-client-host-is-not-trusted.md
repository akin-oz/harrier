---
spec: 083
title: The API does not trust the test client's host name
status: accepted
approved: yes
approved-note: >
  Approved by Akin in session on 2026-10-07, verbally rather than by editing
  this file. Recorded here because the agent normally never sets this flag.
milestone: M8
depends: [035, 051, 060]
---

# Spec 083: The API does not trust the test client's host name

## Problem

Spec 035 made the trusted-host check the load-bearing defence against DNS
rebinding. A page that rebinds its own host name to 127.0.0.1 becomes
same-origin with the API, so it could read the token from `/session` the way
the app does. Its requests still carry its own name in the Host header, and
`TrustedHostMiddleware` refuses any name outside `TRUSTED_HOSTS`
(`services/api/src/harrier_api/localauth.py`) before a route runs
(`services/api/tests/test_api_exposure.py::test_a_request_with_a_foreign_host_is_refused`).

The check holds only while no trusted name is one an attacker can point at
their own server. Four of the five names hold that line: `localhost` is
reserved for loopback (RFC 6761), and `127.0.0.1`, `[::1]` and `0.0.0.0` are
IP literals, which no DNS answer resolves. The fifth, `testserver`, is an
ordinary single-label host name. It is in the list because Starlette's
`TestClient` sends it by default (`base_url="http://testserver"` in
`starlette/testclient.py`), and every API test builds its client that way.
Nothing outside the tests sends it: the container health check calls
`http://127.0.0.1:8000/health` (`docker-compose.yml`), the Vite dev proxy
forwards to `http://127.0.0.1:8000` (`apps/web/vite.config.ts`), and the
README and `justfile` send the browser to `http://127.0.0.1:8000`.

An operating system resolves `testserver` like any other name: its hosts file
first, then the DNS servers and search domains the current network supplies.
On a network whose DNS an attacker controls, such as a hostile Wi-Fi network,
a page the operator opens can load `http://testserver:8000` from the
attacker's server and then rebind the name to 127.0.0.1. From then on its
requests carry Host `testserver`, pass the check, and are same-origin. The
page can read the token and drive every state-changing route spec 035
protects: a run that inherits every credential, a billed Apify run, and
stored configuration that the next scheduled run executes.

Checked on 2026-10-07 against `create_app()` with Starlette 1.5.0:
`GET /health` answers 200 with Host `testserver` and with Host
`testserver:8000`, and 400 with Host `evil.example.com`.

Who it affects: the operator, while harrier runs on a machine that is on a
network whose DNS the operator does not control. That precondition is
narrower than spec 035's original finding, which needed only a page visit.
Once it holds, the reach is the same.

## Scope

- `services/api/src/harrier_api/localauth.py`: `TRUSTED_HOSTS` no longer
  holds `testserver`.
- `services/api/tests/conftest.py`: a test client built without a base URL
  reaches the app as `localhost`. The default is set once, beside spec 060's
  data-directory default, so the existing client construction sites do not
  change and a new test cannot forget it (decision 2).
- `services/api/tests/test_api_exposure.py`: the test client's old host is
  refused on a read and on a write, and every remaining trusted name except
  `[::1]` still passes (see Out of scope).
- Spec 051: its sentence quoting `TRUSTED_HOSTS` gives the new list.

This spec is the only new file, and specs are public, so
`config/data-classification.json` does not change.

## Behavior

- A request whose Host header names `testserver`, with or without a port,
  gets 400 before any route runs, the same answer as any other name outside
  the list. Reads and writes alike, with a token or without.
- `localhost`, `127.0.0.1` and `0.0.0.0`, with or without a port, pass the
  check as before.
- Nothing widens the list at run time: no environment variable, no
  `create_app` parameter, no entry kept for tests. A switch that widens it
  for tests would widen it in production too.
- The test suite reaches the app as `localhost`. A test that needs another
  host passes it explicitly, as the foreign-host tests already do.

## Failure modes

- A test client still on `testserver`. Every request it sends gets 400, so a
  test that asserts an exact status fails loudly. A test that asserts
  loosely, such as "not 200", could pass for the wrong reason. The suite has
  no such refusal assertion today. Its one inexact status check,
  `in (200, 201)` in `test_the_ui_can_still_start_a_run`, accepts success
  codes only, so a 400 fails it. The conftest default is what keeps a new
  test off the old host.
- Spec 060's leak marker goes away. Spec 060 knew a test had written to the
  operator's log because the log held lines naming `testserver`, and its
  landing check counted those lines by hand. After this change a test request
  names `localhost`, so that count no longer finds test traffic. The defence
  is the audit-hook guard spec 060 installed in
  `services/api/tests/conftest.py`, which fails any test that opens the
  operator's data directory. The count was evidence, not a gate.
- An operator who mapped `testserver` in their hosts file and browses to it
  gets 400. Nothing in the repository gives a browser that name.
- The container and the demo are reached as `127.0.0.1` or `localhost`, so
  neither changes.

## Acceptance criteria

- [x] `GET /health` with Host `testserver`, and with Host `testserver:8000`,
      answers 400
      (`services/api/tests/test_api_exposure.py::test_the_test_clients_host_is_refused`)
- [x] `POST /runs` with a valid token and Host `testserver` answers 400
      (`::test_the_test_clients_host_is_refused_on_a_write`)
- [x] `localhost`, `127.0.0.1`, `localhost:8000` and `0.0.0.0:8000` answer
      200 (`services/api/tests/test_api_exposure.py::test_a_local_host_is_allowed`,
      which gains `0.0.0.0:8000`)
- [x] a test client built with no base URL sends Host `localhost`
      (`::test_a_client_without_a_base_url_reaches_the_app_as_localhost`)
- [x] `TRUSTED_HOSTS` is `("localhost", "127.0.0.1", "0.0.0.0")`, and the
      diff adds no environment variable or parameter that changes it. When
      this spec shipped the list also held `[::1]`; spec 084 removed it.
- [x] no existing `TestClient(...)` call is edited, and the full suite passes
- [x] spec 051 quotes the new list
- [x] `just check` passes

The tests are in `services/api/tests/test_api_exposure.py`.

## Honest limitations

- The check is still by name. It stops a page whose origin is not a trusted
  name. It cannot stop a process on the machine that sets any Host header it
  likes, which spec 035 already says.
- `localhost` stays trusted on the strength of RFC 6761, which asks resolvers
  and browsers to answer it with loopback without asking the network. This
  spec tests no resolver.
- A browser may block this rebinding by itself, through DNS caching or rules
  on requests to private addresses. This repository neither relies on that nor
  tests it, which is why spec 035 made the Host check load-bearing.
- How often a network hands out DNS an attacker controls is not something
  this spec can measure. The change does not depend on it: no production
  caller sends the name, so refusing it costs nothing.

## Out of scope

- `[::1]`. It is in the list, but Starlette 1.5.0 takes the host as the text
  before the first colon, so Host `[::1]` and Host `[::1]:8000` both answer
  400 today (checked 2026-10-07). The Dockerfile comment says it is allowed.
  Removing the entry or making IPv6 loopback pass is its own spec
  (decision 1).
- `0.0.0.0` stays. It is an IP literal, so no DNS answer can make it a page's
  origin, and a cross-origin request to it still needs the token for any
  write (spec 035).
- The token, CORS, how the server binds, and browser-side defences.
- Spec 060's record. Its sentences about `testserver` describe what it
  observed and checked at landing, which stays true.

## Decisions

Akin chose the recommended option for both, in session on 2026-10-07.

1. `[::1]` gets its own spec rather than being folded into this one. Folding
   it in would mean choosing between removing the entry, which edits spec 051
   and the Dockerfile comment again, and working around Starlette's port
   parsing. Neither choice is about `testserver`.
2. The test default is set once in `services/api/tests/conftest.py`, the way
   spec 060 set the data directory, which means wrapping the default of
   Starlette's `TestClient` for the session. The alternative was passing
   `base_url="http://localhost"` at every construction site: no library
   wrapper, but every site changes, and a new test that forgets gets 400s
   rather than the default.

## Migration

None for the operator: no browser URL, health check, proxy or document uses
`testserver`. An operator who mapped `testserver` in their hosts file switches
to `localhost` or `127.0.0.1`.

## Proof / origin

- Spec 035: the trusted-host check is the load-bearing defence against
  rebinding, and `TrustedHostMiddleware` wraps the whole app
  (`services/api/src/harrier_api/app.py`).
- `services/api/src/harrier_api/localauth.py`: `TRUSTED_HOSTS`.
- Starlette's `TestClient` default, `base_url="http://testserver"`, in
  `starlette/testclient.py` of the installed package.
- The probe in Problem, run on 2026-10-07 against `create_app()`.
- First recorded as out of scope on PR #148 (spec 045).
