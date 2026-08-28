"""The four provider backends (spec 012 port). Internal to harrier.llm:
an import-linter contract forbids importing this module from anywhere
else; call harrier.llm.generate_text instead.

Stated changes from the old client: both API providers use urllib from
the stdlib (the old client used the openai package and requests), and
debug logs live under the data directory (never-in-git) instead of a
repo-level logs/ directory. API keys are never written to logs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harrier.db import data_dir
from harrier.llm.config import (
    CLAUDE_FALLBACK_LOCATIONS,
    CODEX_FALLBACK_LOCATIONS,
    LLMClientError,
    LLMConfig,
    LLMTransientError,
    debug_enabled,
    find_binary,
)

logger = logging.getLogger(__name__)

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _log_call(provider: str, entry: dict[str, object]) -> None:
    try:
        log_dir = data_dir() / "llm-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        payload = {**entry, "provider": provider, "ended_at": _now_iso()}
        with (log_dir / f"{provider}.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("llm debug log write failed", exc_info=True)


def _combined_prompt(system_prompt: str, user_input: str) -> str:
    return "\n\n".join(
        [
            "Follow these task-specific system instructions exactly:",
            system_prompt.strip(),
            "Use this input payload:",
            user_input,
        ]
    )


def _generate_codex_cli(
    system_prompt: str, user_input: str, config: LLMConfig, timeout: int
) -> str:
    binary = find_binary("codex", "CODEX_CLI_PATH", CODEX_FALLBACK_LOCATIONS)
    if not binary:
        raise LLMClientError("`codex` CLI not found. Set CODEX_CLI_PATH or install the Codex CLI.")

    with tempfile.NamedTemporaryFile(
        prefix="codex-last-message-", suffix=".txt", delete=False
    ) as tmp:
        output_path = Path(tmp.name)
    cmd = [
        binary,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--output-last-message",
        str(output_path),
        "-",
    ]
    if config.model:
        cmd[-1:-1] = ["--model", config.model]
    prompt = _combined_prompt(system_prompt, user_input)
    started = _now_iso()
    output_text = ""
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        if output_path.exists():
            output_text = output_path.read_text(encoding="utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired as exc:
        raise LLMTransientError(f"codex CLI timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise LLMClientError("`codex` CLI not found on PATH") from exc
    finally:
        with contextlib.suppress(OSError):
            output_path.unlink(missing_ok=True)

    if debug_enabled(config.provider):
        _log_call(
            config.provider,
            {
                "started_at": started,
                "cmd": cmd,
                "input_preview": prompt[:500],
                "input_chars": len(prompt),
                "returncode": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "output_text": output_text,
            },
        )

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "(no output)"
        raise LLMTransientError(f"codex CLI exited {proc.returncode}: {detail}")
    return output_text or (proc.stdout or "").strip()


def _generate_claude_cli(
    system_prompt: str, user_input: str, config: LLMConfig, timeout: int
) -> str:
    binary = find_binary("claude", "CLAUDE_CLI_PATH", CLAUDE_FALLBACK_LOCATIONS)
    if not binary:
        raise LLMClientError("`claude` CLI not found. Set CLAUDE_CLI_PATH or install Claude Code.")

    cmd = [
        binary,
        "-p",
        "--system-prompt",
        system_prompt,
        "--output-format",
        "json",
        "--tools",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--model",
        config.model,
    ]
    child_env = os.environ.copy()
    if os.getenv("CLAUDE_CLI_USE_API_KEY", "").strip().lower() not in {"1", "true", "yes"}:
        child_env.pop("ANTHROPIC_API_KEY", None)

    started = _now_iso()
    try:
        proc = subprocess.run(
            cmd,
            input=user_input,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMTransientError(f"claude CLI timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise LLMClientError("`claude` CLI not found on PATH") from exc

    if debug_enabled(config.provider):
        _log_call(
            config.provider,
            {
                "started_at": started,
                "cmd": cmd,
                "input_preview": user_input[:500],
                "input_chars": len(user_input),
                "returncode": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
            },
        )

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "(no output)"
        raise LLMTransientError(f"claude CLI exited {proc.returncode}: {detail}")

    raw = (proc.stdout or "").strip()
    if not raw:
        raise LLMTransientError("claude CLI returned no output")
    try:
        envelope: object = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(envelope, dict):
        return raw
    envelope_dict = cast("dict[str, object]", envelope)
    if envelope_dict.get("is_error"):
        msg = envelope_dict.get("result") or envelope_dict.get("error") or "(no message)"
        raise LLMTransientError(f"claude CLI error: {msg}")
    result = envelope_dict.get("result")
    if not isinstance(result, str) or not result.strip():
        raise LLMTransientError("claude CLI returned empty result")
    return result


def _post_json(url: str, headers: dict[str, str], body: dict[str, object], timeout: int) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={**headers, "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        # 429 and 5xx pass on a retry often enough to earn one; a 4xx is
        # the request's fault and will repeat identically (spec 058).
        error_cls = LLMTransientError if exc.code == 429 or exc.code >= 500 else LLMClientError
        raise error_cls(f"{url} exited {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMTransientError(f"{url} request failed: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMTransientError(f"{url} returned non-JSON output") from exc


def _api_debug_log(
    config: LLMConfig,
    started: str,
    url: str,
    system_prompt: str,
    user_input: str,
    *,
    output_text: str = "",
    error: str = "",
) -> None:
    """Sanitized debug record for the API providers: never headers, never
    the API key."""
    if not debug_enabled(config.provider):
        return
    _log_call(
        config.provider,
        {
            "started_at": started,
            "url": url,
            "model": config.model,
            "input_preview": user_input[:500],
            "input_chars": len(system_prompt) + len(user_input),
            "output_text": output_text,
            "error": error,
        },
    )


def _generate_openai_api(
    system_prompt: str, user_input: str, config: LLMConfig, timeout: int
) -> str:
    if not config.api_key:
        raise LLMClientError("OPENAI_API_KEY is required when AI_PROVIDER=openai-api")
    started = _now_iso()
    try:
        data = _post_json(
            OPENAI_RESPONSES_URL,
            {"authorization": f"Bearer {config.api_key}"},
            {"model": config.model, "instructions": system_prompt, "input": user_input},
            timeout,
        )
    except LLMClientError as exc:
        _api_debug_log(
            config, started, OPENAI_RESPONSES_URL, system_prompt, user_input, error=str(exc)
        )
        raise
    result = ""
    if isinstance(data, dict):
        payload = cast("dict[str, object]", data)
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            result = output_text.strip()
        else:
            chunks: list[str] = []
            output = payload.get("output")
            if isinstance(output, list):
                for item in cast("list[object]", output):
                    if not isinstance(item, dict):
                        continue
                    content = cast("dict[str, object]", item).get("content")
                    if not isinstance(content, list):
                        continue
                    for part in cast("list[object]", content):
                        if isinstance(part, dict):
                            text = cast("dict[str, object]", part).get("text")
                            if isinstance(text, str) and text:
                                chunks.append(text)
            result = "\n".join(chunks).strip()
    _api_debug_log(
        config, started, OPENAI_RESPONSES_URL, system_prompt, user_input, output_text=result
    )
    return result


def _anthropic_max_tokens() -> int:
    """Validated so a bad value raises LLMClientError, which the auto chain
    catches and aggregates instead of dying on ValueError."""
    raw = os.getenv("ANTHROPIC_MAX_TOKENS", "4096").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise LLMClientError(f"ANTHROPIC_MAX_TOKENS must be an integer, got {raw!r}") from exc
    if value < 1:
        raise LLMClientError(f"ANTHROPIC_MAX_TOKENS must be positive, got {value}")
    return value


def _generate_anthropic_api(
    system_prompt: str, user_input: str, config: LLMConfig, timeout: int
) -> str:
    if not config.api_key:
        raise LLMClientError("ANTHROPIC_API_KEY is required when AI_PROVIDER=anthropic-api")
    max_tokens = _anthropic_max_tokens()
    started = _now_iso()
    try:
        data = _post_json(
            ANTHROPIC_MESSAGES_URL,
            {"x-api-key": config.api_key, "anthropic-version": "2023-06-01"},
            {
                "model": config.model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_input}],
            },
            timeout,
        )
    except LLMClientError as exc:
        _api_debug_log(
            config, started, ANTHROPIC_MESSAGES_URL, system_prompt, user_input, error=str(exc)
        )
        raise
    chunks: list[str] = []
    if isinstance(data, dict):
        content = cast("dict[str, object]", data).get("content")
        if isinstance(content, list):
            for item in cast("list[object]", content):
                if isinstance(item, dict):
                    item_dict = cast("dict[str, object]", item)
                    if item_dict.get("type") == "text":
                        text = item_dict.get("text")
                        if isinstance(text, str) and text:
                            chunks.append(text)
    result = "\n".join(chunks).strip()
    _api_debug_log(
        config, started, ANTHROPIC_MESSAGES_URL, system_prompt, user_input, output_text=result
    )
    return result


def generate_with_config(
    system_prompt: str, user_input: str, config: LLMConfig, timeout: int
) -> str:
    if config.provider == "codex-cli":
        return _generate_codex_cli(system_prompt, user_input, config, timeout)
    if config.provider == "claude-cli":
        return _generate_claude_cli(system_prompt, user_input, config, timeout)
    if config.provider == "openai-api":
        return _generate_openai_api(system_prompt, user_input, config, timeout)
    return _generate_anthropic_api(system_prompt, user_input, config, timeout)
