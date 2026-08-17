from __future__ import annotations

from claude_code_backend.service import LocalAgentService
from personal_news_agent import config
from personal_news_agent.services.llm import LLMClient


def _clear_model_env(monkeypatch) -> None:
    for name in (
        "PNA_LLM_ENDPOINT",
        "PNA_LLM_KEY",
        "PNA_LLM_MODEL",
        "PNA_CC_RUNTIME_BASE_URL",
        "PNA_CC_RUNTIME_AUTH_TOKEN",
        "PNA_CC_RUNTIME_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_deepseek_uses_one_shared_key_and_ignores_stale_cc_token(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("PNA_LLM_ENDPOINT", "https://api.deepseek.com")
    monkeypatch.setenv("PNA_LLM_KEY", "current-deepseek-key")
    monkeypatch.setenv("PNA_CC_RUNTIME_AUTH_TOKEN", "stale-different-key")

    settings = config.Settings()

    assert settings.llm_key == "current-deepseek-key"
    assert settings.effective_cc_runtime_base_url == "https://api.deepseek.com/anthropic"
    assert settings.effective_cc_runtime_auth_token == settings.llm_key


def test_explicit_deepseek_cc_url_still_uses_shared_llm_key(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("PNA_LLM_ENDPOINT", "https://api.deepseek.com/")
    monkeypatch.setenv("PNA_LLM_KEY", "one-key")
    monkeypatch.setenv("PNA_CC_RUNTIME_BASE_URL", "https://api.deepseek.com/anthropic/")
    monkeypatch.setenv("PNA_CC_RUNTIME_AUTH_TOKEN", "must-not-win")

    settings = config.Settings()

    assert settings.effective_cc_runtime_base_url == "https://api.deepseek.com/anthropic"
    assert settings.effective_cc_runtime_auth_token == "one-key"


def test_provider_contract_is_pinned_even_if_legacy_cc_env_exists(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("PNA_LLM_ENDPOINT", "https://api.deepseek.com")
    monkeypatch.setenv("PNA_LLM_KEY", "shared-key")
    monkeypatch.setenv("PNA_CC_RUNTIME_BASE_URL", "https://gateway.example.test/anthropic")
    monkeypatch.setenv("PNA_CC_RUNTIME_AUTH_TOKEN", "gateway-token")

    settings = config.Settings()

    assert settings.effective_cc_runtime_base_url == "https://api.deepseek.com/anthropic"
    assert settings.effective_cc_runtime_auth_token == "shared-key"


def test_deepseek_without_key_is_not_cc_configured(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("PNA_LLM_ENDPOINT", "https://api.deepseek.com")

    settings = config.Settings()

    assert settings.effective_cc_runtime_base_url == "https://api.deepseek.com/anthropic"
    assert settings.llm_key is None
    assert settings.effective_cc_runtime_auth_token is None


def test_explicit_settings_cannot_override_deepseek_runtime_policy():
    settings = config.Settings(
        llm_endpoint="https://api.deepseek.com",
        llm_key="shared-key",
    )

    assert settings.effective_cc_runtime_base_url == "https://api.deepseek.com/anthropic"
    assert settings.effective_cc_runtime_auth_token == "shared-key"
    assert settings.effective_cc_runtime_api_key is None


def test_compatibility_agent_uses_the_same_application_provider_contract(tmp_path):
    settings = config.Settings(
        llm_endpoint="https://api.deepseek.com",
        llm_key="shared-key",
        llm_model="deepseek-v4-flash",
        llm_default_model="yuanrong-personal-assistant",
        llm_timeout_seconds=120,
        news_llm_analysis_enabled=True,
    )

    service = LocalAgentService.from_app_settings(settings)

    assert service.config.provider_name == "deepseek"
    assert service.config.base_url == settings.llm_endpoint
    assert service.config.api_key == settings.llm_key
    assert service.config.runtime_model == settings.llm_model
    assert service.config.default_model_key == settings.llm_default_model
    assert service.config.timeout_seconds == settings.llm_timeout_seconds
    assert "shared-key" not in repr(service.config)


def test_news_analysis_pause_disables_direct_and_compatibility_model_calls():
    settings = config.Settings(
        llm_endpoint="https://api.deepseek.com",
        llm_key="shared-key",
        news_llm_analysis_enabled=False,
    )

    assert LLMClient(settings).configured is False
    assert LocalAgentService.from_app_settings(settings).config.api_key is None


def test_model_env_template_is_the_fixed_supported_contract():
    lines = (config.BASE_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
    actual = {
        line.split("=", 1)[0]
        for line in lines
        if line.startswith(("PNA_LLM_", "PNA_CC_RUNTIME_", "PNA_LOCAL_AGENT_"))
    }
    assert actual == {
        "PNA_LLM_ENDPOINT",
        "PNA_LLM_KEY",
        "PNA_LLM_MODEL",
        "PNA_LLM_DEFAULT_MODEL",
        "PNA_LLM_TIMEOUT_SECONDS",
        "PNA_CC_RUNTIME_ENABLED",
        "PNA_CC_RUNTIME_EFFORT",
        "PNA_CC_RUNTIME_MAX_TURNS",
        "PNA_CC_RUNTIME_MAX_BUDGET_USD",
        "PNA_CC_RUNTIME_TIMEOUT_SECONDS",
        "PNA_CC_RUNTIME_ALLOW_EXISTING_LOGIN",
        "PNA_CC_RUNTIME_BUILTIN_WEB_SEARCH",
        "PNA_CC_RUNTIME_CONFIG_DIR",
        "PNA_LOCAL_AGENT_MAX_HISTORY",
        "PNA_LOCAL_AGENT_TEMPERATURE",
        "PNA_LOCAL_AGENT_SESSION_STORE",
    }
