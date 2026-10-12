---
spec: 134
title: Each hosted request runs as its user, and the hosted API serves
status: proposed
approved: no
milestone: M10
depends: [103, 104, 105, 110, 112, 115, 132, 133]
---

# Spec 134: Each hosted request runs as its user, and the hosted API serves

Split from spec 104 on 2026-10-12 (spec 104, "Amendment (2026-10-12):
split into hosted mode, run ownership, store password and the request
transaction", decision 1). This is part D, the last of four. Spec 104 (A)
builds hosted mode and the bearer token with no database, and keeps the
spec 103 gate closed. Spec 132 (B) limits runs to their owner and refuses
starting one hosted. Spec 133 (C) keeps the store password out of logs and
HTTP errors. This spec opens each request's transaction as its user,
checks the login role and the store at start, and lifts the gate. It
lands after A, B and C, so the gate never opens on an API that serves
another owner's runs or prints the store's password.

This text is spec 104's Scope items 2, 5 and 6, the `psycopg-pool` half
of item 11, its Behavior sections "The request transaction", "The store
version" and "The login role" (less the environment check, which stayed
in A), the failure modes and criteria that went with them, its amendment
"request-role timeouts", and its amendment "migration 16's follow-ups".
It is restated here with Akin's decision 3 of the split and four gaps the
split's analysis found (Gaps the split found, below).

## Problem

After spec 104, a hosted request carries a verified token, and nothing
uses it below the middleware. `get_conn`
(`services/api/src/harrier_api/deps.py`) opens a SQLite connection per
request with no identity, and `create_app`
(`services/api/src/harrier_api/app.py`) still refuses a Postgres URL with
spec 103's text (`POSTGRES_NOT_YET`,
`services/api/src/harrier/connection.py`). That refusal is right while
nothing sets the request's identity: an API on Postgres without it would
serve every owner's rows to anyone with a token, or none of them.

Three things decide whether row-level security (spec 105) holds for a
request:

- the transaction's role is `harrier_tenant` and its claims are the
  verified token's, for every statement the request runs, and for none
  after it on the same pooled connection;
- the API's own login role cannot step past the policy, by attribute or by
  membership, since a statement in the request can `SET ROLE` to any role
  the login role is a member of (spec 104, "Amendment (2026-10-11):
  migration 16's follow-ups", Why);
- the store is at the version the code expects, so `harrier_owner_id()`
  and the policies exist as the code assumes.

A request that waits forever on another session's lock, or holds a lock
forever, stalls every owner (spec 105, Honest limitations; spec 104,
"Amendment (2026-10-11): request-role timeouts").

## Scope

1. **The spec 103 gate lifts here.** `create_app` on a Postgres URL starts
   only in complete hosted mode (spec 104, Configuration) and only after
   the start checks pass. `app.py` no longer imports `POSTGRES_NOT_YET`.
   The CLI's text is spec 112's and is unchanged.
2. **A connection pool.** `psycopg-pool` joins spec 104's `hosted`
   dependency group in `services/api/pyproject.toml`. The pool connects as
   the login role in `HARRIER_DATABASE_URL`, in autocommit mode, as the
   seam requires (spec 112), with `prepare_threshold` set to None.
3. **The request transaction** (Behavior): `get_conn` hosted opens one
   transaction per request through the seam, with the role, the claims,
   the read-back and three timeouts, at `READ COMMITTED`.
4. **The owner read-back is in `assume_identity`** and covers every owner
   transaction, requests and spec 106's runs alike (Akin's decision 3 of
   the split).
5. **Commit or roll back by the response's status,** in an ASGI middleware
   that sees the status before the client does (Behavior, "Commit and
   rollback").
6. **Start checks.** The store version, the login role and the store's
   default isolation, read once at start. The login role checks live in a
   new module, `services/api/src/harrier/loginrole.py`, that spec 110's
   `harrier store grant-tenant` and `harrier store check-deploy` call too.
7. **Documents.** `docs/architecture.md`'s hosted section says the hosted
   API serves, names the `harrier_api` login role, what it may be a
   member of, and how the operator creates it, and names this spec.

Classification: `services/api/src/harrier/loginrole.py` and the new test
files sit under `services/api/src/` and `services/api/tests/`, which
`config/data-classification.json` classifies as public. No new directory.

## Behavior

### The gate

| `HARRIER_AUTH` | `HARRIER_DATABASE_URL` | Start |
|---|---|---|
| unset or empty | unset or SQLite | the local API, unchanged |
| unset or empty | Postgres | refused: `error: the API serves a Postgres store only with HARRIER_AUTH=supabase (spec 104)`, exit 1 |
| `supabase` | SQLite or unset | refused with spec 104's text, unchanged |
| `supabase` | Postgres, configuration incomplete | refused with spec 104's texts, unchanged |
| `supabase` | Postgres, configuration complete | the start checks below; the API serves when they pass |

A start refusal is raised while the app is built, which uvicorn does on
import, so the process exits 1. Its text names the check, never the URL,
the password, a role's password or a claim, and it is raised without a
chained driver error, so a traceback prints none either.

### The request transaction

For a request that passed spec 104's middleware, `get_conn` hosted:

1. Refuses with 500 and runs nothing when the request carries no verified
   claims, so a route that reached it some other way opens nothing.
2. Takes a connection from the pool and wraps it as the seam's Postgres
   `Connection`, with the verified payload as its claims
   (`Connection.postgres(driver, claims=...)`,
   `services/api/src/harrier/connection.py`).
3. Opens the outermost `transaction()`. The seam begins it with `BEGIN`,
   then `SET LOCAL lock_timeout`, then `assume_identity`: `SET LOCAL ROLE
   harrier_tenant`, the claims set with `set_config('request.jwt.claims',
   <claims JSON>, true)`, and the read-back (below). The lock timeout is
   the request's chosen value, set on the `Connection` as
   `busy_timeout_ms` before the transaction opens.
4. Runs `SET LOCAL statement_timeout` and `SET LOCAL
   idle_in_transaction_session_timeout` with the request's chosen values.
   The three values are chosen by the implementation, from what the routes
   need, and the change that sets them says, for each, why that value. No
   number is fixed here, and none is `0`.
5. Yields the `Connection` to the route. Domain code that opens its own
   `transaction()` gets a savepoint inside this one (spec 112, decision 3),
   so the role, the claims and the timeouts hold for every statement the
   request runs.
6. Leaves the end of the transaction to the middleware (Commit and
   rollback).

`SET LOCAL` and `set_config(..., true)` end with the transaction, so the
next request on that connection starts as the login role with no claims
and with its own timeouts, whatever an earlier request set. A plain `SET`
made inside a request outlives it on the connection, which is why all
three timeouts are `SET LOCAL` in every request and never on the login
role (spec 104, "Amendment (2026-10-11): request-role timeouts").

The request transaction never sets an isolation level. It runs at the
store's default, which the start checks require to be `READ COMMITTED`
(The start checks). Two track inserts for one owner rely on it: spec
105's numbering trigger locks the owner's track 1, and the second
insert's next statement then sees the first's track. Under `REPEATABLE
READ` or `SERIALIZABLE` the second insert fails closed rather than
writing a duplicate (spec 104, "Amendment (2026-10-11): request-role
timeouts").

The claims set are the whole verified payload. Spec 105's policy and
defaults read the owner from them through `public.harrier_owner_id()`. No
harrier statement calls `auth.uid()` or `auth.jwt()`, and as
`harrier_tenant` neither can be called on Supabase (spec 105, migration
16).

Hosted mode creates no table, grants no privilege and adds no migration.

### The read-back, in every owner transaction

`assume_identity` reads back, in one statement and in the same
transaction, `current_user`, `current_setting('request.jwt.claims',
true)::jsonb ->> 'sub'` and `public.harrier_owner_id()::text`. It goes on
only when `current_user` is `harrier_tenant` and both the claims' `sub`
and `harrier_owner_id()` equal the `sub` it set. Otherwise it raises the
seam's `StoreDatabaseError` with `IDENTITY_DID_NOT_HOLD`, as it does
today, and the error names which of the three differed (`role`,
`claims` or `owner`) and carries none of their values. If the read-back
statement raises, for instance `42501 permission denied for function
harrier_owner_id` on a store where the tenant lost `EXECUTE`, the same
error is raised, naming the read-back.

`assume_identity` is the seam's, called at the start of every outermost
transaction of a `Connection` opened with claims
(`services/api/src/harrier/connection.py`). So the owner check covers
requests and spec 106's runs alike (Akin's decision 3 of the split). A
request whose read-back fails is answered 500 with a generic detail,
`the request could not run as its user`; the API logs the name of the
value that differed, with the request's method and path, and nothing
else; no domain statement runs; the transaction rolls back.

The function exists whenever an owner transaction runs: the API refuses
to start on a store that is not at `target_version()`
(`services/api/src/harrier/pgstore.py`), which is 16 or later, and
`open_postgres_store` checks the version for every other opener.

### Commit and rollback

An ASGI middleware, installed hosted only and inside spec 104's
authentication middleware, ends the request transaction:

- When the route's response start message (`http.response.start`) passes
  it, and the request opened a transaction, it commits when the status is
  below 400 and rolls back otherwise, and only then forwards the message.
  If the commit raises, it sends a 500 with the generic detail instead of
  the route's response, and the client never sees a success the store did
  not keep.
- When the request ends with an exception and no response start was
  sent, it rolls back.
- In every case it returns the connection to the pool, and a connection
  that is closed, broken or not idle is discarded rather than handed out
  again. `idle_in_transaction_session_timeout` ends the connection, not
  only the transaction, which is why this matters.
- After the commit or rollback, the request's `Connection` refuses every
  further statement, so a response body that read the store after its
  headers would fail rather than run outside the transaction. No route
  does that today: the one streaming route,
  `GET /runs/{run_id}/events`, reads the run manager, not the store.

Why a middleware and not `get_conn`'s own exit: FastAPI, as installed
(0.141.1), runs the exit code of a request-scoped dependency with `yield`
after `await response(...)` has sent the response
(`fastapi/routing.py`, `request_response`). A commit there would come
after the client saw the status, so a commit that failed would leave a
200 for a write the store did not keep. And the dependency sees only
exceptions, not the status: `POST /capture/add` answers 400 and 409 by
returning a `JSONResponse` with that status, without raising
(`services/api/src/harrier_api/capture_routes.py`, `_STATUS_CODES`), and
its transaction would commit. The middleware sees the status, and sees it
before the client does. Checked in the installed packages on 2026-10-12.

### The start checks

At start, hosted, after spec 104's configuration checks and the first key
fetch, `create_app` takes one connection from the pool and, in one
transaction:

1. Runs the login role checks (below) as the login role.
2. Reads `current_setting('default_transaction_isolation')` and refuses
   unless it is `read committed`.
3. Runs `SET LOCAL ROLE harrier_tenant`, which is also the "can set the
   tenant role" check, and reads the store's version as `harrier_tenant`
   (the login role holds no grant of its own; spec 105 grants `SELECT` on
   `schema_version` to `harrier_tenant`). It refuses when the version is
   behind or ahead of `target_version()`, with
   `check_postgres_version`'s texts (`pgstore.py`).

The transaction rolls back; the checks write nothing. Requests do not
check the version again: `get_conn` takes a pooled connection and never
calls `open_postgres_store`, whose `postgres_version` runs catalog queries
per open. A refusal at start is what enforces spec 110's "migrations
apply before a new API version serves": an image newer than the store
does not serve.

### The login role

The API connects as a dedicated login role, called `harrier_api` in the
documents, that is `NOINHERIT` and a member of `harrier_tenant` and of
nothing else. Migration 10 creates `harrier_tenant` (spec 105); the
operator creates `harrier_api` and grants it, with spec 110's `harrier
store grant-tenant`.

`services/api/src/harrier/loginrole.py` holds one function that takes a
Postgres connection and the login role's name and returns every check the
role fails, each naming the check and, for a membership, the role
reached. It returns names, never a URL or a password. Start refuses when
it returns anything. Spec 110's `grant-tenant` and `check-deploy` call the
same function, so the deploy refuses the same login roles. The checks:

- the login role is a superuser or has `BYPASSRLS`;
- the login role is a member, directly or through other roles, of any role
  that is a superuser, has `BYPASSRLS`, owns a table in `public`, owns a
  table with row security enabled in any other schema, or is
  `service_role` or `postgres`. Membership is read with
  `pg_has_role(<login role>, <role>, 'MEMBER')`, which counts a grant
  whatever its `INHERIT` and `SET` options, so a grant without `SET` today
  is still refused. `harrier_tenant` passes: migration 13 makes it
  `NOBYPASSRLS` and refuses it membership of any role, and it owns no
  table;
- the login role owns any table in the `public` schema (an owner bypasses
  row-level security unless it is forced; spec 105 forces it, and this
  check does not rely on that);
- the login role cannot `SET ROLE harrier_tenant`.

The module sits under `harrier`, not `harrier_api`, because the CLI calls
it and `harrier_api` is the API's own package; the import contracts
(`uv run lint-imports`) keep `harrier` from importing either.

### Gaps the split found

- **The owner read-back's home (decision 3).** Spec 104 put the
  `harrier_owner_id()` read-back in the request transaction's step 3.
  `assume_identity` is shared with spec 106's run connections. Akin
  decided on 2026-10-12 that it lives there and covers runs too (Scope
  item 4).
- **How step 5 sees the status.** Spec 104 said the request commits below
  400 and rolls back otherwise, and did not say how. The answer is the
  middleware above, and why.
- **A pool-level proof that a refused request takes nothing.** Spec 104's
  criterion for a request without a token named the pool, and spec 104
  (A) has no pool. A proves `get_conn` is never entered; this spec proves
  the pool hands out nothing.
- **The isolation level was assumed, not checked.** The seam begins with a
  plain `BEGIN` (`connection.py`, `_PostgresBackend.begin`), which runs at
  the store's default isolation. A store or role whose default is another
  level would break the track numbering guarantee silently. The start
  check above refuses it.

## Failure modes

- **Postgres without hosted mode.** Start refused with this spec's text,
  exit 1.
- **Login role too strong.** Any login role check fails: start refused
  naming the check and the role reached, never the URL or password,
  exit 1.
- **Store behind or ahead of the code.** Start refused with
  `check_postgres_version`'s text, exit 1. A deploy that serves before it
  migrates fails to start rather than failing per request.
- **The store's default isolation is not `read committed`.** Start
  refused naming the setting, exit 1.
- **The database is unreachable at start.** Start refused naming the
  check it could not run, never the URL, exit 1.
- **A route reaches `get_conn` without claims.** 500, nothing taken from
  the pool.
- **Pooler or connection drops `SET LOCAL`.** Autocommit outside the
  seam's transaction, a transaction-mode pooler that is not pinning the
  transaction, or a wrapper that commits between statements: the
  read-back differs, the request is answered 500, and no domain statement
  runs. Spec 115 found that Supabase's poolers keep `SET LOCAL` inside a
  transaction (its A5).
- **The policy's owner differs from the claims.** `harrier_owner_id()`
  returns null or another UUID, or raises: 500, no domain statement runs.
  A run's connection (spec 106) fails its transaction the same way.
- **A request's transaction runs as the table owner or as
  `service_role`.** Row security does not apply to a role with
  `BYPASSRLS` (spec 105,
  `services/api/tests/test_owner_policy.py::test_a_bypassrls_role_sees_every_owner`).
  Start refuses a login role that can reach one, and the read-back answers
  500 when `current_user` is not `harrier_tenant`.
- **A route returns a 4xx without raising.** Rolled back, by status.
- **The commit fails.** 500 instead of the route's response.
- **A lock wait passes `lock_timeout`, or a statement passes
  `statement_timeout`.** The database cancels it, the route raises, and
  the transaction rolls back: 500.
- **A request sits idle in its transaction past
  `idle_in_transaction_session_timeout`.** The server ends the connection;
  the pool discards it.
- **The pool is exhausted.** The pool's wait times out, the route raises,
  and the request is answered 500 (Open decision 1).
- **Token for a deleted or signed-out user.** The request runs as a `sub`
  that owns no rows: reads return nothing, and a write fails on the
  `owner_id` foreign key (spec 104, Failure modes).

## Acceptance criteria

The gate and the start checks:

- [ ] With `HARRIER_AUTH` unset and a Postgres `HARRIER_DATABASE_URL`, the
      API exits 1 with this spec's text; with complete hosted mode, a
      store at `target_version()` and a login role that passes, it starts;
      `app.py` holds no `POSTGRES_NOT_YET`
      (planned test_the_api_serves_postgres_only_in_hosted_mode).
- [ ] A login role that is a superuser, has `BYPASSRLS`, is a member of
      `service_role`, cannot `SET ROLE harrier_tenant`, or owns a table in
      `public` is refused at start, one case each, and no refusal prints
      the URL or the password
      (planned test_hosted_start_refuses_a_login_role_that_can_bypass_policy).
- [ ] A login role is refused at start, one case each, when it is a
      member, through a `NOINHERIT` intermediate role, of a `BYPASSRLS`
      role, of a superuser role, of a role without `BYPASSRLS` that owns a
      table in `public`, and of `service_role`; a direct membership granted
      `WITH SET FALSE` is refused too; and a login role that is a member
      of `harrier_tenant` alone starts
      (planned test_hosted_start_refuses_a_login_role_that_can_set_role_past_the_policy).
- [ ] The function in `services/api/src/harrier/loginrole.py` returns the
      same check names for each case above when called directly, and
      returns nothing for a login role that passes
      (planned test_the_login_role_checks_name_each_failure).
- [ ] A store one version behind `target_version()`, and one ahead, are
      each refused at start with `check_postgres_version`'s text, exit 1;
      a store at the target version starts, and a request after start
      runs no statement against `schema_version`
      (planned test_hosted_start_checks_the_store_version_once).
- [ ] A login role whose `default_transaction_isolation` is `repeatable
      read` is refused at start
      (planned test_a_store_defaulting_to_another_isolation_is_refused).

The request transaction (Postgres tests, run in CI as spec 103 requires):

- [ ] Inside a request, `current_user` is `harrier_tenant`,
      `public.harrier_owner_id()` is the token's `sub`, and
      `current_setting('request.jwt.claims', true)::jsonb` equals its
      verified payload (planned test_the_request_transaction_runs_as_its_user).
- [ ] Inside a request, `current_user` is `harrier_tenant` and its
      `rolbypassrls` in `pg_roles` is false
      (planned test_a_request_never_runs_as_a_bypassrls_role).
- [ ] After the request, the same pooled connection reports the login role
      and no claims (planned test_identity_ends_with_the_request).
- [ ] A connection in autocommit mode is detected by the read-back: 500,
      and a probe statement placed after it never runs
      (planned test_a_lost_set_local_stops_the_request).
- [ ] Two concurrent requests with different tokens each see only their
      own `sub` (planned test_concurrent_requests_keep_their_identities).
- [ ] A route that raises rolls its writes back; a 4xx response rolls
      back, including one a route returns without raising, such as
      `POST /capture/add`'s 409; and a commit that fails answers 500, not
      the route's 200
      (planned test_a_refused_request_commits_nothing).
- [ ] A request without a token, an expired one and a forged one each
      leave the pool's count of connections handed out unchanged
      (planned test_a_refused_token_takes_nothing_from_the_pool).
- [ ] A request that reaches `get_conn` without verified claims is answered
      500 and takes nothing from the pool
      (planned test_a_request_without_claims_opens_nothing).

The owner read-back:

- [ ] On a throwaway store whose `harrier_owner_id()` is replaced to
      return a fixed other UUID, a request is answered 500 and a probe
      statement placed after the read-back never runs; the same holds with
      the function returning null, and with `EXECUTE` on it revoked from
      `harrier_tenant`; and the log record names the value that differed
      and holds no claim value
      (planned test_the_read_back_checks_the_owner_the_policy_reads).
- [ ] On the same three stores, a `Connection` opened by `open_store(owner=...)`
      raises `StoreDatabaseError` with `IDENTITY_DID_NOT_HOLD` at its
      first transaction, before any domain statement runs
      (planned test_an_owner_connection_checks_the_owner_the_policy_reads).

Timeouts and isolation:

- [ ] Inside a request, `current_setting('lock_timeout')`,
      `current_setting('statement_timeout')` and
      `current_setting('idle_in_transaction_session_timeout')` are each the
      value the implementation chose, none of them `0`; a request that
      waits on a table another session holds in `ACCESS EXCLUSIVE` mode
      fails once its `lock_timeout` passes, rather than waiting for that
      session, and commits nothing; and after a request that ran `SET
      lock_timeout = 0`, `SET statement_timeout = 0` and `SET
      idle_in_transaction_session_timeout = 0` without `LOCAL`, the next
      request on the same pooled connection sees the chosen values again
      (planned test_the_request_role_has_lock_and_statement_timeouts).
- [ ] A connection the server ended for idling in its transaction is not
      handed to the next request
      (planned test_a_connection_ended_by_the_idle_timeout_is_not_reused).
- [ ] Inside a request, `current_setting('transaction_isolation')` is
      `read committed`, and two concurrent track inserts for one owner
      through the request transaction get two ids
      (planned test_the_request_transaction_reads_committed).
- [ ] The change that sets the three timeout values says, for each, why
      that value.

Gates:

- [ ] With `HARRIER_AUTH` and `HARRIER_DATABASE_URL` unset, every existing
      test passes unchanged, and `just demo` starts as before.
- [ ] Each new behavior has a test that fails when the code it proves is
      removed, and the pull request records each mutation and its result.
- [ ] The tests create the `harrier_api` login role, the Supabase roles and
      their memberships in throwaway databases only, and the roles and
      databases of the test server are the same before and after the run.
- [ ] `uv run ruff check`, `uv run pyright` and `uv run lint-imports`
      pass, and `just check` passes.

## Honest limitations

- The CI Postgres is plain `postgres:17`, not Supabase. The tests create
  the `authenticated`, `anon` and `service_role` roles, and migrations
  create `harrier_tenant` and `harrier_owner_id()`. Spec 115 compared the
  shape with a real Supabase project; drift after that is not seen in CI.
- **`RESET ROLE` still works.** SQL injected into a request can return to
  the login role, which holds no grant of its own and reads nothing. It
  can also set `harrier_tenant` again with claims of its choosing; that is
  spec 105's limitation and is unchanged here.
- **The login role check reads memberships and role attributes, not every
  path past a policy.** A `SECURITY DEFINER` function owned by a bypassing
  role and executable by the login role or by `PUBLIC` is another way
  past. Spec 105's catalog check covers such functions in `public`; this
  check reads no other schema's functions.
- **Read at start.** A membership or grant made while the API runs is not
  seen until the next start. Spec 110's deploy reads it again at each
  deploy.
- **Any role may `SET` the three timeouts.** SQL injected into a request
  can raise its own `statement_timeout` and
  `idle_in_transaction_session_timeout` and hold a table lock longer within
  that request. What bounds the stall for every other owner is each other
  request's own `lock_timeout`, which the injected session cannot change.
- **The isolation check reads the default once.** A session-level `SET
  default_transaction_isolation` made by SQL injected into a request stays
  on that pooled connection after it; the next request on it runs at that
  level. It fails closed (spec 104, "Amendment (2026-10-11): request-role
  timeouts"), and spec 105's limitations already cover injected SQL.
- **The middleware owns the end of the transaction.** A route that streams
  its body from the store after its headers would fail. None does today;
  a new one is a new change.
- After this spec the hosted API serves the tracker's routes under spec
  105's policy. Runs are refused until spec 106 (spec 132), file-backed
  routes read the image until spec 107, and the browser cannot sign in
  until spec 114.

## Migration

None for the local product: no variable it reads changes meaning, and no
file moves. No hosted install exists. Before the first hosted start the
operator creates the `harrier_api` login role and grants it
`harrier_tenant` (spec 110), and migrates the store to `target_version()`.

## Changes to approved specs

None is written into another spec here; each is for Akin to approve with
this spec.

- **Spec 106.** Its run connections, opened by `open_store(owner=...)`,
  now also check that `harrier_owner_id()` equals the run's owner at the
  start of every transaction (Akin's decision 3 of the split). A run on a
  store whose function disagrees fails its first transaction with
  `IDENTITY_DID_NOT_HOLD`. Spec 106 should say so where it describes the
  run's database session.
- **Spec 110.** Its `harrier store grant-tenant` and `check-deploy` call
  "the same function as spec 104's start checks". That function is in
  `services/api/src/harrier/loginrole.py`, specified here.

## Options weighed

- **Commit in `get_conn`'s exit.** Runs after the response is sent and
  sees no status (Commit and rollback). Not chosen.
- **The seam sets role and claims at every transaction instead of one
  request transaction.** Spec 104's Open decision 3 alternative; it costs
  a round trip per domain transaction. Not chosen (spec 104, Open
  decision 3); domain transactions are savepoints.
- **The owner read-back only on the request path.** Leaves spec 106's
  runs policed by an owner nothing checks. Not chosen (decision 3).
- **Set the isolation level in every transaction.** Would need a change
  to the seam's `BEGIN` for every caller, local SQLite excepted. A start
  check states the assumption and costs nothing per request. Not chosen.

## Open decisions for Akin

1. **The pool is exhausted.** Recommended: the request fails as any
   exception does, 500, and the pool's size and wait are chosen and
   justified with the timeouts. Alternative: a 503 with a declared body,
   which changes the contract on every route that opens the store.

## Proof / origin

- `specs/104-the-hosted-api-authenticates-every-request.md`: Scope items
  2, 5, 6 and 11; Behavior "The request transaction", "The store version"
  and "The login role"; "Amendment (2026-10-11): request-role timeouts";
  "Amendment (2026-10-11): migration 16's follow-ups"; and "Amendment
  (2026-10-12): split into hosted mode, run ownership, store password and
  the request transaction".
- `specs/105-every-personal-table-is-owned-and-policed.md`: the policy,
  `harrier_tenant`, migration 16's `harrier_owner_id()`.
- `specs/112-the-tracker-domain-runs-on-either-store.md`, "Amendment
  (2026-10-11): split, owner and nesting": `open_store(owner=...)`, the
  savepoint, `assume_identity`.
- `specs/115-spec-105-holds-on-a-real-supabase-project.md`: A5, the
  pooler keeps `SET LOCAL` and accepts a custom login role.
- `specs/110-deploy-pipeline-and-environments.md`: `grant-tenant`,
  `check-deploy`, migrations before a deploy serves.
- `services/api/src/harrier/connection.py` (`assume_identity`,
  `Connection.postgres`, `_PostgresBackend.begin`, `open_store`,
  `POSTGRES_NOT_YET`), `services/api/src/harrier/pgstore.py`
  (`target_version`, `check_postgres_version`, `open_postgres_store`),
  `services/api/src/harrier_api/deps.py` (`get_conn`),
  `services/api/src/harrier_api/app.py` (`create_app`),
  `services/api/src/harrier_api/capture_routes.py` (`_STATUS_CODES`).
- FastAPI 0.141.1, as installed: `fastapi/routing.py`,
  `request_response`.
- PostgreSQL, `set_config(name, value, is_local)`, `SET LOCAL`,
  `pg_has_role`, `default_transaction_isolation`,
  `idle_in_transaction_session_timeout`.
  https://www.postgresql.org/docs/current/functions-admin.html
- psycopg 3, prepared statements, and `psycopg_pool`.
  https://www.psycopg.org/psycopg3/docs/advanced/prepare.html

## Out of scope

- Hosted mode, the token, the middleware, CORS, hosts and the contract:
  spec 104.
- Run ownership and the 501: spec 132. A run's own credentials and
  database identity: spec 106.
- The store password in logs and HTTP errors: spec 133.
- Creating the `harrier_api` role, Fly secrets and health checks, and
  running migrations before a deploy serves: spec 110.
- Identity values in hosted logs, rate limiting, and audit logging of
  successful requests.
