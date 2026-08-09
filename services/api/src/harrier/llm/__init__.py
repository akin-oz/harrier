"""The LLM provider seam (spec 012).

Every LLM call in the codebase goes through generate_text. The provider
modules are internal: an import-linter contract forbids importing
harrier.llm.providers from anywhere else, which is what closes the old
repo's bypass class (evaluate_offer.py and tailor_resume.py constructed
their own OpenAI clients).
"""

from __future__ import annotations

import logging
import os

from harrier.llm.config import (
    DEFAULT_TIMEOUT_SECONDS,
    PROVIDER_ENV,
    LLMClientError,
    LLMConfig,
    load_config,
    normalize_provider,
    resolve_auto_providers,
)
from harrier.llm.providers import generate_with_config

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "LLMClientError",
    "LLMConfig",
    "generate_text",
    "load_config",
    "normalize_provider",
    "resolve_auto_providers",
]

logger = logging.getLogger(__name__)


def generate_text(
    system_prompt: str,
    user_input: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    requested_provider = normalize_provider(provider or os.getenv(PROVIDER_ENV))
    if requested_provider == "auto":
        errors: list[str] = []
        for candidate in resolve_auto_providers():
            try:
                config = load_config(provider=candidate, model=model)
                output = generate_with_config(system_prompt, user_input, config, timeout)
                if output.strip():
                    if errors:
                        logger.warning(
                            "auto provider fell back to %s after: %s",
                            config.provider,
                            "; ".join(errors),
                        )
                    return output
                errors.append(f"{candidate}: empty response")
            except LLMClientError as exc:
                errors.append(f"{candidate}: {exc}")
        raise LLMClientError("all auto AI providers failed: " + "; ".join(errors))

    config = load_config(provider=requested_provider, model=model)
    output = generate_with_config(system_prompt, user_input, config, timeout)
    if not output.strip():
        raise LLMClientError(f"{config.provider} returned an empty response")
    return output
