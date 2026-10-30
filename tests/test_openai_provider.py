import pytest
from unittest.mock import MagicMock

from app.providers import openai as openai_provider


def test_normalize_openai_base_url_adds_openai_v1_suffix():
    assert (
        openai_provider.normalize_openai_base_url(
            "https://example-resource.cognitiveservices.azure.com"
        )
        == "https://example-resource.cognitiveservices.azure.com/openai/v1/"
    )


def test_normalize_openai_base_url_preserves_openai_v1_suffix():
    assert (
        openai_provider.normalize_openai_base_url(
            "https://example-resource.cognitiveservices.azure.com/openai/v1/"
        )
        == "https://example-resource.cognitiveservices.azure.com/openai/v1/"
    )


def test_gpt52_provider_uses_plain_openai_client(monkeypatch):
    captured = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", fake_async_openai)
    monkeypatch.setenv(
        "OPENAI_BASE_URL_GPT_5_2",
        "https://example-resource.cognitiveservices.azure.com",
    )
    monkeypatch.setenv("OPENAI_GPT_5_2_API_KEY", "azure-key")

    provider = openai_provider.GPT52Provider()
    provider._get_client()

    assert captured["api_key"] == "azure-key"
    assert (
        captured["base_url"]
        == "https://example-resource.cognitiveservices.azure.com/openai/v1/"
    )
    assert captured["max_retries"] == 0


def test_openai_provider_uses_openai_base_url_when_specific_url_is_missing(monkeypatch):
    captured = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", fake_async_openai)
    monkeypatch.setenv(
        "OPENAI_BASE_URL",
        "https://main-resource.cognitiveservices.azure.com",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "main-key")
    monkeypatch.delenv("OPENAI_BASE_URL_GPT_5_2", raising=False)

    provider = openai_provider.OpenAIProvider()
    provider._get_client()

    assert captured["api_key"] == "main-key"
    assert (
        captured["base_url"]
        == "https://main-resource.cognitiveservices.azure.com/openai/v1/"
    )
    assert captured["max_retries"] == 0


def test_gpt52_provider_falls_back_to_openai_base_url_and_api_key(monkeypatch):
    captured = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", fake_async_openai)
    monkeypatch.setenv(
        "OPENAI_BASE_URL",
        "https://main-resource.cognitiveservices.azure.com",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "main-key")
    monkeypatch.delenv("OPENAI_BASE_URL_GPT_5_2", raising=False)
    monkeypatch.delenv("OPENAI_GPT_5_2_API_KEY", raising=False)

    provider = openai_provider.GPT52Provider()
    provider._get_client()

    assert captured["api_key"] == "main-key"
    assert (
        captured["base_url"]
        == "https://main-resource.cognitiveservices.azure.com/openai/v1/"
    )
    assert captured["max_retries"] == 0


def _gpt5_provider_with_fake_client(captured: dict):
    """Build a GPT5Provider whose Responses API call is intercepted, without
    hitting the network or requiring real credentials."""
    provider = openai_provider.GPT5Provider.__new__(openai_provider.GPT5Provider)
    provider.provider_type = "openai"
    provider.model_name = "gpt-5"
    provider.instance_name = "gpt-5"

    class FakeResponse:
        output_text = '{"categories": ["Family Law"], "followup_questions": []}'

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeResponse()

    fake_client = MagicMock()
    fake_client.responses.create = fake_create
    provider.client = fake_client
    return provider


@pytest.mark.asyncio
async def test_gpt5_responses_api_includes_followup_answers():
    """Regression test for the bug where the Responses API branch built its
    `input` from problem_description alone and never referenced
    followup_answers, so a refinement call was byte-identical to the initial
    call (see app/services/classification_service.py's follow-up handling
    and the multi-turn regression coverage)."""
    captured: dict = {}
    provider = _gpt5_provider_with_fake_client(captured)

    followup_answers = [
        {
            "question": "Are you the tenant or the landlord?",
            "answer": "I am the tenant.",
        }
    ]

    await provider.classify(
        problem_description="My landlord kept my deposit.",
        taxonomy=None,
        custom_prompt="SYSTEM PROMPT",
        followup_answers=followup_answers,
    )

    assert isinstance(captured["input"], list)
    roles_and_content = [(m["role"], m["content"]) for m in captured["input"]]
    assert ("assistant", "Are you the tenant or the landlord?") in roles_and_content
    assert ("user", "I am the tenant.") in roles_and_content


@pytest.mark.asyncio
async def test_gpt5_responses_api_single_turn_when_no_followup():
    """Without follow-up answers, the Responses API input is still just the
    system+user turn pair (no behavior change for the initial call)."""
    captured: dict = {}
    provider = _gpt5_provider_with_fake_client(captured)

    await provider.classify(
        problem_description="My landlord kept my deposit.",
        taxonomy=None,
        custom_prompt="SYSTEM PROMPT",
    )

    assert captured["input"] == [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "My landlord kept my deposit."},
    ]
