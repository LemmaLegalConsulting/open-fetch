from app.providers import openai as openai_provider
from app.providers.mistral import MistralProvider


def test_mistral_provider_falls_back_to_shared_openai_base_url(monkeypatch):
    captured = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", fake_async_openai)
    monkeypatch.setenv(
        "OPENAI_BASE_URL",
        "https://shared-resource.cognitiveservices.azure.com",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key")
    monkeypatch.delenv("OPENAI_BASE_URL_MISTRAL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    provider = MistralProvider()

    assert captured["api_key"] == "shared-key"
    assert (
        captured["base_url"]
        == "https://shared-resource.cognitiveservices.azure.com/openai/v1/"
    )
    assert captured["max_retries"] == 0


def test_mistral_provider_prefers_model_specific_base_url(monkeypatch):
    captured = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", fake_async_openai)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shared-resource.example")
    monkeypatch.setenv(
        "OPENAI_BASE_URL_MISTRAL",
        "https://mistral-resource.cognitiveservices.azure.com/openai/v1/",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "mistral-key")

    provider = MistralProvider()

    assert captured["api_key"] == "mistral-key"
    assert (
        captured["base_url"]
        == "https://mistral-resource.cognitiveservices.azure.com/openai/v1/"
    )
    assert captured["max_retries"] == 0
