import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.api_models import ClassificationRequest, Label, ScreeningAnswer
from app.providers.base import LLMClassifierProvider
from app.services.classification_service import ClassificationService
from app.services.screening_protocols import (
    CATEGORY_LABELS,
    PROTOCOLS,
    build_effective_categories,
    build_mandatory_categories,
    evaluate_answers,
    merge_narrative_resolutions,
    narrative_results,
    resolve_narrative,
    select_questions,
    validate_llm_resolutions,
)


def _answer(protocol, choice_id):
    return ScreeningAnswer(
        protocol_id=protocol.protocol_id,
        question_id=protocol.question_id,
        choice_id=choice_id,
    )


@pytest.mark.parametrize(
    "protocol,choice",
    [(protocol, choice) for protocol in PROTOCOLS for choice in protocol.choices],
)
def test_every_choice_produces_exact_configured_outcome(protocol, choice):
    result = evaluate_answers([_answer(protocol, choice.id)])[0]

    assert result.protocol_id == protocol.protocol_id
    assert result.protocol_version == protocol.version
    assert result.question_id == protocol.question_id
    assert result.choice_id == choice.id
    assert result.risk_level == choice.outcome.risk_level
    assert result.information_trigger == choice.outcome.information_trigger
    assert result.mandatory_category_ids == list(choice.outcome.mandatory_category_ids)


def test_none_and_prefer_not_never_create_mandatory_categories():
    for protocol in PROTOCOLS:
        for choice in protocol.choices:
            if choice.id in {"none", "no", "prefer_not", "criminal_defense_only"}:
                result = evaluate_answers([_answer(protocol, choice.id)])[0]
                assert not result.mandatory_category_ids


def test_only_explicit_urgent_choices_are_urgent():
    for protocol in PROTOCOLS:
        for choice in protocol.choices:
            result = evaluate_answers([_answer(protocol, choice.id)])[0]
            assert (result.risk_level == "urgent") == (
                choice.id in {"immediate_danger", "basic_needs_or_safety"}
            )


def test_checkbox_answer_can_return_multiple_deterministic_outcomes():
    family = PROTOCOLS[0]
    answer = ScreeningAnswer(
        protocol_id=family.protocol_id,
        question_id=family.question_id,
        choice_ids=["controlling_or_threatening", "physical_or_sexual_harm"],
    )

    results = evaluate_answers([answer])

    assert [result.choice_id for result in results] == [
        "controlling_or_threatening",
        "physical_or_sexual_harm",
    ]
    assert [result.risk_level for result in results] == [
        "informational",
        "elevated",
    ]


def test_checkbox_answer_rejects_exclusive_choice_combination():
    family = PROTOCOLS[0]
    answer = ScreeningAnswer(
        protocol_id=family.protocol_id,
        question_id=family.question_id,
        choice_ids=["none", "physical_or_sexual_harm"],
    )

    with pytest.raises(ValueError, match="Exclusive choice"):
        evaluate_answers([answer])


def test_effective_categories_preserve_confidence_and_protocol_only_null():
    family = PROTOCOLS[0]
    result = evaluate_answers([_answer(family, "physical_or_sexual_harm")])
    mandatory = build_mandatory_categories(result)
    labels = [
        Label(label=CATEGORY_LABELS["family.restraining_orders"], confidence=1.8),
        Label(label="Family Law > Divorce/Separation", confidence=2.4),
    ]

    effective = build_effective_categories(labels, mandatory)

    assert len(effective) == 2
    restraining = effective[0]
    assert restraining.confidence == 1.8
    assert restraining.mandatory is True
    assert restraining.sources == ["model", "protocol"]
    assert effective[1].label == "Family Law > Divorce/Separation"

    protocol_only = build_effective_categories([], mandatory)[0]
    assert protocol_only.confidence is None
    assert protocol_only.mandatory is True
    assert protocol_only.sources == ["protocol"]


def test_mandatory_routes_are_not_subject_to_three_label_limit():
    employment = next(
        protocol
        for protocol in PROTOCOLS
        if protocol.protocol_id == "employment_retaliation.v1"
    )
    results = evaluate_answers([_answer(employment, "yes_fired")])
    mandatory = build_mandatory_categories(results)
    labels = [
        Label(label=f"Unrelated > Category {number}", confidence=3 - number / 10)
        for number in range(3)
    ]

    effective = build_effective_categories(labels, mandatory)

    assert len(effective) == 5
    assert {category.id for category in effective if category.mandatory} == {
        "employment.whistleblowers",
        "employment.wrongful_discharge",
    }
    assert all(category.confidence is None for category in effective[3:])


def test_questions_are_bilingual_and_expose_translation_invariants():
    english = select_questions("I am getting divorced.", [], [], "en")
    spanish = select_questions("Me estoy divorciando.", [], [], "es")

    assert english[0].protocol_id == spanish[0].protocol_id == "family_safety.v1"
    assert english[0].format == spanish[0].format == "checkbox"
    assert english[0].question_id == spanish[0].question_id
    assert english[0].translation_key == spanish[0].translation_key
    assert english[0].question == english[0].translations.en
    assert spanish[0].question == spanish[0].translations.es
    assert [choice.id for choice in english[0].choices] == [
        choice.id for choice in spanish[0].choices
    ]
    assert all(
        choice.resolution_status == "unresolved" for choice in english[0].choices
    )


def test_priority_caps_protocol_questions_and_completed_protocol_is_suppressed():
    labels = [
        "Family Law > Divorce/Separation",
        "Wills & Trusts > Elder Abuse",
        "International Law > Deportation",
        "General Litigation > Actions Against Police",
        "Labor & Employment > Wrongful Discharge - Employee",
        "Workers' Comp > Third Party Litigation",
    ]

    questions = select_questions("", labels, [], limit=3)
    assert [question.protocol_id for question in questions] == [
        "family_safety.v1",
        "elder_exploitation.v1",
        "immigration_consequences.v1",
    ]

    completed = select_questions(
        "I am getting divorced.", labels[:1], ["family_safety.v1"]
    )
    assert completed == []


@pytest.mark.parametrize(
    "text,labels,protocol_id,disclosed_choice_id,question_remains",
    [
        (
            "I am divorcing my husband because he strangled me.",
            [],
            "family_safety.v1",
            "physical_or_sexual_harm",
            True,
        ),
        (
            "The caregiver stole my elderly mother's money.",
            [],
            "elder_exploitation.v1",
            "taking_assets",
            True,
        ),
        (
            "I received an ICE deportation notice.",
            [],
            "immigration_consequences.v1",
            "removal_or_deportation",
            True,
        ),
        (
            "The police used excessive force during my arrest.",
            [],
            "police_government_claim.v1",
            "police_misconduct",
            True,
        ),
        (
            "My employer fired me after I reported fraud.",
            [],
            "employment_retaliation.v1",
            "yes_fired",
            False,
        ),
        (
            "A driver hit me while I was driving for work.",
            [],
            "work_injury_third_party.v1",
            "yes",
            False,
        ),
    ],
)
def test_explicit_narrative_answer_suppresses_disclosed_choice(
    text, labels, protocol_id, disclosed_choice_id, question_remains
):
    questions = {
        question.protocol_id: question
        for question in select_questions(text, labels, [])
    }
    assert (protocol_id in questions) is question_remains
    if question_remains:
        assert disclosed_choice_id not in {
            choice.id for choice in questions[protocol_id].choices
        }

    resolutions = resolve_narrative(text, labels)
    assert any(
        resolution.protocol_id == protocol_id
        and resolution.choice_id == disclosed_choice_id
        and resolution.status == "disclosed"
        for resolution in resolutions
    )


def test_trying_to_control_me_resolves_and_removes_controlling_choice():
    text = "I need a divorce. My husband is always trying to control me."

    question = next(
        question
        for question in select_questions(text, [], [])
        if question.protocol_id == "family_safety.v1"
    )
    choice_ids = {choice.id for choice in question.choices}

    assert "controlling_or_threatening" not in choice_ids
    assert "none" not in choice_ids
    assert "physical_or_sexual_harm" in choice_ids
    assert "immediate_danger" in choice_ids

    resolutions = resolve_narrative(text, [])
    disclosed = next(
        resolution
        for resolution in resolutions
        if resolution.protocol_id == "family_safety.v1"
        and resolution.choice_id == "controlling_or_threatening"
    )
    assert disclosed.status == "disclosed"
    assert disclosed.information_trigger == "domestic_violence_safety_resources"


def test_llm_resolution_supplements_keywords_for_unmatched_paraphrase():
    text = "I need a divorce. My husband dictates every aspect of my life."
    raw = [
        {
            "protocol_id": "family_safety.v1",
            "question_id": "family_safety.behavior.v1",
            "choice_id": "controlling_or_threatening",
            "status": "disclosed",
        }
    ]

    llm_resolutions = validate_llm_resolutions(raw, text, [])
    keyword_resolutions = resolve_narrative(text, [])
    merged = merge_narrative_resolutions(keyword_resolutions, llm_resolutions)
    question = next(
        question
        for question in select_questions(text, [], [], resolutions=merged)
        if question.protocol_id == "family_safety.v1"
    )

    assert keyword_resolutions == []
    assert llm_resolutions[0].source == "narrative_llm"
    assert (
        llm_resolutions[0].information_trigger == "domestic_violence_safety_resources"
    )
    assert "controlling_or_threatening" not in {
        choice.id for choice in question.choices
    }
    assert "none" not in {choice.id for choice in question.choices}


def test_llm_resolution_rejects_ineligible_or_invented_ids():
    raw = [
        {
            "protocol_id": "family_safety.v1",
            "question_id": "family_safety.behavior.v1",
            "choice_id": "invented_choice",
            "status": "disclosed",
        },
        {
            "protocol_id": "employment_retaliation.v1",
            "question_id": "employment_retaliation.report.v1",
            "choice_id": "yes_fired",
            "status": "disclosed",
        },
    ]

    assert validate_llm_resolutions(raw, "I need a divorce from my husband.", []) == []


@pytest.mark.asyncio
async def test_lightweight_llm_checker_returns_validated_resolutions(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    service = ClassificationService(enabled_providers_override=[])
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"resolutions":[{"protocol_id":"family_safety.v1",'
                            '"question_id":"family_safety.behavior.v1",'
                            '"choice_id":"controlling_or_threatening",'
                            '"status":"disclosed"}]}'
                        )
                    )
                )
            ]
        )
    )
    service.offline_mode = False  # Explicitly exercise a mocked online extraction.
    service.openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    resolutions = await service._resolve_screening_options_with_llm(
        "I need a divorce. My husband dictates every aspect of my life.",
        ["Family Law > Divorce/Separation"],
    )

    assert len(resolutions) == 1
    assert resolutions[0].choice_id == "controlling_or_threatening"
    assert resolutions[0].source == "narrative_llm"
    assert resolutions[0].information_trigger == "domestic_violence_safety_resources"
    assert create.await_args.kwargs["model"] == "gpt-4.1-nano"


@pytest.mark.parametrize(
    "text,labels,protocol_id,suppressed_choice_ids",
    [
        (
            "We have a custody dispute, but everyone is safe right now and there is no immediate danger.",
            [],
            "family_safety.v1",
            {"immediate_danger"},
        ),
        (
            "The older adult still has access to medication and care.",
            ["Wills & Trusts > Elder Abuse"],
            "elder_exploitation.v1",
            {"basic_needs_or_safety"},
        ),
        (
            "I have a visa issue, but I have no ICE deportation notice.",
            [],
            "immigration_consequences.v1",
            {"removal_or_deportation"},
        ),
        (
            "The police detained me, but there are no criminal charges.",
            [],
            "police_government_claim.v1",
            {"criminal_defense_only", "both_criminal_and_misconduct"},
        ),
        (
            "I was fired yesterday in an employment dispute.",
            ["Labor & Employment > Wrongful Discharge - Employee"],
            "employment_retaliation.v1",
            {"yes_not_fired"},
        ),
    ],
)
def test_explicit_narrative_facts_suppress_individual_choices(
    text, labels, protocol_id, suppressed_choice_ids
):
    questions = select_questions(text, labels, [])
    question = next(
        question for question in questions if question.protocol_id == protocol_id
    )
    returned_choice_ids = {choice.id for choice in question.choices}

    assert returned_choice_ids.isdisjoint(suppressed_choice_ids)
    assert "prefer_not" in returned_choice_ids


def test_resolved_work_injury_cause_suppresses_question_when_nothing_remains():
    questions = select_questions(
        "My work injury was caused by my employer.",
        ["Workers' Comp > General"],
        [],
    )

    assert all(
        question.protocol_id != "work_injury_third_party.v1" for question in questions
    )


def test_negated_disclosure_does_not_suppress_whole_question():
    questions = select_questions(
        "I am divorcing my husband. He has never threatened or hurt me.", [], []
    )

    assert questions[0].protocol_id == "family_safety.v1"


def test_narrative_resolutions_expose_disclosed_and_ruled_out_outcomes():
    text = "My employer fired me after I reported fraud."
    resolutions = resolve_narrative(
        text, ["Labor & Employment > Wrongful Discharge - Employee"]
    )

    disclosed = next(
        resolution for resolution in resolutions if resolution.status == "disclosed"
    )
    ruled_out = next(
        resolution
        for resolution in resolutions
        if resolution.status == "ruled_out" and resolution.choice_id == "yes_not_fired"
    )
    assert disclosed.choice_id == "yes_fired"
    assert disclosed.risk_level == "elevated"
    assert disclosed.information_trigger == "employment_retaliation_time_sensitive"
    assert disclosed.mandatory_category_ids == [
        "employment.whistleblowers",
        "employment.wrongful_discharge",
    ]
    assert ruled_out.choice_id == "yes_not_fired"
    assert ruled_out.risk_level is None
    assert ruled_out.information_trigger is None

    result = narrative_results(resolutions)[0]
    assert result.answer_source == "narrative"
    assert result.choice_id == "yes_fired"
    assert result.information_trigger == "employment_retaliation_time_sensitive"


@pytest.mark.parametrize(
    "text",
    [
        "My name is García.",
        "I speak Spanish and need help with rent.",
        "I am a woman with a disability and my landlord will not repair the heat.",
        "I was born in Mexico and have a wage dispute.",
    ],
)
def test_demographic_characteristics_do_not_activate_protocols(text):
    assert select_questions(text, [], []) == []


def test_display_text_cannot_change_deterministic_evaluation():
    protocol = PROTOCOLS[0]
    answer = ScreeningAnswer(
        protocol_id=protocol.protocol_id,
        question_id=protocol.question_id,
        choice_id="none",
        question="Someone is in danger",
        answer="immediate danger",
    )

    result = evaluate_answers([answer])[0]
    assert result.risk_level == "none"
    assert result.information_trigger is None


def test_unknown_choice_id_is_rejected_by_api_contract(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("ENV", "dev")
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    response = client.post(
        "/api/classify",
        json={
            "text": "I am getting divorced.",
            "screening_answers": [
                {
                    "protocol_id": "family_safety.v1",
                    "question_id": "family_safety.behavior.v1",
                    "choice_id": "made_up_choice",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert "Unknown choice_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_protocol_questions_take_priority_in_shared_three_question_round(
    monkeypatch,
):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    service = ClassificationService(enabled_providers_override=[])

    class FakeLLMProvider(LLMClassifierProvider):
        def __init__(self):
            super().__init__(provider_type="openai", model_name="fake-llm")

        def _get_client(self):
            return None

        async def classify(self, *args, **kwargs):
            raise AssertionError("telemetry wrapper is mocked")

    service.providers = [FakeLLMProvider()]
    provider_result = {
        "labels": [{"label": "Family Law > Divorce/Separation", "confidence": 2.4}],
        "questions": [
            {"question": "LLM question 1", "format": "text"},
            {"question": "LLM question 2", "format": "text"},
            {"question": "LLM question 3", "format": "text"},
        ],
        "likely_no_legal_problem": False,
    }
    narrative = "I need a divorce. My husband dictates every aspect of my life."
    llm_resolutions = validate_llm_resolutions(
        [
            {
                "protocol_id": "family_safety.v1",
                "question_id": "family_safety.behavior.v1",
                "choice_id": "controlling_or_threatening",
                "status": "disclosed",
            }
        ],
        narrative,
        ["Family Law > Divorce/Separation"],
    )

    with patch.object(
        service,
        "_classify_with_telemetry",
        AsyncMock(return_value=provider_result),
    ), patch.object(
        service,
        "_resolve_screening_options_with_llm",
        AsyncMock(return_value=llm_resolutions),
    ):
        response = await service.classify(
            ClassificationRequest(
                text=narrative,
                skip_semantic_merge=True,
                enable_screening_protocols=True,
                legacy_screening=False,
            )
        )

    assert [question.protocol_id for question in response.screening_questions] == [
        "family_safety.v1"
    ]
    assert "controlling_or_threatening" not in {
        choice.id for choice in response.screening_questions[0].choices
    }
    assert [question.question for question in response.follow_up_questions] == [
        "LLM question 1",
        "LLM question 2",
    ]
    assert len(response.screening_questions) + len(response.follow_up_questions) == 3


@pytest.mark.asyncio
async def test_submitted_answer_routes_deterministically_and_refines_model(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    service = ClassificationService(enabled_providers_override=[])

    class FakeLLMProvider(LLMClassifierProvider):
        def __init__(self):
            super().__init__(provider_type="openai", model_name="fake-llm")

        def _get_client(self):
            return None

        async def classify(self, *args, **kwargs):
            raise AssertionError("telemetry wrapper is mocked")

    service.providers = [FakeLLMProvider()]

    async def provider_result(*, request, **kwargs):
        assert request.followup_answers
        rendered = request.followup_answers[0]
        assert rendered.question.startswith("Has anyone involved")
        assert rendered.answer.startswith("Physically hurt")
        return {
            "labels": [{"label": "Family Law > Divorce/Separation", "confidence": 2.4}],
            "questions": [{"question": "Unrelated model question"}],
            "likely_no_legal_problem": False,
        }

    with patch.object(
        service,
        "_classify_with_telemetry",
        AsyncMock(side_effect=provider_result),
    ):
        response = await service.classify(
            ClassificationRequest(
                text="I am getting divorced.",
                enable_screening_protocols=True,
                screening_answers=[
                    {
                        "protocol_id": "family_safety.v1",
                        "question_id": "family_safety.behavior.v1",
                        "choice_id": "physical_or_sexual_harm",
                    }
                ],
            )
        )

    assert response.screening_questions == []
    assert response.screening_results[0].risk_level == "elevated"
    assert response.information_triggers == ["domestic_violence_elevated_risk"]
    assert response.mandatory_categories[0].id == "family.restraining_orders"
    assert response.effective_categories[0].label == "Family Law > Divorce/Separation"
    assert response.effective_categories[1].confidence is None
    assert response.effective_categories[1].mandatory is True


@pytest.mark.asyncio
async def test_legacy_screening_is_default_and_precedes_llm_questions(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    service = ClassificationService(enabled_providers_override=[])

    class FakeLLMProvider(LLMClassifierProvider):
        def __init__(self):
            super().__init__(provider_type="openai", model_name="fake-llm")

        def _get_client(self):
            return None

        async def classify(self, *args, **kwargs):
            raise AssertionError("telemetry wrapper is mocked")

    service.providers = [FakeLLMProvider()]
    provider_result = {
        "labels": [{"label": "Family Law > Divorce/Separation", "confidence": 2.4}],
        "questions": [
            {"question": f"LLM question {number}", "format": "text"}
            for number in range(1, 4)
        ],
        "likely_no_legal_problem": False,
    }

    with patch.object(
        service,
        "_classify_with_telemetry",
        AsyncMock(return_value=provider_result),
    ):
        response = await service.classify(
            ClassificationRequest(
                text="I am getting divorced.", skip_semantic_merge=True
            )
        )

    assert len(response.screening_questions) == 1
    assert response.screening_questions[0].format == "checkbox"
    assert len(response.follow_up_questions) == 3
    assert (
        response.follow_up_questions[0].question
        == response.screening_questions[0].question
    )
    assert response.follow_up_questions[0].format == "checkbox"
    assert response.follow_up_questions[0].options == [
        choice.label for choice in response.screening_questions[0].choices
    ]
    assert [question.question for question in response.follow_up_questions[1:]] == [
        "LLM question 1",
        "LLM question 2",
    ]


@pytest.mark.asyncio
async def test_service_returns_narrative_warning_when_question_is_suppressed(
    monkeypatch,
):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    service = ClassificationService(enabled_providers_override=[])

    class FakeLLMProvider(LLMClassifierProvider):
        def __init__(self):
            super().__init__(provider_type="openai", model_name="fake-llm")

        def _get_client(self):
            return None

        async def classify(self, *args, **kwargs):
            raise AssertionError("telemetry wrapper is mocked")

    service.providers = [FakeLLMProvider()]
    provider_result = {
        "labels": [
            {
                "label": "Labor & Employment > Wrongful Discharge - Employee",
                "confidence": 2.1,
            }
        ],
        "questions": [],
        "likely_no_legal_problem": False,
    }

    with patch.object(
        service,
        "_classify_with_telemetry",
        AsyncMock(return_value=provider_result),
    ):
        response = await service.classify(
            ClassificationRequest(
                text="My employer fired me after I reported fraud.",
                enable_screening_protocols=True,
            )
        )

    assert response.screening_questions == []
    disclosed = next(
        resolution
        for resolution in response.screening_resolutions
        if resolution.status == "disclosed"
    )
    assert disclosed.choice_id == "yes_fired"
    assert disclosed.information_trigger == "employment_retaliation_time_sensitive"
    assert response.screening_results[0].answer_source == "narrative"
    assert response.information_triggers == ["employment_retaliation_time_sensitive"]
    assert {category.id for category in response.mandatory_categories} == {
        "employment.whistleblowers",
        "employment.wrongful_discharge",
    }
