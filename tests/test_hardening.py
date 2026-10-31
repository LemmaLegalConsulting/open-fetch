"""Regressions from the full-repository privacy and correctness review."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.identity import (
    CredentialScope,
    current_scope,
    current_client,
    credential_registry,
)
from app.models.api_models import (
    ClassificationRequest,
    ClassificationResponse,
    FollowUpQuestion,
)
from app.services.classification_service import ClassificationService
from app.services.screening_protocols import (
    resolve_narrative,
    validate_llm_resolutions,
    narrative_results,
)


@pytest.mark.parametrize(
    "mapping", ["school/production/key=secret", "alpha=secret,beta=secret"]
)
def test_invalid_legacy_partitions_fail_closed(monkeypatch, mapping):
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", mapping)
    response = TestClient(app).post(
        "/api/classify",
        headers={"Authorization": "Bearer secret"},
        json={"text": "Synthetic"},
    )
    assert response.status_code == 503


def test_configured_credentials_disable_dev_bypass(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("API_TOKENS", "synthetic")
    assert (
        TestClient(app).post("/api/classify", json={"text": "Synthetic"}).status_code
        == 403
    )


def test_duplicate_registry_keys_rejected(monkeypatch):
    monkeypatch.setenv("API_CREDENTIALS", '{"key":{},"key":{}}')
    with pytest.raises(ValueError, match="Duplicate"):
        credential_registry()


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "x" * 50001),
        ("conversation_id", "x" * 129),
        ("conversation_id", "someone@example.org"),
        ("taxonomy_name", "missing"),
    ],
)
def test_request_contract_rejects_invalid_inputs(monkeypatch, field, value):
    monkeypatch.setenv("API_TOKENS", "synthetic")
    response = TestClient(app).post(
        "/api/classify",
        headers={"Authorization": "Bearer synthetic"},
        json={"text": "Synthetic", field: value},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [ValueError("SYNTHETIC PRIVATE SENTINEL"), "{}", '{"merged_questions":[]}'],
)
async def test_merge_failure_preserves_questions_and_sanitizes_logs(
    monkeypatch, caplog, content
):
    service = ClassificationService()
    service.offline_mode = False
    if isinstance(content, Exception):
        call = AsyncMock(side_effect=content)
    else:
        call = AsyncMock(return_value=("synthetic", {}, content))
    monkeypatch.setattr(service, "_call_semantic_merge_model", call)
    questions = [FollowUpQuestion(question="Synthetic question?")]
    result = await service._semantically_merge_questions(
        questions, request_span=None, request_id="synthetic", taxonomy_name="default"
    )
    assert result == questions
    assert "SYNTHETIC PRIVATE SENTINEL" not in caplog.text


@pytest.mark.asyncio
async def test_failed_providers_do_not_vote():
    service = ClassificationService()
    response = await service._get_voted_results(
        [
            (
                "working",
                {"labels": [], "questions": [], "likely_no_legal_problem": True},
            ),
            ("failed", {"error": "synthetic"}),
            ("raised", RuntimeError("synthetic")),
        ],
        False,
        request_span=None,
        request_id="synthetic",
        taxonomy_name="default",
        skip_followups=True,
    )
    assert response.likely_no_legal_problem.weighted_result == 1


def test_cache_scope_and_rendered_prompt_distinguish_results():
    service = ClassificationService()
    keys = []
    for name in ["one", "two"]:
        token = current_scope.set(CredentialScope("school", "test", name))
        try:
            keys.append(
                service._get_cache_key("SYNTHETIC PRIVATE", "model", "taxonomy-one")
            )
        finally:
            current_scope.reset(token)
    keys.append(service._get_cache_key("SYNTHETIC PRIVATE", "model", "taxonomy-two"))
    assert len(set(keys)) == 3
    assert all("SYNTHETIC PRIVATE" not in key for key in keys)


@pytest.mark.parametrize(
    "text",
    [
        "My spouse says no matter what I do the threats continue",
        "My husband hurts me; no one knows about the physical abuse",
        "Mi esposo: No sé qué hacer, las amenazas siguen",
    ],
)
def test_ambiguous_negation_does_not_rule_out_safety(text):
    resolutions = resolve_narrative(text, ["Family Law > Divorce"])
    assert not any(
        r.status == "ruled_out"
        and r.choice_id in {"controlling_or_threatening", "physical_or_sexual_harm"}
        for r in resolutions
    )


def test_model_cannot_rule_out_safety_without_explicit_negative_fact():
    from app.services.screening_protocols import PROTOCOL_BY_ID

    protocol = PROTOCOL_BY_ID["family_safety.v1"]
    rows = [
        dict(
            protocol_id=protocol.protocol_id,
            question_id=protocol.question_id,
            choice_id="immediate_danger",
            status="ruled_out",
        )
    ]
    assert validate_llm_resolutions(rows, "A custody dispute", []) == []


def test_all_disclosed_routes_survive_same_priority():
    from app.models.api_models import ScreeningResolution

    rows = [
        ScreeningResolution(
            protocol_id="synthetic",
            protocol_version=1,
            question_id="q",
            choice_id=str(i),
            status="disclosed",
            source="narrative_keyword",
            risk_level="elevated",
            mandatory_category_ids=[str(i)],
        )
        for i in range(2)
    ]
    assert {r.choice_id for r in narrative_results(rows)} == {"0", "1"}


@pytest.mark.asyncio
async def test_responses_retry_extracts_text_and_logs_no_content(monkeypatch, caplog):
    from app.providers.openai import GPT52Provider

    monkeypatch.setenv("OPENAI_GPT_5_2_MODEL", "custom-azure-deployment")
    monkeypatch.setattr(
        GPT52Provider,
        "_get_client",
        lambda self: SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(
                    side_effect=[
                        TypeError("synthetic"),
                        SimpleNamespace(
                            output_text='{"categories":["SYNTHETIC PRIVATE SENTINEL"]}'
                        ),
                    ]
                )
            )
        ),
    )
    provider = GPT52Provider()
    with caplog.at_level("INFO"):
        result = await provider.classify("Synthetic", [], custom_prompt="Return JSON")
    assert result["labels"][0]["label"] == "SYNTHETIC PRIVATE SENTINEL"
    assert provider.instance_name == "gpt-5.2"
    assert "SYNTHETIC PRIVATE SENTINEL" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_auth_context_reset_in_same_task(monkeypatch, fail):
    from app.main import verify_token
    from app.core.identity import http_scope_enforced
    from fastapi.security import HTTPAuthorizationCredentials

    monkeypatch.setenv("API_TOKENS", "synthetic")
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "school=synthetic")
    previous = (current_scope.get(), current_client.get(), http_scope_enforced.get())
    from starlette.requests import Request

    generator = verify_token(
        Request(
            {
                "type": "http",
                "app": app,
                "path": "/api/classify",
                "headers": [],
                "client": ("test", 0),
            }
        ),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="synthetic"),
    )
    assert await anext(generator) == "synthetic"
    assert current_client.get() == "school" and http_scope_enforced.get()
    if fail:
        with pytest.raises(RuntimeError):
            await generator.athrow(RuntimeError("synthetic handler error"))
    else:
        await generator.aclose()
    assert (
        current_scope.get(),
        current_client.get(),
        http_scope_enforced.get(),
    ) == previous


def test_content_permission_never_includes_sdk_error_bodies():
    from unittest.mock import MagicMock
    from app.telemetry.opentelemetry_client import _annotate_error

    span = MagicMock()
    _annotate_error(span, ValueError("SYNTHETIC SDK SECRET"))
    assert "SYNTHETIC SDK SECRET" not in str(span.mock_calls)
    span.record_exception.assert_not_called()


@pytest.mark.parametrize(
    "text,protocol,choice",
    [
        (
            "My husband scares me and I don't feel safe now",
            "family_safety.v1",
            "immediate_danger",
        ),
        ("My wife says I'm not currently safe", "family_safety.v1", "immediate_danger"),
        (
            "The older adult doesn't have access to her money",
            "elder_exploitation.v1",
            "basic_needs_or_safety",
        ),
        (
            "El adulto mayor no tiene acceso a su dinero",
            "elder_exploitation.v1",
            "basic_needs_or_safety",
        ),
        (
            "No one noticed money was transferred out of her account",
            "elder_exploitation.v1",
            "taking_assets",
        ),
        (
            "I have no lawyer for my deportation case",
            "immigration_consequences.v1",
            "removal_or_deportation",
        ),
        (
            "The police arrested me without a warrant and now I face charges",
            "police_government_claim.v1",
            "criminal_defense_only",
        ),
        (
            "My work injury was caused by the other driver, not my employer",
            "work_injury_third_party.v1",
            "yes",
        ),
    ],
)
def test_negated_safety_statements_remain_unresolved(text, protocol, choice):
    from app.services.screening_protocols import PROTOCOL_BY_ID, _suppressed_choice_ids

    assert choice not in _suppressed_choice_ids(PROTOCOL_BY_ID[protocol], text)


@pytest.mark.asyncio
async def test_single_negative_vote_cannot_screen_out_three_member_ensemble():
    service = ClassificationService()
    rows = [
        ("one", {"labels": [], "questions": [], "likely_no_legal_problem": True}),
        ("two", {"labels": [{"label": "Family Law"}], "questions": []}),
        ("three", RuntimeError("synthetic")),
    ]
    result = await service._get_voted_results(rows, False, skip_followups=True)
    assert result.likely_no_legal_problem.value is False


@pytest.mark.asyncio
async def test_offline_ensemble_never_resolves_online_auxiliary(monkeypatch):
    monkeypatch.setenv("FETCH_AUXILIARY_MODEL", "haiku")
    service = ClassificationService()
    assert (
        await service._resolve_screening_options_with_llm("A custody dispute", []) == []
    )


@pytest.mark.asyncio
async def test_total_provider_failure_is_unavailable(monkeypatch):
    from app.services.classification_service import ClassificationUnavailable

    service = ClassificationService()
    monkeypatch.setattr(
        service.providers[0],
        "classify",
        AsyncMock(side_effect=RuntimeError("synthetic")),
    )
    with pytest.raises(ClassificationUnavailable):
        await service.classify(
            ClassificationRequest(text="Synthetic", skip_followups=True)
        )
