"""Role credentials refresh without blocking the API or crossing endpoint boundaries."""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.providers.bedrock_auth import BedrockAuth
from app.providers.compatible import CompatibleProvider


@pytest.mark.parametrize(
    "protocol,path,header",
    [
        ("openai", "/openai/v1", "authorization"),
        ("anthropic", "/anthropic", "x-api-key"),
    ],
)
@pytest.mark.asyncio
async def test_role_auth_refreshes_each_request(monkeypatch, protocol, path, header):
    monkeypatch.setenv("MODEL", "synthetic")
    monkeypatch.setenv("URL", "https://bedrock-runtime.us-east-1.amazonaws.com" + path)
    thread = threading.get_ident()
    generated = []

    def token(self):
        assert threading.get_ident() != thread
        generated.append(len(generated) + 1)
        return f"synthetic-{len(generated)}"

    monkeypatch.setattr(BedrockAuth, "_token", token)
    observed = []

    def handle(request):
        observed.append(request.headers[header])
        if protocol == "anthropic":
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "{}"}],
                },
            )
        assert "x-api-key" not in request.headers
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ],
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.providers.compatible.httpx",
        SimpleNamespace(
            AsyncClient=lambda **kw: real_client(
                transport=httpx.MockTransport(handle), **kw
            )
        ),
    )
    provider = CompatibleProvider(
        "test",
        {
            "protocol": protocol,
            "model_env": "MODEL",
            "base_url_env": "URL",
            "auth": "bedrock",
        },
    )
    try:
        await provider.complete("system", "synthetic")
        await provider.complete("system", "synthetic")
    finally:
        await provider.aclose()
    prefix = "Bearer " if protocol == "openai" else ""
    assert observed == [prefix + "synthetic-1", prefix + "synthetic-2"]


@pytest.mark.parametrize(
    "url",
    [
        "https://bedrock-runtime.us-east-1.amazonaws.com.evil.example/openai/v1",
        "https://example.com/openai/v1",
        "https://bedrock-runtime.us-east-1.amazonaws.com:444/openai/v1",
        "https://bedrock-runtime.us-east-1.amazonaws.com/wrong",
    ],
)
def test_role_auth_rejects_untrusted_endpoints(url):
    with pytest.raises(ValueError):
        BedrockAuth(url, "openai")


@pytest.mark.asyncio
async def test_role_auth_refuses_other_request_destination(monkeypatch):
    auth = BedrockAuth(
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1", "openai"
    )

    def fail():
        pytest.fail("Must not retrieve AWS credentials for another destination")

    monkeypatch.setattr(auth, "_token", fail)
    async with httpx.AsyncClient(
        auth=auth, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        with pytest.raises(ValueError):
            await client.get("https://example.com/")


def test_role_auth_freezes_refreshed_credentials(monkeypatch):
    import aws_bedrock_token_generator

    auth = BedrockAuth(
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1", "openai"
    )
    credentials = iter(["first-frozen-session", "refreshed-frozen-session"])
    auth.session = SimpleNamespace(
        get_credentials=lambda: SimpleNamespace(
            get_frozen_credentials=lambda: next(credentials)
        )
    )

    def provide_token(**kw):
        assert kw["region"] == "us-east-1"
        assert kw["expiry"].total_seconds() == 300
        return kw["aws_credentials_provider"].load()

    monkeypatch.setattr(aws_bedrock_token_generator, "provide_token", provide_token)
    assert auth._token() == "first-frozen-session"
    assert auth._token() == "refreshed-frozen-session"
