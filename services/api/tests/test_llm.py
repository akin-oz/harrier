"""Behavior pins for the LLM provider seam (spec 012), ported from the old
repo's tests/test_llm_client.py plus the error-path pins."""

import pytest

import harrier.llm as llm
import harrier.llm.config as llm_config
import harrier.llm.providers as llm_providers
from harrier.llm import LLMClientError, LLMConfig, generate_text, load_config


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
    monkeypatch: pytest.MonkeyPatch,
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

    output = generate_text("system", "user")

    assert output == "ok"
    assert calls == [("openai-api", "gpt-5-mini"), ("anthropic-api", "claude-sonnet-4-5")]


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
    # Stated change from the old client, which dropped an explicit model
    # during auto fallback unless AI_MODEL was also set.
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
