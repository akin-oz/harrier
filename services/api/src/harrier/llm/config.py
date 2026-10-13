"""Provider selection and model resolution (spec 012 port).

Stated change from the old client: no .env reading here. The CLI's
load_project_env owns environment loading; the domain reads os.environ.

In a hosted run (`HARRIER_RUN_OWNER` set) only the API providers are
offered, and `auto` resolves among them by which key is set (spec 106).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from harrier.connection import in_a_hosted_run

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_PROVIDER = "auto"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_CODEX_MODEL = ""
DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"

PROVIDER_ENV = "AI_PROVIDER"
MODEL_ENV = "AI_MODEL"
DEBUG_ENV = "AI_DEBUG"

# Each turns on prompt logging (`harrier.llm.providers._log_call`): the
# shared one first, then one per provider, in the order `PROVIDERS` names
# them. The hosted API refuses to start with any of them on (spec 135).
PROVIDER_DEBUG_ENV = {
    "codex-cli": "CODEX_CLI_DEBUG",
    "claude-cli": "CLAUDE_CLI_DEBUG",
    "openai-api": "OPENAI_DEBUG",
    "anthropic-api": "ANTHROPIC_DEBUG",
}
DEBUG_VARIABLES = (DEBUG_ENV, *PROVIDER_DEBUG_ENV.values())

PROVIDERS = ("codex-cli", "claude-cli", "openai-api", "anthropic-api")

# The providers a hosted run may use, and its refusals (spec 106, Providers
# in a hosted run). ADR-013 decision 8 makes the CLI providers local only.
CLI_PROVIDERS = ("codex-cli", "claude-cli")
HOSTED_PROVIDERS = ("openai-api", "anthropic-api")
CLI_PROVIDERS_LOCAL_ONLY = "the CLI providers are local only (ADR-013 decision 8)"
NO_HOSTED_AI_CREDENTIAL = "no AI credential is set for this account"

CODEX_FALLBACK_LOCATIONS = (
    "~/.local/bin/codex",
    "~/.npm-global/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
)

CLAUDE_FALLBACK_LOCATIONS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "~/.npm-global/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
)


class LLMClientError(RuntimeError):
    pass


class LLMTransientError(LLMClientError):
    """A failure worth one retry: the same request may succeed if repeated.

    Providers raise this for timeouts, non-zero CLI exits, connection
    errors, and 429/5xx responses. Configuration failures (missing binary,
    missing key, unknown provider) stay plain LLMClientError so they fail
    immediately (spec 058).
    """


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str


def normalize_provider(provider: str | None) -> str:
    value = (provider or DEFAULT_PROVIDER).strip().lower().replace("_", "-")
    aliases = {
        "": "auto",
        "codex": "codex-cli",
        "codexcli": "codex-cli",
        "claude": "claude-cli",
        "claudecli": "claude-cli",
        "openai": "openai-api",
        "openaiapi": "openai-api",
        "anthropic": "anthropic-api",
        "anthropicapi": "anthropic-api",
    }
    value = aliases.get(value, value)
    if value != "auto" and value not in PROVIDERS:
        raise LLMClientError(
            f"unsupported AI provider {provider!r}; expected auto, codex-cli, "
            "claude-cli, openai-api, or anthropic-api"
        )
    return value


def default_model_for(provider: str) -> str:
    return {
        "openai-api": DEFAULT_OPENAI_MODEL,
        "codex-cli": DEFAULT_CODEX_MODEL,
        "anthropic-api": DEFAULT_ANTHROPIC_MODEL,
        "claude-cli": DEFAULT_CLAUDE_MODEL,
    }[provider]


def provider_model_env(provider: str) -> str:
    return {
        "codex-cli": "CODEX_CLI_MODEL",
        "claude-cli": "CLAUDE_CLI_MODEL",
        "openai-api": "OPENAI_MODEL",
        "anthropic-api": "ANTHROPIC_MODEL",
    }[provider]


def find_binary(name: str, path_env: str, fallbacks: tuple[str, ...]) -> str | None:
    paths: list[Path] = []
    override = os.getenv(path_env, "").strip()
    if override:
        paths.append(Path(override).expanduser())
    on_path = shutil.which(name)
    if on_path:
        paths.append(Path(on_path))
    home = os.getenv("HOME") or str(Path.home())
    for raw in fallbacks:
        paths.append(Path(home + raw[1:]) if raw.startswith("~") else Path(raw))
    for candidate in paths:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def resolve_auto_providers() -> list[str]:
    """Configured providers in fallback order for AI_PROVIDER=auto.

    codex-cli is the last resort when nothing is configured, matching the
    old client. In a hosted run, only the API providers whose key is set,
    and no binary is looked for; with neither key, LLMClientError (spec
    106)."""
    if in_a_hosted_run():
        hosted = [
            name
            for name, variable in zip(
                HOSTED_PROVIDERS, ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"), strict=True
            )
            if os.getenv(variable, "").strip()
        ]
        if not hosted:
            raise LLMClientError(NO_HOSTED_AI_CREDENTIAL)
        return hosted
    providers: list[str] = []
    if find_binary("codex", "CODEX_CLI_PATH", CODEX_FALLBACK_LOCATIONS):
        providers.append("codex-cli")
    if os.getenv("OPENAI_API_KEY", "").strip():
        providers.append("openai-api")
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        providers.append("anthropic-api")
    if find_binary("claude", "CLAUDE_CLI_PATH", CLAUDE_FALLBACK_LOCATIONS):
        providers.append("claude-cli")
    return providers or ["codex-cli"]


def load_config(*, provider: str | None = None, model: str | None = None) -> LLMConfig:
    selected_provider = normalize_provider(provider or os.getenv(PROVIDER_ENV))
    if in_a_hosted_run() and selected_provider in CLI_PROVIDERS:
        raise LLMClientError(CLI_PROVIDERS_LOCAL_ONLY)
    if selected_provider == "auto":
        selected_provider = resolve_auto_providers()[0]
    selected_model = (
        (model or "").strip()
        or os.getenv(MODEL_ENV, "").strip()
        or os.getenv(provider_model_env(selected_provider), "").strip()
        or default_model_for(selected_provider)
    )
    if selected_provider == "openai-api":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
    elif selected_provider == "anthropic-api":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    else:
        # CLI providers authenticate themselves; the old client stored the
        # provider name here and callers depend on it being non-empty.
        api_key = selected_provider
    return LLMConfig(provider=selected_provider, model=selected_model, api_key=api_key)


def debug_value_on(value: str) -> bool:
    """Whether a debug variable's value turns prompt logging on."""
    return value.strip().lower() in {"1", "true", "yes"}


def debug_enabled(provider: str) -> bool:
    values = [os.getenv(DEBUG_ENV, ""), os.getenv(PROVIDER_DEBUG_ENV[provider], "")]
    return any(debug_value_on(value) for value in values)
