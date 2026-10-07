---
spec: 084
title: Every name in the trusted-host list can match a request
status: proposed
approved: no
milestone: M8
depends: [035, 051, 083]
---

# Spec 084: Every name in the trusted-host list can match a request

## Problem

`TRUSTED_HOSTS` in `services/api/src/harrier_api/localauth.py` lists `[::1]`,
the IPv6 loopback address, and the comment above the server command in the
`Dockerfile` says `TrustedHostMiddleware` allows it. It does not. Starlette
1.5.0's middleware takes the host as the text before the first colon
(`starlette/middleware/trustedhost.py` in the installed package). For Host
`[::1]` or `[::1]:8000` that text is `[`, which matches nothing in the list,
so the answer is 400 "Invalid host header". Checked on 2026-10-07 against
`create_app()`: both forms answer 400.

Nobody meets that refusal today, because nothing listens on IPv6 loopback.
`just dev` and `just demo` start uvicorn without `--host` (`justfile`), so it
binds its default, 127.0.0.1 (`uvicorn/main.py` in the installed package).
The container binds 0.0.0.0, which is IPv4, and the compose file publishes
its port on `127.0.0.1` only (`docker-compose.yml`). A browser pointed at
`localhost` sends Host `localhost` whichever address it connects to, and that
matches.

What is wrong is the claim. The list names a host that can never match, the
`Dockerfile` comment repeats it, and no test runs it:
`services/api/tests/test_api_exposure.py::test_a_local_host_is_allowed`
covers `localhost`, `127.0.0.1` and `localhost:8000` only. The claim is also
a trap for the change that does listen on IPv6.
`services/api/tests/test_container.py::test_the_published_port_never_leaves_loopback`
permits a `[::1]` port mapping, and a container published that way would
answer `http://[::1]:8000` with 400 Invalid host header: a refusal that reads
like spec 035's rebinding defence firing, not like a gap in a list.

Spec 083 found this while reviewing the list for `testserver`, and its
decision 1 left it to this spec.

## Scope

- `services/api/src/harrier_api/localauth.py`: `TRUSTED_HOSTS` no longer
  holds `[::1]`. A comment beside it says why: the middleware cannot match a
  bracketed host, and nothing listens on IPv6 loopback.
- `Dockerfile`: the comment above `CMD` lists the names the middleware
  allows, `localhost`, `127.0.0.1` and `0.0.0.0`.
- `services/api/tests/test_api_exposure.py`: Host `[::1]` is refused, and
  every name in `TRUSTED_HOSTS`, bare and with a port, reaches a route.
- Spec 051's quote of the list, and spec 083's criterion that pins it, give
  the list without `[::1]`. Spec 083's criterion stays ticked, because it held
  when spec 083 shipped, and gains a sentence saying this spec removed
  `[::1]`.

This spec is implemented after spec 083, which edits the same list. This spec
is the only new file, and specs are public, so
`config/data-classification.json` does not change.

## Behavior

- Host `[::1]`, with or without a port, gets 400, as it does today. No
  response a client can observe changes.
- `TRUSTED_HOSTS` is `("localhost", "127.0.0.1", "0.0.0.0")`.
- Every name in the list, bare and with a port, reaches a route. A name the
  middleware cannot match fails the suite on the day it is added, so the list
  cannot again promise a host it refuses.

## Failure modes

- Someone adds `[::1]` back, expecting IPv6 loopback to work. The test over
  every listed name fails and names it, and the comment beside the list says
  why it is absent.
- A later change listens on IPv6, through a `[::1]` port mapping the compose
  test permits or through `--host ::1`. `http://[::1]:8000` answers 400, as it
  would today. `http://localhost:8000` works over either address family,
  because its Host header is `localhost`.
- A later Starlette parses bracketed hosts. Nothing starts passing by itself,
  because `[::1]` is no longer listed. Supporting IPv6 loopback would then be
  a one-entry change, with its own spec.
- Host `[::1]` sent to 127.0.0.1 by hand, for example with curl, gets 400
  before and after.

## Acceptance criteria

- [ ] `GET /health` with Host `[::1]`, and with Host `[::1]:8000`, answers
      400 (planned test_ipv6_loopback_is_not_a_trusted_host)
- [ ] `GET /health` with each name in `TRUSTED_HOSTS`, bare and with `:8000`,
      answers 200 (planned test_every_trusted_host_reaches_a_route)
- [ ] with `[::1]` put back into `TRUSTED_HOSTS`, that test fails and names
      `[::1]`, and the pull request records the run
- [ ] `TRUSTED_HOSTS` is `("localhost", "127.0.0.1", "0.0.0.0")`, with a
      comment saying why `[::1]` is absent
- [ ] the `Dockerfile` comment above `CMD` names `localhost`, `127.0.0.1` and
      `0.0.0.0` only
- [ ] specs 051 and 083 quote the list without `[::1]`
- [ ] `just check` passes

The planned tests go in `services/api/tests/test_api_exposure.py`.

## Honest limitations

- Nothing pins uvicorn's default. `just dev` and `just demo` rely on it
  binding 127.0.0.1 rather than passing `--host 127.0.0.1`. If the default
  became an IPv6 address, `http://[::1]:8000` would connect and answer 400.
- The tests drive the middleware through Starlette's test client. No test
  opens an IPv6 socket.
- This spec decides that IPv6 loopback is not supported. Using it would need a
  listener and a host parser, both out of scope (decision 1).

## Out of scope

- Supporting IPv6 loopback: listening on `::1`, publishing `[::1]:8000`, or
  parsing a bracketed Host header.
- The compose test's allowance of a `[::1]` port mapping. It is about where a
  port is published, which stays loopback either way, and `localhost` works
  over IPv6.
- `testserver` (spec 083), `0.0.0.0`, the token, and how the server binds.
- Upgrading Starlette.

## Decisions

Akin chose the recommended option, in session on 2026-10-07.

1. `[::1]` is removed rather than made to work. Making IPv6 loopback work
   would mean parsing the Host header ourselves, in a subclass of the
   middleware or a replacement, plus a listener on `::1`, for an address
   nothing uses today. A hand-written parser is new code on the path spec 035
   made load-bearing. Removing the entry changes no response anyone can
   observe.

## Migration

None. Nothing listens on IPv6 loopback, and Host `[::1]` gets 400 before and
after.

## Proof / origin

- `starlette/middleware/trustedhost.py` in the installed Starlette 1.5.0: the
  host is the text before the first colon, and a mismatch answers
  `PlainTextResponse("Invalid host header", status_code=400)`.
- `uvicorn/main.py` in the installed uvicorn: `--host` defaults to
  `127.0.0.1`.
- `justfile`, `Dockerfile` and `docker-compose.yml`: how the server starts and
  where its port is published.
- The probe on 2026-10-07 against `create_app()`.
- Spec 083, decision 1 (PR #154).
