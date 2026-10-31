"""Named portable endpoints using Chat Completions or Anthropic Messages."""

import os
import json
import re
from urllib.parse import urlsplit
import httpx
from typing import Any
from openai import AsyncOpenAI
from app.models.api_models import Label, FollowUpQuestion
from app.providers.base import ClassifierProvider, load_prompt
from app.providers.utils import build_messages, build_basic_telemetry
from app.utils.backoff import run_with_backoff_async
from app.utils.json_helpers import parse_json_from_llm_response


def configured_endpoints():
    """Named model endpoints contain environment references, never inline secrets."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate model endpoint name")
            result[key] = value
        return result

    endpoints = json.loads(
        os.getenv("FETCH_MODEL_ENDPOINTS", "{}"), object_pairs_hook=unique
    )
    reserved = {
        "compatible",
        "keyword",
        "openai",
        "gemini",
        "mistral",
        "spot",
        "gpt-5",
        "gpt-5.2",
        "gpt-41-nano",
        "gpt-4.1-nano",
    }
    if not isinstance(endpoints, dict):
        raise ValueError("FETCH_MODEL_ENDPOINTS must be an object")
    for name, config in endpoints.items():
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
            or name in reserved
            or not isinstance(config, dict)
        ):
            raise ValueError("Invalid or reserved model endpoint name")
        if set(config) - {
            "protocol",
            "model_env",
            "base_url_env",
            "api_key_env",
            "max_tokens",
            "auth",
        }:
            raise ValueError("Unknown model endpoint option")
    return endpoints


class CompatibleProvider(ClassifierProvider):
    def __init__(self, name="compatible", config=None):
        config = (
            {
                "protocol": "openai",
                "model_env": "COMPATIBLE_MODEL",
                "base_url_env": "COMPATIBLE_BASE_URL",
                "api_key_env": "COMPATIBLE_API_KEY",
            }
            if config is None
            else config
        )
        self.protocol = config.get("protocol", "openai")
        if self.protocol not in {"openai", "anthropic"}:
            raise ValueError("Unsupported model endpoint protocol")
        auth_mode = config.get("auth", "api_key")
        if auth_mode not in {"api_key", "bedrock"}:
            raise ValueError("Unsupported model authentication")
        if auth_mode == "bedrock" and "api_key_env" in config:
            raise ValueError(
                "Bedrock role authentication must not configure an API key"
            )
        values = []
        fields = ["model_env", "base_url_env"]
        if auth_mode == "api_key":
            fields.append("api_key_env")
        for field in fields:
            env_name = config.get(field)
            if (
                not isinstance(env_name, str)
                or not env_name
                or not os.getenv(env_name, "").strip()
            ):
                raise ValueError("Model endpoint environment values are required")
            values.append(os.environ[env_name].strip())
        model, base_url = values[:2]
        key = values[2] if auth_mode == "api_key" else "bedrock-role-auth"
        url = urlsplit(base_url)
        if (
            url.scheme != "https"
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Model endpoint must be a complete HTTPS base URL")
        super().__init__("compatible", model)
        self.instance_name = name
        self.max_tokens = config.get("max_tokens", 4096)
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 32768:
            raise ValueError("max_tokens must be between 1 and 32768")
        auth = None
        if auth_mode == "bedrock":
            from app.providers.bedrock_auth import BedrockAuth

            auth = BedrockAuth(base_url, self.protocol)
        if self.protocol == "anthropic":
            self.messages_url = base_url.rstrip("/") + "/v1/messages"
            self.client = httpx.AsyncClient(
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=15,
                follow_redirects=False,
                auth=auth,
            )
        else:
            options = {}
            if auth is not None:
                options["http_client"] = httpx.AsyncClient(
                    auth=auth, timeout=15, follow_redirects=False
                )
            self.client = AsyncOpenAI(
                base_url=base_url, api_key=key, timeout=15, max_retries=1, **options
            )

    async def aclose(self):
        if self.protocol == "anthropic":
            await self.client.aclose()
        else:
            await self.client.close()

    async def complete(self, system: str, text: str, followup_answers=None):
        if self.protocol == "anthropic":

            async def send():
                response = await self.client.post(
                    self.messages_url,
                    json={
                        "model": self.model_name,
                        "system": system,
                        "messages": build_messages(system, text, followup_answers)[1:],
                        "max_tokens": self.max_tokens,
                    },
                )
                response.raise_for_status()
                return response

            response = await run_with_backoff_async(
                send, max_attempts=2, max_wait_seconds=1
            )
            data = response.json()
            if data.get("stop_reason") not in {"end_turn", "stop_sequence"}:
                raise ValueError("Messages endpoint did not return a complete response")
            content = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            if not content.strip():
                raise ValueError("Messages endpoint returned no text")
            return data, content
        # Avoid provider-specific temperature/reasoning/JSON-mode parameters.
        response = await self.client.chat.completions.create(
            model=self.model_name,
            messages=build_messages(system, text, followup_answers),
        )
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise ValueError("Compatible endpoint did not return a complete response")
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise ValueError("Compatible endpoint returned no text")
        return response, content

    async def classify(
        self,
        problem_description: str,
        taxonomy: Any,
        custom_prompt=None,
        reasoning_effort=None,
        followup_answers=None,
    ):
        prompt = custom_prompt or load_prompt("compatible", taxonomy)[0]
        response, content = await self.complete(
            prompt, problem_description, followup_answers
        )
        parsed = parse_json_from_llm_response(content)
        if not isinstance(parsed, dict):
            raise ValueError("Model classification must be a JSON object")
        labels = parsed.get("categories", parsed.get("labels", [])) or []
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, list):
            raise ValueError("Model labels must be a list")
        questions = parsed.get("followup_questions", parsed.get("questions", [])) or []
        if isinstance(questions, str):
            questions = [questions]
        if not isinstance(questions, list):
            raise ValueError("Model questions must be a list")
        return {
            "labels": [
                Label(label=x) if isinstance(x, str) else Label(**x) for x in labels
            ],
            "questions": [
                (
                    FollowUpQuestion(question=x)
                    if isinstance(x, str)
                    else FollowUpQuestion(
                        **{**x, "format": x.get("format") or x.get("type")}
                    )
                ).model_dump()
                for x in questions
            ],
            "likely_no_legal_problem": parsed.get("likely_no_legal_problem") is True,
            "telemetry": build_basic_telemetry(
                provider="compatible",
                instance_name=self.instance_name,
                model_name=self.model_name,
                response=response,
            ),
        }
