---
spec: 106
title: Tenant credentials and run isolation
status: accepted
approved: yes
milestone: M10
depends: [104, 105]
---

# Spec 106: Tenant credentials and run isolation

## Problem

Credentials live in one `.env`, and a run's subprocess inherits the server's
whole environment (spec 035). Hosted, that would hand one tenant's run every
tenant's keys. ADR-013 decision 8 stores credentials per owner and gives
each run only its owner's. Akin chose bring-your-own LLM keys (spec 102,
open decision 3).

The post-merge review of PRs #194 and #207 (2026-10-10) added five
problems this spec owns:

- **A run is a CLI process, and ADR-013 says there is no tenant CLI.**
  Every run kind is a `harrier_cli.main` command
  (`services/api/src/harrier_api/runs.py:38`, `KIND_COMMANDS`), spawned by
  `asyncio.create_subprocess_exec` (`runs.py:611-615`). ADR-013 decision 4
  says "the hosted deployment has no tenant CLI", and spec 112 refuses
  every command but `harrier store` on a Postgres URL. This spec amends
  ADR-013 decision 4: a run is a CLI process bound by its parent, the API,
  to one owner. Spec 112's refusal classes gain a third, "run command,
  owner required". (architecture 1)
- **The trust model is unwritten.** A run process connects with the API's
  login role, which can set any claims it likes. Row-level policy catches
  a bug inside a run; it does not contain a compromised run, which can
  claim any owner. This spec states that limit. (architecture 1)
- **The child's environment is inherited.** `create_subprocess_exec` is
  called with no `env` (`runs.py:611-615`), and the CLI LLM providers copy
  `os.environ` again (`services/api/src/harrier/llm/providers.py:103`,
  `:157`). Hosted, the child's environment is built from an allowlist,
  never inherited, and excludes `HARRIER_DATABASE_URL` and every other
  server secret. (privacy 4, architecture 1)
- **The credential key has no home.** Credentials are encrypted at rest,
  and this spec names where the encryption key lives (Supabase Vault or a
  KMS) and decrypts an owner's credentials only at run start.
  (architecture 10)
- **Runs are per machine on Fly.** The run registry is an in-memory dict
  per process (`runs.py:479`). A stream request routed to another Fly
  machine answers 404, and Fly's auto-stop kills a machine's runs. Either
  one machine with auto-stop off, or `fly-replay` routing of run requests
  to the machine that holds the run, decided with spec 110.
  (architecture 8)

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Credentials
are stored per owner, encrypted at rest; a run records its owner and its
environment holds only that owner's credentials; hosted offers only API LLM
providers. A track id is no longer unique across owners on Postgres (spec
105), so every run key built from one, such as academic discovery's
`discovery:<track id>` (`services/api/src/harrier/academic/discovery.py:71`),
is qualified by owner hosted. ADR-013 decision 4 is amended so a run is a
CLI process bound to one owner, and spec 112's refusal table gains "run
command, owner required"; the trust model says row-level policy catches
a bug in a run and does not contain a compromised run; a hosted run's
environment comes from an allowlist
that excludes `HARRIER_DATABASE_URL` and server secrets; the encryption
key's location is named and credentials are decrypted only at run start;
run requests reach the machine that holds the run, or there is one
machine with auto-stop off (post-merge review of PRs #194 and #207,
2026-10-10).

## Proof / origin

ADR-013 decisions 4 and 8; spec 102 open decision 3; `.env.example`;
the post-merge review of PRs #194 and #207 (2026-10-10).

## Out of scope

Gmail and Telegram setup flows (spec 109). Operator-paid LLM calls.
