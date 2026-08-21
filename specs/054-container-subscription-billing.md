---
spec: 054
title: Container AI runs bill the subscription, not the API key
status: accepted
approved: yes
milestone: M8
depends: [012, 051]
---

# Spec 054: Container AI runs bill the subscription, not the API key

## Problem

Every AI run started from the container is billed per token against the
Anthropic API key, and when that key's account has no credits, every
LLM-backed feature fails: "cover letter failed: AI request failed: claude
CLI exited 1: ... Credit balance is too low". The operator has a Max
subscription and wants container runs billed to it, exactly as host runs
are.

The billing split is deliberate, which is why this is a spec and not a
bug fix. Spec 051 set `CLAUDE_CLI_USE_API_KEY: "1"` in
`docker-compose.yml` on this recorded premise: the host's subscription
credential lives in the macOS Keychain, cannot cross into a Linux
container, so the container's CLI would have nothing to authenticate
with. The premise is incomplete. `claude setup-token` mints a long-lived
subscription OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`, `sk-ant-oat...`)
that is a plain environment value, crosses into the container through the
existing `env_file: .env` line, and authenticates the CLI against the
subscription. Verified on this machine: the container-shaped environment
(OAuth token present, API key withheld) succeeds and bills the
subscription; the compose-forced shape (API key present) reproduces the
credit-balance error verbatim.

The compose switch therefore forces the expensive path even when the
cheap one is available and already configured.

## Scope

- `docker-compose.yml`: the `environment:` block stops forcing
  `CLAUDE_CLI_USE_API_KEY`; its comment is rewritten.
- `.env.example`: documents `CLAUDE_CODE_OAUTH_TOKEN` and
  `CLAUDE_CLI_USE_API_KEY` with placeholder values.
- `services/api/tests/test_container.py`: the test pinning the forced
  switch inverts to pin its absence; a provider test pins that the OAuth
  token passes through to the CLI.

No change to `services/api/src/harrier/llm/providers.py`: the provider
already strips `ANTHROPIC_API_KEY` unless the switch is set, and already
passes `CLAUDE_CODE_OAUTH_TOKEN` through. The change is which
environment the compose file builds around that existing behavior.

## Behavior

- The compose file sets no `CLAUDE_CLI_USE_API_KEY`. The provider
  therefore strips `ANTHROPIC_API_KEY` from the CLI's environment in the
  container, the same as on the host, and the CLI authenticates with
  `CLAUDE_CODE_OAUTH_TOKEN` from `.env`. Container runs bill the
  subscription.
- API-key billing remains available as an explicit opt-in only: setting
  `CLAUDE_CLI_USE_API_KEY=1` in `.env` reaches the container through
  `env_file` and restores the old behavior. Nothing bills the API key
  silently; that is the property the provider's strip exists to protect,
  and it now holds in the container too.
- The compose comment states the new premise: the subscription credential
  that crosses into a container is the `claude setup-token` OAuth token,
  and the operator supplies it in `.env`.
- `.env.example` documents both variables: `CLAUDE_CODE_OAUTH_TOKEN`
  (from `claude setup-token`, used by the claude-cli provider) and
  `CLAUDE_CLI_USE_API_KEY` (explicit opt-in to per-token API billing).

This supersedes the cost-asymmetry clause of spec 051 ("AI runs started
from the container are billed per token"). Spec 051 is otherwise
untouched and its text stays as the record of what was decided then.

## Failure modes

- **No `CLAUDE_CODE_OAUTH_TOKEN` in `.env` and no opt-in switch**: the
  container's CLI has no credential and every LLM call fails with the
  CLI's own authentication error, surfaced through the existing
  `LLMClientError` path. Loud and immediate, not silent billing. The
  compose comment names the remedy: run `claude setup-token` on the
  host and put the token in `.env`, or opt into API billing explicitly.
- **Token revoked or expired**: the CLI returns an OAuth error
  (observed shape: "OAuth access token has been revoked"), surfaced the
  same way. Remedy is a fresh `claude setup-token`.
- **Both token and switch set**: the switch wins by the provider's
  existing logic (the API key stays in the environment, and the CLI
  prefers it). That is what "explicit opt-in" means; no new precedence
  rule is introduced.
- **Existing deployments**: a running container created from the old
  compose file keeps its forced switch until recreated; `docker compose
  up -d` after pulling the change recreates it with the new environment.

## Acceptance criteria

- `docker-compose.yml` carries no active `CLAUDE_CLI_USE_API_KEY`
  assignment, empty values included, since an `environment:` entry
  outranks `env_file` and even an empty one would shadow the operator's
  opt-in. The identifier may still appear in comments. The test that
  asserted the compose file forces the switch now asserts this absence
  (services/api/tests/test_container.py).
- A provider-level test proves `CLAUDE_CODE_OAUTH_TOKEN` set in the
  parent environment reaches the CLI child environment while
  `ANTHROPIC_API_KEY` is stripped: the container credential path exists
  end to end.
- `test_without_the_switch_the_key_is_still_withheld` continues to pass
  unchanged.
- `.env.example` names both variables; the classification of `.env`
  (never-in-git) is unchanged and no real token value appears anywhere
  in the diff.
- With `CLAUDE_CLI_USE_API_KEY=1` in the environment, the API key still
  reaches the CLI (existing provider behavior, still covered by a test).

## Proof / origin

Observed failure: a cover-letter request through the container returned
"Credit balance is too low" (api_error_status 400) from the CLI. Direct
reproduction on this machine, same CLI binary, three environment shapes:
OAuth token with API key withheld succeeds ("ok", billed to the
subscription); keychain-only fails with a revoked-token error (why the
interactive CLI also failed); API key present reproduces "Credit balance
is too low".

The forced switch is `docker-compose.yml` (environment block, commit
13aaa0c); the premise it rests on is recorded in spec 051 and in the
compose comment. The provider's strip-unless-switch logic is
`_generate_claude_cli` in `services/api/src/harrier/llm/providers.py`
(spec 012). The pinning tests are
`test_the_container_supplies_the_credential_the_cli_authenticates_with`
and `test_without_the_switch_the_key_is_still_withheld` in
`services/api/tests/test_container.py`.

## Out of scope

- Any change to the provider seam's logic or to the other three
  providers.
- Provisioning or refreshing the OAuth token automatically. Minting it
  is a browser consent flow that belongs on the host with the operator
  watching, the same reasoning spec 050 applied to Gmail OAuth.
- The host CLI's own login state (the revoked keychain token): that is
  operator-side, fixed by `claude /login`, and no repo behavior depends
  on it.
- Fallback chains (try subscription, then API key). Automatic fallback
  is exactly the silent billing the strip exists to prevent.

## Migration

Recreate the container after pulling: `docker compose up -d` (or
`just` recipe equivalent). Ensure `.env` carries a current
`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. Operators who want
per-token API billing set `CLAUDE_CLI_USE_API_KEY=1` in `.env`.
