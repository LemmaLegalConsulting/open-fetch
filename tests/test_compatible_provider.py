import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from app.providers.compatible import CompatibleProvider
from app.providers.openai import normalize_openai_base_url
from app.services.classification_service import ClassificationService
from app.models.api_models import ClassificationRequest, FollowUpAnswer


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("COMPATIBLE_MODEL", "openai.gpt-oss-120b-1:0")
    monkeypatch.setenv(
        "COMPATIBLE_BASE_URL",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
    )
    monkeypatch.setenv("COMPATIBLE_API_KEY", "synthetic-bedrock-key")
    monkeypatch.setenv("FETCH_ENABLED_PROVIDERS", "compatible,keyword")
    monkeypatch.setenv("OFFLINE_MODE", "0")
    monkeypatch.delenv("DISABLE_EXTERNAL_PROVIDERS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_bedrock_sdk_http_contract(configured):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "openai.gpt-oss-120b-1:0",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "categories": ["Family Law > Paternity"],
                                    "followup_questions": [
                                        {
                                            "question": "Is parentage disputed?",
                                            "format": "yesno",
                                        }
                                    ],
                                }
                            ),
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 6,
                    "total_tokens": 11,
                },
            },
        )

    p = CompatibleProvider()
    await p.client.close()
    p.client = AsyncOpenAI(
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
        api_key="synthetic-bedrock-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    result = await p.classify(
        "Paternity",
        [],
        custom_prompt="Return JSON",
        followup_answers=[FollowUpAnswer(question="Who?", answer="A parent")],
    )
    assert result["labels"][0].label == "Family Law > Paternity"
    assert result["telemetry"]["usage_details"]["total_tokens"] == 11
    request = requests[0]
    body = json.loads(request.content)
    assert (
        str(request.url)
        == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions"
    )
    assert request.headers["authorization"] == "Bearer synthetic-bedrock-key"
    assert body["model"] == "openai.gpt-oss-120b-1:0"
    assert any(m["content"] == "A parent" for m in body["messages"])
    assert (
        "reasoning" not in body
        and "temperature" not in body
        and "response_format" not in body
    )
    await p.aclose()


@pytest.mark.asyncio
async def test_portable_backend_merge_screening_and_refinement(configured, monkeypatch):
    import app.services.classification_service as module

    def forbidden(**kwargs):
        raise AssertionError("Must not create an OpenAI-service client")

    monkeypatch.setattr(module, "build_async_openai_client", forbidden)
    service = ClassificationService()
    assert service.openai_client is None
    assert set(service._all_providers) == {"compatible", "keyword"}
    provider = service._all_providers["compatible"]
    provider.complete = AsyncMock(return_value=({}, '{"merged_questions":[]}'))
    model, _, content = await service._call_semantic_merge_model("prompt", "questions")
    assert model == "openai.gpt-oss-120b-1:0"
    monkeypatch.setattr(
        module, "llm_resolution_contracts", lambda *args: [{"protocol_id": "synthetic"}]
    )
    provider.complete = AsyncMock(return_value=({}, '{"resolutions":[]}'))
    assert await service._resolve_screening_options_with_llm("A story", []) == []
    provider.complete.assert_awaited_once()
    provider.complete = AsyncMock(
        return_value=({}, '{"categories":["Family Law > Paternity"]}')
    )
    request = ClassificationRequest(
        text="Paternity",
        skip_followups=True,
        enable_screening_protocols=False,
        followup_answers=[FollowUpAnswer(question="Who?", answer="A parent")],
    )
    result = await service.classify(request)
    assert any(x.label == "Family Law > Paternity" for x in result.labels)
    assert provider.complete.await_count >= 1
    with pytest.raises(ValueError, match="not enabled"):
        service._init_providers(["spot"])
    await service.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish,content",
    [("length", "{}"), ("content_filter", ""), ("stop", ""), ("stop", "[]")],
)
async def test_incomplete_invalid_responses_fail(configured, finish, content):
    p = CompatibleProvider()
    p.client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish, message=SimpleNamespace(content=content)
                )
            ]
        )
    )
    with pytest.raises(ValueError):
        await p.classify("Example", [], custom_prompt="JSON")
    await p.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://bedrock-mantle.us-east-1.api.aws/v1",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
    ],
)
def test_complete_endpoint_paths_preserved(url):
    assert normalize_openai_base_url(url) == url + "/"


@pytest.mark.asyncio
async def test_followup_questions_flow_through_vote_and_merge(configured):
    service = ClassificationService()
    provider = service._all_providers["compatible"]
    provider.complete = AsyncMock(
        side_effect=[
            (
                {},
                '{"categories":["Family Law > Paternity"],"followup_questions":[{"question":"Is parentage disputed?","format":"yesno"}]}',
            ),
            (
                {},
                '{"merged_questions":[{"question":"Is parentage disputed?","format":"yesno"}]}',
            ),
        ]
    )
    result = await service.classify(
        ClassificationRequest(
            text="Paternity",
            enable_screening_protocols=False,
            include_debug_details=True,
        )
    )
    assert result.follow_up_questions[0].question == "Is parentage disputed?"
    assert "error" not in result.raw_provider_results["compatible"]
    await service.cleanup()


@pytest.mark.parametrize("setting", ["unknown", "compatible"])
def test_invalid_explicit_configuration_fails(configured, monkeypatch, setting):
    monkeypatch.setenv("FETCH_ENABLED_PROVIDERS", setting)
    monkeypatch.delenv("COMPATIBLE_MODEL", raising=False)
    with pytest.raises((ValueError, RuntimeError)):
        ClassificationService()
