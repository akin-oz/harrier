"""Behavior pins for the LLM provider seam (spec 012), ported from the old
repo's tests/test_llm_client.py plus the error-path and debug-log pins."""

import json
from pathlib import Path

import pytest

import harrier.llm as llm
import harrier.llm.config as llm_config
import harrier.llm.providers as llm_providers
from harrier.llm import LLMClientError, LLMConfig, LLMTransientError, generate_text, load_config


@pytest.fixture(autouse=True)
def zero_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transient retry (spec 058) waits 2 seconds in production; tests
    that trigger it should not."""
    monkeypatch.setattr(llm, "RETRY_WAIT_SECONDS", 0)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AI_PROVIDER",
        "AI_MODEL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_MODEL",
        "ANTHROPIC_MODEL",
        "CODEX_CLI_MODEL",
        "CLAUDE_CLI_MODEL",
        "CODEX_CLI_PATH",
        "CLAUDE_CLI_PATH",
        "ANTHROPIC_MAX_TOKENS",
        "AI_DEBUG",
        "OPENAI_DEBUG",
        "ANTHROPIC_DEBUG",
        "CODEX_CLI_DEBUG",
        "CLAUDE_CLI_DEBUG",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_provider_is_codex_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def only_codex() -> list[str]:
        return ["codex-cli"]

    monkeypatch.setattr(llm_config, "resolve_auto_providers", only_codex)
    config = load_config()
    assert config.provider == "codex-cli"
    assert config.model == ""
    assert config.api_key == "codex-cli"


def test_auto_provider_uses_openai_key_when_codex_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_binary(name: str, path_env: str, fallbacks: tuple[str, ...]) -> str | None:
        return None

    monkeypatch.setattr(llm_config, "find_binary", no_binary)
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    config = load_config()
    assert config.provider == "openai-api"
    assert config.model == "gpt-5-mini"
    assert config.api_key == "key"


def test_provider_aliases_and_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_MODEL", "gpt-custom")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    config = load_config()
    assert config.provider == "openai-api"
    assert config.model == "gpt-custom"
    assert config.api_key == "key"


def test_provider_specific_model_is_used_without_global_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_PROVIDER", "anthropic-api")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    config = load_config()
    assert config.provider == "anthropic-api"
    assert config.model == "claude-test"
    assert config.api_key == "key"


def test_unknown_provider_raises() -> None:
    with pytest.raises(LLMClientError, match="unsupported AI provider"):
        load_config(provider="grok")


def test_auto_generation_falls_back_after_provider_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_generate(
        _system_prompt: str, _user_input: str, config: LLMConfig, _timeout: int
    ) -> str:
        calls.append((config.provider, config.model))
        if config.provider == "openai-api":
            raise LLMClientError("quota exceeded")
        return "ok"

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    def two_providers() -> list[str]:
        return ["openai-api", "anthropic-api"]

    monkeypatch.setattr(llm, "resolve_auto_providers", two_providers)
    monkeypatch.setattr(llm, "generate_with_config", fake_generate)

    with caplog.at_level("WARNING", logger="harrier.llm"):
        output = generate_text("system", "user")

    assert output == "ok"
    assert calls == [("openai-api", "gpt-5-mini"), ("anthropic-api", "claude-sonnet-4-5")]
    assert any(
        "fell back to anthropic-api" in record.getMessage()
        and "openai-api: quota exceeded" in record.getMessage()
        for record in caplog.records
    )


def test_auto_generation_raises_with_every_error_when_all_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_generate(
        _system_prompt: str, _user_input: str, config: LLMConfig, _timeout: int
    ) -> str:
        if config.provider == "openai-api":
            raise LLMClientError("quota exceeded")
        return "   "

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    def two_providers() -> list[str]:
        return ["openai-api", "anthropic-api"]

    monkeypatch.setattr(llm, "resolve_auto_providers", two_providers)
    monkeypatch.setattr(llm, "generate_with_config", fake_generate)

    with pytest.raises(LLMClientError) as excinfo:
        generate_text("system", "user")
    message = str(excinfo.value)
    assert "openai-api: quota exceeded" in message
    assert "anthropic-api: empty response" in message


def test_fixed_provider_empty_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "key")

    def empty_generate(
        _system_prompt: str, _user_input: str, _config: LLMConfig, _timeout: int
    ) -> str:
        return ""

    monkeypatch.setattr(llm, "generate_with_config", empty_generate)
    with pytest.raises(LLMClientError, match="empty response"):
        generate_text("system", "user", provider="openai-api")


def test_explicit_model_is_honored_in_auto_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Contract: an explicitly passed model reaches the provider config in
    # auto mode; this test proves it for the single-available-provider
    # case. (Stated change in spec 012: the old client dropped the model
    # argument during auto fallback unless AI_MODEL was also set.)
    captured: list[str] = []

    def fake_generate(
        _system_prompt: str, _user_input: str, config: LLMConfig, _timeout: int
    ) -> str:
        captured.append(config.model)
        return "ok"

    monkeypatch.setenv("OPENAI_API_KEY", "key")

    def one_provider() -> list[str]:
        return ["openai-api"]

    monkeypatch.setattr(llm, "resolve_auto_providers", one_provider)
    monkeypatch.setattr(llm, "generate_with_config", fake_generate)

    generate_text("system", "user", model="my-model")
    assert captured == ["my-model"]


def test_invalid_anthropic_max_tokens_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LLMClientError, not ValueError, so the auto chain can catch and
    # aggregate it instead of dying mid-fallback.
    config = LLMConfig(provider="anthropic-api", model="m", api_key="key")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "lots")
    with pytest.raises(LLMClientError, match="must be an integer"):
        llm_providers.generate_with_config("system", "user", config, 30)
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "0")
    with pytest.raises(LLMClientError, match="must be positive"):
        llm_providers.generate_with_config("system", "user", config, 30)


def test_api_debug_flag_logs_success_without_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(data_directory))
    monkeypatch.setenv("OPENAI_DEBUG", "1")

    def fake_post(
        url: str, headers: dict[str, str], body: dict[str, object], timeout: int
    ) -> object:
        return {"output_text": "hello"}

    monkeypatch.setattr(llm_providers, "_post_json", fake_post)
    config = LLMConfig(provider="openai-api", model="m", api_key="sk-secret-value")
    output = llm_providers.generate_with_config("system", "user", config, 30)
    assert output == "hello"

    log_path = data_directory / "llm-logs" / "openai-api.log"
    entry = json.loads(log_path.read_text().splitlines()[-1])
    assert entry["output_text"] == "hello"
    assert "sk-secret-value" not in log_path.read_text()


def test_api_debug_flag_logs_failure_without_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(data_directory))
    monkeypatch.setenv("ANTHROPIC_DEBUG", "1")

    def fake_post(
        url: str, headers: dict[str, str], body: dict[str, object], timeout: int
    ) -> object:
        raise LLMClientError("api exited 500")

    monkeypatch.setattr(llm_providers, "_post_json", fake_post)
    config = LLMConfig(provider="anthropic-api", model="m", api_key="sk-ant-secret")
    with pytest.raises(LLMClientError, match="api exited 500"):
        llm_providers.generate_with_config("system", "user", config, 30)

    log_path = data_directory / "llm-logs" / "anthropic-api.log"
    entry = json.loads(log_path.read_text().splitlines()[-1])
    assert entry["error"] == "api exited 500"
    assert "sk-ant-secret" not in log_path.read_text()


def test_claude_cli_error_envelope_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        returncode = 0
        stdout = '{"is_error": true, "result": "over quota"}'
        stderr = ""

    def fake_binary(name: str, path_env: str, fallbacks: tuple[str, ...]) -> str | None:
        return "/bin/claude"

    def fake_run(*args: object, **kwargs: object) -> FakeProc:
        return FakeProc()

    monkeypatch.setattr(llm_providers, "find_binary", fake_binary)
    monkeypatch.setattr(llm_providers.subprocess, "run", fake_run)
    config = LLMConfig(provider="claude-cli", model="sonnet", api_key="claude-cli")
    with pytest.raises(LLMClientError, match="over quota"):
        llm_providers.generate_with_config("system", "user", config, 30)


# ---------------------------------------------------------------------------
# Transient retry (spec 058)
# ---------------------------------------------------------------------------


def test_transient_failure_retries_once_and_returns_second_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[int] = []

    def flaky(_system_prompt: str, _user_input: str, _config: LLMConfig, _timeout: int) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise LLMTransientError("claude CLI exited 1: connection closed mid-response")
        return "recovered"

    monkeypatch.setattr(llm, "generate_with_config", flaky)
    with caplog.at_level("WARNING", logger="harrier.llm"):
        output = generate_text("system", "user", provider="claude-cli")
    assert output == "recovered"
    assert len(attempts) == 2
    assert any("retrying once" in record.getMessage() for record in caplog.records)


def test_second_transient_failure_raises_with_second_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def failing(_system_prompt: str, _user_input: str, _config: LLMConfig, _timeout: int) -> str:
        attempts.append(1)
        raise LLMTransientError(f"attempt {len(attempts)} failed")

    monkeypatch.setattr(llm, "generate_with_config", failing)
    with pytest.raises(LLMClientError, match="attempt 2 failed"):
        generate_text("system", "user", provider="claude-cli")
    assert len(attempts) == 2


def test_non_transient_failure_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def missing_binary(
        _system_prompt: str, _user_input: str, _config: LLMConfig, _timeout: int
    ) -> str:
        attempts.append(1)
        raise LLMClientError("`claude` CLI not found on PATH")

    monkeypatch.setattr(llm, "generate_with_config", missing_binary)
    with pytest.raises(LLMClientError, match="not found"):
        generate_text("system", "user", provider="claude-cli")
    assert len(attempts) == 1


def test_empty_first_response_retries_and_returns_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def empty_then_full(
        _system_prompt: str, _user_input: str, _config: LLMConfig, _timeout: int
    ) -> str:
        attempts.append(1)
        return "" if len(attempts) == 1 else "second"

    monkeypatch.setattr(llm, "generate_with_config", empty_then_full)
    assert generate_text("system", "user", provider="claude-cli") == "second"
    assert len(attempts) == 2


def test_empty_response_twice_raises_the_same_error_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def always_empty(
        _system_prompt: str, _user_input: str, _config: LLMConfig, _timeout: int
    ) -> str:
        attempts.append(1)
        return "   "

    monkeypatch.setattr(llm, "generate_with_config", always_empty)
    with pytest.raises(LLMClientError, match="claude-cli returned an empty response"):
        generate_text("system", "user", provider="claude-cli")
    assert len(attempts) == 2
