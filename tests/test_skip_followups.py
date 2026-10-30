"""Tests for skip_followups feature across the API and providers."""

import pytest
import os
from unittest.mock import patch, AsyncMock

# Set up test environment before importing app modules
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import load_prompt
from app.models.api_models import ClassificationRequest, ClassificationResponse

# no pandas; use simple sequences for taxonomy


# =============================================================================
# Tests for load_prompt with skip_followups
# =============================================================================


class TestLoadPromptSkipFollowups:
    """Tests for the load_prompt function with skip_followups parameter."""

    @pytest.fixture
    def sample_taxonomy(self):
        """Create a sample taxonomy as a sequence of dicts for testing."""
        return [
            {"Category": "Family Law"},
            {"Category": "Criminal Law"},
            {"Category": "Civil Law"},
        ]

    def test_load_prompt_default_uses_regular_prompt(self, sample_taxonomy):
        """Test that skip_followups=False uses the default prompt."""
        final_prompt, _ = load_prompt("default", sample_taxonomy, skip_followups=False)

        # The default prompt should contain follow-up question guidelines
        assert "FOLLOW-UP QUESTIONS GUIDELINES" in final_prompt
        assert "Limit to 3 questions" in final_prompt

    def test_load_prompt_skip_followups_uses_no_followups_prompt(self, sample_taxonomy):
        """Test that skip_followups=True uses the no-followups prompt."""
        final_prompt, _ = load_prompt("default", sample_taxonomy, skip_followups=True)

        # The no-followups prompt should NOT contain follow-up question guidelines
        assert "FOLLOW-UP QUESTIONS GUIDELINES" not in final_prompt
        # But should still contain classification instructions
        assert "CLASSIFICATION APPROACH" in final_prompt
        assert "NO LEGAL PROBLEM" in final_prompt

    def test_load_prompt_skip_followups_prompt_has_correct_format(
        self, sample_taxonomy
    ):
        """Test that the no-followups prompt expects only categories in response."""
        final_prompt, _ = load_prompt("default", sample_taxonomy, skip_followups=True)

        # Should have the simple format without followup_questions
        assert (
            '{ "categories": ["<Category Name 1>", "<Category Name 2>"], "likely_no_legal_problem": false }'
            in final_prompt
        )
        # Should NOT have the format with followup_questions
        assert '"followup_questions"' not in final_prompt

    def test_load_prompt_skip_followups_includes_taxonomy(self, sample_taxonomy):
        """Test that the no-followups prompt includes the taxonomy."""
        final_prompt, _ = load_prompt("default", sample_taxonomy, skip_followups=True)

        # Taxonomy should be injected
        assert "Family Law" in final_prompt
        assert "Criminal Law" in final_prompt
        assert "Civil Law" in final_prompt

    def test_load_prompt_fallback_to_default_no_followups(self, sample_taxonomy):
        """Test that unknown provider falls back to default_no_followups.txt."""
        # Using a non-existent provider type should fall back to default_no_followups.txt
        final_prompt, _ = load_prompt(
            "nonexistent_provider", sample_taxonomy, skip_followups=True
        )

        # Should still work with the default no-followups prompt
        assert "CLASSIFICATION APPROACH" in final_prompt
        assert "FOLLOW-UP QUESTIONS GUIDELINES" not in final_prompt


# =============================================================================
# Tests for ClassificationRequest model with skip_followups
# =============================================================================


class TestClassificationRequestModel:
    """Tests for the ClassificationRequest model with skip_followups."""

    def test_skip_followups_default_is_false(self):
        """Test that skip_followups defaults to False."""
        request = ClassificationRequest(problem_description="Test problem")
        assert request.skip_followups is False

    def test_skip_followups_can_be_set_to_true(self):
        """Test that skip_followups can be set to True."""
        request = ClassificationRequest(
            problem_description="Test problem", skip_followups=True
        )
        assert request.skip_followups is True

    def test_skip_followups_works_with_text_alias(self):
        """Test that skip_followups works when using the 'text' alias."""
        # The API accepts 'text' as an alias for 'problem_description'
        request = ClassificationRequest(text="Test problem", skip_followups=True)
        assert request.skip_followups is True
        assert request.problem_description == "Test problem"

    def test_skip_followups_combines_with_other_options(self):
        """Test that skip_followups works with other request options."""
        request = ClassificationRequest(
            problem_description="Test problem",
            skip_followups=True,
            decision_mode="first",
            include_debug_details=True,
            taxonomy_name="list",
        )
        assert request.skip_followups is True
        assert request.decision_mode == "first"
        assert request.include_debug_details is True
        assert request.taxonomy_name == "list"

    def test_accepts_conversation_id(self):
        """Test that ClassificationRequest accepts an optional conversation_id."""
        request = ClassificationRequest(
            problem_description="Test problem", conversation_id="conv-1"
        )
        assert request.conversation_id == "conv-1"


# =============================================================================
# Tests for API endpoint with skip_followups
# =============================================================================

client = TestClient(app)


@pytest.fixture(autouse=True)
def set_dev_mode():
    """Set dev mode to bypass auth for API tests."""
    os.environ["ENV"] = "dev"
    yield
    os.environ.pop("ENV", None)


class TestAPISkipFollowups:
    """Tests for the /api/classify endpoint with skip_followups."""

    @patch("app.services.classification_service.ClassificationService.classify")
    @pytest.mark.asyncio
    async def test_api_accepts_skip_followups_parameter(self, mock_classify):
        """Test that the API accepts the skip_followups parameter."""
        # Setup mock to return a proper ClassificationResponse
        mock_classify.return_value = ClassificationResponse(
            labels=[],
            follow_up_questions=[],
            likely_no_legal_problem={
                "value": False,
                "weighted_result": 0.0,
                "disagreement_score": 0.0,
            },
        )

        response = client.post(
            "/api/classify",
            json={
                "text": "I have a legal problem",
                "skip_followups": True,
                "enabled_models": ["keyword"],
            },
        )

        # Should be accepted (not a validation error)
        assert response.status_code == 200

    @patch("app.services.classification_service.ClassificationService.classify")
    @pytest.mark.asyncio
    async def test_api_accepts_conversation_id(self, mock_classify):
        """Test that the API accepts the conversation_id parameter and passes through."""
        mock_classify.return_value = ClassificationResponse(
            labels=[],
            follow_up_questions=[],
            likely_no_legal_problem={
                "value": False,
                "weighted_result": 0.0,
                "disagreement_score": 0.0,
            },
        )

        response = client.post(
            "/api/classify",
            json={
                "text": "I have a legal problem",
                "conversation_id": "conv-api-1",
                "enabled_models": ["keyword"],
            },
        )

        assert response.status_code == 200

    @patch("app.services.classification_service.ClassificationService.classify")
    def test_api_skip_followups_false_by_default(self, mock_classify):
        """Test that skip_followups is False by default in API requests."""
        mock_response = ClassificationResponse(
            labels=[],
            follow_up_questions=[],
            likely_no_legal_problem={
                "value": False,
                "weighted_result": 0.0,
                "disagreement_score": 0.0,
            },
        )
        mock_classify.return_value = mock_response

        response = client.post(
            "/api/classify",
            json={"text": "I have a legal problem", "enabled_models": ["keyword"]},
        )

        assert response.status_code == 200
        # Verify the request was made (classify was called)
        mock_classify.assert_called_once()
        # Check the request object passed to classify
        call_args = mock_classify.call_args
        request_arg = call_args[0][0]  # First positional argument
        assert request_arg.skip_followups is False

    @patch("app.services.classification_service.ClassificationService.classify")
    def test_api_skip_followups_true_passed_to_service(self, mock_classify):
        """Test that skip_followups=True is passed to the classification service."""
        mock_response = ClassificationResponse(
            labels=[],
            follow_up_questions=[],
            likely_no_legal_problem={
                "value": False,
                "weighted_result": 0.0,
                "disagreement_score": 0.0,
            },
        )
        mock_classify.return_value = mock_response

        response = client.post(
            "/api/classify",
            json={
                "text": "I have a legal problem",
                "skip_followups": True,
                "enabled_models": ["keyword"],
            },
        )

        assert response.status_code == 200
        # Check the request object passed to classify
        call_args = mock_classify.call_args
        request_arg = call_args[0][0]  # First positional argument
        assert request_arg.skip_followups is True


# =============================================================================
# Integration-style tests for skip_followups feature
# =============================================================================


class TestSkipFollowupsIntegration:
    """Integration-style tests for the skip_followups feature."""

    @pytest.mark.asyncio
    async def test_full_flow_with_skip_followups(self):
        """Test the complete flow with skip_followups=True using mocked providers."""
        from app.services.classification_service import ClassificationService
        from app.models.api_models import ClassificationRequest

        service = ClassificationService(enabled_providers_override=[])

        # Mock provider results
        results_with_providers = [
            (
                "provider1",
                {
                    "labels": [{"label": "Family Law", "confidence": 0.9}],
                    "questions": [
                        {
                            "question": "What custody arrangement?",
                            "type": "radio",
                            "options": ["Joint", "Sole"],
                        }
                    ],
                    "likely_no_legal_problem": False,
                },
            ),
            (
                "provider2",
                {
                    "labels": [{"label": "Family Law", "confidence": 0.85}],
                    "questions": [
                        {"question": "Are children involved?", "type": "yesno"}
                    ],
                    "likely_no_legal_problem": False,
                },
            ),
        ]

        # Act with skip_followups=True
        response = await service._get_voted_results(
            results_with_providers, include_debug_details=False, skip_followups=True
        )

        # Assert
        assert response.follow_up_questions == []
        assert len(response.labels) == 1
        assert response.labels[0].label == "Family Law"
        assert response.likely_no_legal_problem.value is False

    @pytest.mark.asyncio
    async def test_full_flow_without_skip_followups(self):
        """Test the complete flow with skip_followups=False (default) using mocked providers."""
        from app.services.classification_service import ClassificationService
        from app.models.api_models import FollowUpQuestion

        service = ClassificationService(enabled_providers_override=[])

        with patch(
            "app.services.classification_service.ClassificationService._semantically_merge_questions",
            new_callable=AsyncMock,
        ) as mock_merge:
            mock_merge.return_value = [
                FollowUpQuestion(
                    question="What custody arrangement?",
                    format="radio",
                    options=["Joint", "Sole"],
                ),
            ]

            # Note: We need >3 questions to trigger the semantic merge (optimization skips it for ≤3)
            results_with_providers = [
                (
                    "provider1",
                    {
                        "labels": [{"label": "Family Law", "confidence": 0.9}],
                        "questions": [
                            {
                                "question": "What custody arrangement?",
                                "type": "radio",
                                "options": ["Joint", "Sole"],
                            },
                            {"question": "What state are you in?", "type": "text"},
                        ],
                        "likely_no_legal_problem": False,
                    },
                ),
                (
                    "provider2",
                    {
                        "labels": [{"label": "Family Law", "confidence": 0.85}],
                        "questions": [
                            {
                                "question": "Do you have children?",
                                "type": "radio",
                                "options": ["Yes", "No"],
                            },
                            {
                                "question": "Are you married?",
                                "type": "radio",
                                "options": ["Yes", "No"],
                            },
                        ],
                        "likely_no_legal_problem": False,
                    },
                ),
            ]

            # Act with skip_followups=False (default)
            response = await service._get_voted_results(
                results_with_providers,
                include_debug_details=False,
                skip_followups=False,
            )

            # Assert - follow_up_questions should be present and include the merged question
            assert len(response.follow_up_questions) == 1
            assert (
                response.follow_up_questions[0].question == "What custody arrangement?"
            )
            mock_merge.assert_called_once()
