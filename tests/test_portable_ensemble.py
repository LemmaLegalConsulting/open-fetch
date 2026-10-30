"""Offline wire contracts for named Azure Foundry and AWS Bedrock endpoints."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from app.providers.compatible import CompatibleProvider, configured_endpoints
from app.services.classification_service import ClassificationService
from app.models.api_models import ClassificationRequest


@pytest.mark.parametrize("cloud", ["azure", "bedrock"])
@pytest.mark.asyncio
async def test_three_model_ensemble_and_auxiliary_calls(monkeypatch, cloud):
    monkeypatch.setenv("OFFLINE_MODE", "0")
    monkeypatch.setenv("FETCH_ENABLED_PROVIDERS", "gpt,haiku,open_weight")
    monkeypatch.setenv("FETCH_AUXILIARY_MODEL", "haiku")
    configs, urls = {}, {}
    for name, protocol in [
        ("gpt", "openai"),
        ("haiku", "anthropic"),
        ("open_weight", "openai"),
    ]:
        base = (
            "https://synthetic.services.ai.azure.com"
            if cloud == "azure"
            else "https://bedrock-runtime.us-east-1.amazonaws.com"
        )
        urls[name] = base + ("/anthropic" if protocol == "anthropic" else "/openai/v1")
        configs[name] = dict(
            protocol=protocol,
            model_env=name.upper() + "_MODEL",
            base_url_env=name.upper() + "_URL",
            api_key_env=name.upper() + "_KEY",
        )
        monkeypatch.setenv(name.upper() + "_MODEL", "synthetic-" + name)
        monkeypatch.setenv(name.upper() + "_URL", urls[name])
        monkeypatch.setenv(name.upper() + "_KEY", "synthetic-key-" + name)
    monkeypatch.setenv("FETCH_MODEL_ENDPOINTS", json.dumps(configs))
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append((request, body))
        content = json.dumps(
            {
                "categories": ["Family Law > Paternity"],
                "questions": [],
                "resolutions": [],
            }
        )
        if request.url.path.endswith("/messages"):
            assert request.headers["x-api-key"] == "synthetic-key-haiku"
            assert "authorization" not in request.headers
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert body["system"] and body["max_tokens"] == 4096
            assert all(m["role"] != "system" for m in body["messages"])
            return httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": content}],
                    "usage": {"input_tokens": 5, "output_tokens": 6},
                },
            )
        name = body["model"].removeprefix("synthetic-")
        assert str(request.url) == urls[name] + "/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-key-" + name
        assert "x-api-key" not in request.headers
        assert set(body) == {"model", "messages"}
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )

    real_http_client = httpx.AsyncClient
    real_openai_client = AsyncOpenAI
    monkeypatch.setattr(
        "app.providers.compatible.httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: real_http_client(
                transport=httpx.MockTransport(handle), **kwargs
            )
        ),
    )
    monkeypatch.setattr(
        "app.providers.compatible.AsyncOpenAI",
        lambda **kwargs: real_openai_client(
            http_client=real_http_client(transport=httpx.MockTransport(handle)),
            **kwargs
        ),
    )
    service = ClassificationService()
    assert {p.instance_name for p in service.providers} == set(configs)
    assert not {"gemini", "mistral", "openai"} & service._all_providers.keys()
    assert service.openai_client is None
    try:
        result = await service.classify(
            ClassificationRequest(
                text="A synthetic paternity issue",
                followup_answers=[{"question": "Parentage disputed?", "answer": "Yes"}],
                skip_followups=True,
            )
        )
        assert result.labels and len(requests) == 3
        assert {body["model"] for _, body in requests} == {
            "synthetic-" + n for n in configs
        }
        assert all("Yes" in json.dumps(body["messages"]) for _, body in requests)
        await service._call_semantic_merge_model("Merge synthetic questions", "[]")
        await service._resolve_screening_options_with_llm(
            "A custody dispute", ["Family Law > Paternity"]
        )
        assert len(requests) == 5
        assert all(body["model"] == "synthetic-haiku" for _, body in requests[-2:])
        assert all(
            str(req.url) == urls["haiku"] + "/v1/messages" for req, _ in requests[-2:]
        )
    finally:
        await service.cleanup()
        for p in service.providers:
            assert (
                p.client.is_closed
                if p.protocol == "anthropic"
                else p.client.is_closed()
            )


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "tool_use", None])
@pytest.mark.asyncio
async def test_incomplete_claude_response_rejected(monkeypatch, stop):
    for key, value in {
        "MODEL": "synthetic",
        "URL": "https://synthetic.example/anthropic",
        "KEY": "synthetic",
    }.items():
        monkeypatch.setenv(key, value)
    p = CompatibleProvider(
        "haiku",
        dict(
            protocol="anthropic",
            model_env="MODEL",
            base_url_env="URL",
            api_key_env="KEY",
        ),
    )
    await p.aclose()
    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"stop_reason": stop, "content": [{"type": "text", "text": "{}"}]},
            )
        )
    )
    try:
        with pytest.raises(ValueError, match="complete"):
            await p.complete("system", "synthetic")
    finally:
        await p.aclose()


def test_registry_rejects_reserved_names_and_inline_secrets(monkeypatch):
    for config in [{"openai": {}}, {"haiku": {"api_key": "secret"}}, {"bad/name": {}}]:
        monkeypatch.setenv("FETCH_MODEL_ENDPOINTS", json.dumps(config))
        with pytest.raises(ValueError):
            configured_endpoints()
