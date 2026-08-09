"""Provider selection and model resolution (spec 012 port).

Stated change from the old client: no .env reading here. The CLI's
load_project_env owns environment loading; the domain reads os.environ.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_PROVIDER = "auto"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_CODEX_MODEL = ""
DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"

PROVIDER_ENV = "AI_PROVIDER"
MODEL_ENV = "AI_MODEL"
DEBUG_ENV = "AI_DEBUG"

PROVIDERS = ("codex-cli", "claude-cli", "openai-api", "anthropic-api")

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
    old client."""
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


def debug_enabled(provider: str) -> bool:
    env_names = {
        "codex-cli": "CODEX_CLI_DEBUG",
        "claude-cli": "CLAUDE_CLI_DEBUG",
        "openai-api": "OPENAI_DEBUG",
        "anthropic-api": "ANTHROPIC_DEBUG",
    }
    values = [os.getenv(DEBUG_ENV, ""), os.getenv(env_names[provider], "")]
    return any(value.strip().lower() in {"1", "true", "yes"} for value in values)
