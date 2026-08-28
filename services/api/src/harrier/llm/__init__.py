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
import time

from harrier.llm.config import (
    DEFAULT_TIMEOUT_SECONDS,
    PROVIDER_ENV,
    LLMClientError,
    LLMConfig,
    LLMTransientError,
    load_config,
    normalize_provider,
    resolve_auto_providers,
)
from harrier.llm.providers import generate_with_config

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "LLMClientError",
    "LLMConfig",
    "LLMTransientError",
    "generate_text",
    "load_config",
    "normalize_provider",
    "resolve_auto_providers",
]

logger = logging.getLogger(__name__)

RETRY_WAIT_SECONDS = 2


def _generate_with_retry(
    system_prompt: str, user_input: str, config: LLMConfig, timeout: int
) -> str:
    """One attempt plus one retry on a transient failure (spec 058).

    A transient first attempt (LLMTransientError or an empty response) is
    repeated once after a short wait. The second attempt's outcome stands
    as-is: its exception propagates untouched, and an empty second response
    is returned for the caller's existing empty-response handling, so the
    error surfaces are byte-identical to the single-attempt behavior.
    Non-transient errors (missing binary, missing key) raise immediately.
    """
    try:
        output = generate_with_config(system_prompt, user_input, config, timeout)
        if output.strip():
            return output
        reason = "empty response"
    except LLMTransientError as exc:
        reason = str(exc)
    logger.warning(
        "%s attempt failed (%s); retrying once in %ss",
        config.provider,
        reason,
        RETRY_WAIT_SECONDS,
    )
    time.sleep(RETRY_WAIT_SECONDS)
    return generate_with_config(system_prompt, user_input, config, timeout)


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
                output = _generate_with_retry(system_prompt, user_input, config, timeout)
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
    output = _generate_with_retry(system_prompt, user_input, config, timeout)
    if not output.strip():
        raise LLMClientError(f"{config.provider} returned an empty response")
    return output
