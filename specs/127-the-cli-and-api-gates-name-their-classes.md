---
spec: 127
title: The CLI and API gates name their classes
status: accepted
approved: yes
milestone: M10
depends: [103, 104, 106, 108, 109, 111, 112]
---

# Spec 127: The CLI and API gates name their classes

Split from spec 112 on 2026-10-11 (spec 112, Amendment (2026-10-11):
split, owner and nesting, decision 1). This is part C. It carries spec
112's Scope item 9 and spec 112's Amendment (2026-10-11): operator and run
classes, moved here whole. Spec 112 is the seam (A); spec 126 is the
domain's SQL (B); spec 128 is three review findings (D).

## Problem

On a Postgres URL the CLI runs `harrier store` and refuses every other
command with one text, `POSTGRES_NOT_YET`
(`services/api/src/harrier/connection.py:41-43`; the check is
`harrier_cli/main.py:3079-3082` on `main` at `87c3926`, 2026-10-11). The
text says "until spec 112", which is no longer the spec that lifts it,
and it gives one reason for refusals that have three:

- Some commands are local by nature. `backup`, `cutover` and the rest
  work on the SQLite file, the old system or launchd, and will never run
  on a hosted store.
- Some will run there, but only as something specific. Specs 108 and 111
  add operator commands that run only on Postgres, each as a declared
  database role. Spec 106 runs the verbs of a hosted run on Postgres, but
  only for one owner the API names through `HARRIER_RUN_OWNER`.
- The rest have no hosted form: ADR-013 decision 4 says the hosted
  deployment has no tenant CLI.

Spec 112 specified these classes in Scope item 9 and its Amendment
(2026-10-11): operator and run classes. Its analysis of 2026-10-11 found
that the amendment's classes point at commands that do not exist yet:
there is no `harrier operator` group in `harrier_cli/main.py`, and the
run class needs spec 106's owner connection. Akin decided on 2026-10-11
(spec 112's amendment, decision 4) that the classes wait for those
commands: until this spec lands, only `harrier store` runs on a Postgres
URL, run commands refuse even with `HARRIER_RUN_OWNER`, and no empty
`harrier operator` group ships.

The API's refusal (`harrier_api/app.py:1370-1371`) uses the same
`POSTGRES_NOT_YET`. Spec 104 replaces it with its own text before this
spec lands (spec 104, Failure modes, "Postgres without hosted mode").

## Scope

1. **Sequenced after specs 106, 108 and 111 build their commands.** This
   spec is implemented only when `harrier operator` has the four
   subcommands of specs 108 and 111, and spec 106's run binding sets
   `HARRIER_RUN_OWNER`. Until then spec 112's gate stands unchanged.
2. **One table of classes beside the parser** in
   `services/api/src/harrier_cli/main.py`. Every command and subcommand is
   in exactly one class (Behavior).
3. **The gate reads the table.** On a Postgres URL, `main` runs a command
   or refuses it with its class's text, before it opens anything, takes a
   lease or hands anything to the container.
4. **Operator subcommands declare their database role** beside the
   parser, where the walk reads it.
5. **`HARRIER_RUN_OWNER`** is handled as spec 106 states (Behavior), and a
   run command opens its store with spec 112's `open_store(owner=...)`.
6. **`POSTGRES_NOT_YET` goes.** No caller is left once the CLI uses the
   class texts and spec 104 has replaced the API's. The constant is
   removed from `harrier.connection`.
7. **Spec 109's local-only commands.** `gmail-watch` and `gmail-oauth`
   join the local-only class, as spec 109 asked of spec 112 (spec 109,
   Scope item 4).
8. **Docs.** `docs/architecture.md` names the classes and this spec's
   test.

## Behavior

**The classes.**

| Class | Commands | On a Postgres URL | On SQLite |
|---|---|---|---|
| store (spec 103) | `store` and its subcommands | runs | runs |
| operator (specs 108, 111) | `harrier operator` and its subcommands: `scheduler`, `schedule-report` (spec 108), `invite`, `erase` (spec 111) | runs; each subcommand runs as the database role it declares | refused, spec 108's text |
| run command, owner required (spec 106) | `discover`, `demo-run`, `digest`, `config check-feeds`, `reconsider`, `events backfill`, `evaluate-prospects` | runs only with `HARRIER_RUN_OWNER` naming an owner; otherwise refused, spec 106's text | runs, as today |
| local only | `backup`, `restore`, `verify-backup`, `doctor`, `cutover`, `migrate-legacy`, `profile import`, `config import`, `gmail-migrate-state`, `gmail-watch`, `gmail-oauth`, `schedule`, `parity` | refused, the local-only text | runs, as today |
| everything else | the rest | refused, the everything-else text | runs, as today |

The run class's list is spec 106's. A run verb whose kind later becomes
hosted (specs 107, 109) moves into it in that spec, and the walk below
fails until its class changes.

**The texts.** Exit 1 in every case, before anything opens.

- Local only: `error: harrier <command> is local only; it does not run
  while HARRIER_DATABASE_URL names a Postgres store`.
- Everything else: `error: HARRIER_DATABASE_URL names a Postgres store;
  harrier <command> does not run on it. A hosted store is used through
  the API, not the CLI (ADR-013 decision 4)`.
- Run command without an owner: `error: harrier <command> runs on a
  Postgres store only as a hosted run, which the API starts for one owner
  (spec 106)`.
- Operator on SQLite: `error: harrier operator <command> runs only
  against a hosted store; locally the schedule is launchd (harrier
  schedule)` (spec 108).

`<command>` is the command and subcommand words only, never an argument,
because arguments carry contact names and free text (spec 061).

**Operator role declarations.** Each `harrier operator` subcommand
declares, beside the parser, the database role it runs as, or that it
opens no database connection. Specs 108 and 111 state each one:
`scheduler` and `schedule-report` run as the `harrier_scheduler` login
(spec 108, "The CLI on each store"), `erase` runs as `service_role`, and
`invite` opens none (spec 111, decision D2). A subcommand added without a
declaration fails the walk. No other top-level command is an operator
command.

**`HARRIER_RUN_OWNER`.** When it is set, `main`, as spec 106 states:

- does not read `.env`;
- refuses a value that is not a UUID: `error: HARRIER_RUN_OWNER is not an
  owner id`, exit 1;
- refuses unless `HARRIER_DATABASE_URL` names a Postgres store: `error:
  HARRIER_RUN_OWNER is set but HARRIER_DATABASE_URL names no Postgres
  store`, exit 1;
- refuses any command outside the run class with that class's text;
- opens the store with `open_store(owner=...)` (spec 112).

**The API.** This spec does not change the API. By the time it lands,
spec 104 has replaced `POSTGRES_NOT_YET` in `app.py` with `error: the API
serves a Postgres store only with HARRIER_AUTH=supabase (spec 104)`. The
acceptance criteria check that nothing imports `POSTGRES_NOT_YET`.

## Failure modes

- **A command added without a class.** The walk fails, so `just check`
  fails, before the command can reach a Postgres URL.
- **An operator subcommand added without a role declaration.** The walk
  fails.
- **A run command on a Postgres URL without `HARRIER_RUN_OWNER`.** Refused
  with spec 106's text; nothing opens.
- **`HARRIER_RUN_OWNER` beside SQLite, or not a UUID.** Refused with
  spec 106's texts; nothing opens.
- **`harrier operator` on SQLite.** Refused with spec 108's text; nothing
  opens.
- **An argument holding a name or free text.** Never printed; the
  refusal names only command words.

## Acceptance criteria

- [ ] Walking the parser, every command and subcommand is in exactly one
      class of the table above, and a new command without a class fails
      the walk (planned test_every_command_has_a_postgres_refusal_class).
- [ ] Every `harrier operator` subcommand declares a database role or
      declares that it opens none, and a subcommand added without a
      declaration fails the walk; a run command on a Postgres URL without
      `HARRIER_RUN_OWNER` is refused with spec 106's text and opens
      nothing (planned
      test_the_postgres_gate_has_operator_and_run_classes).
- [ ] The local-only and everything-else texts are as under Behavior, and
      name the command words but no argument; spec 103's refusal tests in
      `services/api/tests/test_store_cli.py`, among them
      `services/api/tests/test_store_cli.py::test_a_data_command_is_refused_on_a_postgres_url`,
      are updated to the new texts.
- [ ] `gmail-watch` and `gmail-oauth` are refused on a Postgres URL with
      the local-only text.
- [ ] `POSTGRES_NOT_YET` is not defined in `harrier.connection`, and no
      module under `services/api/src` or `services/api/tests` imports it.
- [ ] `docs/architecture.md` names the classes and the walk test.
- [ ] `just check` passes.

## Honest limitations

- This spec lands last of the hosted CLI work. Until then the CLI's
  Postgres refusal still says "until spec 112" for every command but
  `harrier store`.
- A command's class is declared by hand. The walk proves every command
  has one, not that the one it has is right; that is review's.
- The texts name ADR-013 and specs by number, which a reader outside the
  repository cannot follow.

## Migration

None for a local user: on SQLite every command behaves as today. An
operator with a Postgres URL sees new refusal texts.

## Options weighed

- **The classes now, with an empty `harrier operator` group.** What spec
  112's amendment asked for. It ships a command group that does nothing
  and a run class that cannot be satisfied. Not chosen (spec 112,
  decision 4).
- **A class per command written at each command's handler.** Scatters the
  table; the walk could not find a command that forgot. Not chosen.
- **Lifting the gate for store-only commands as a development aid** (spec
  112, Open decision 8, alternative). Not chosen there, and not here.

## Open decisions for Akin

1. **Depends.** Akin named 106, 108 and 111. Recommended, and applied in
   the frontmatter: 104, 109 and 112 as well, because this spec relies on
   spec 104 having replaced the API's text, takes spec 109's local-only
   commands, and opens a run's store with spec 112's
   `open_store(owner=...)`. Specs 106, 108 and 111 already depend on 104
   and 112, so this adds no wait beyond 109. Alternative: 106, 108 and
   111 only, with the two gmail commands classed whether or not spec 109
   has landed.

## Proof / origin

- `specs/112-the-tracker-domain-runs-on-either-store.md`: Scope item 9,
  Behavior ("The CLI on a Postgres URL", "The API on a Postgres URL"),
  Open decision 8, the Amendment (2026-10-11): operator and run classes,
  and the Amendment (2026-10-11): split, owner and nesting, decision 4.
- `specs/106-tenant-credentials-and-run-isolation.md`, "The CLI on a
  Postgres URL": the run class, its commands and texts.
- `specs/108-hosted-scheduling.md`, "The CLI on each store", and decision
  D2: the operator group and its text on SQLite.
- `specs/111-sign-up-export-and-deletion.md`, Scope item 5 and decision
  D2: `invite` and `erase` and their roles.
- `specs/109-gmail-and-telegram-for-tenants.md`, Scope item 4:
  `gmail-watch` and `gmail-oauth` are local only.
- `specs/104-the-hosted-api-authenticates-every-request.md`, Failure
  modes: the API's own refusal text.
- `specs/061-one-kernel-owns-the-database.md`: refusals name the
  command, never its arguments.
- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decision 4: no
  tenant CLI.

## Out of scope

- The operator commands themselves: specs 108 and 111.
- The run binding and `HARRIER_RUN_OWNER`'s source: spec 106.
- The API's hosted mode and its refusals: spec 104.
- The seam and `open_store(owner=...)`: spec 112.
- The domain's SQL on Postgres: spec 126.
