from app.providers.base import ClassifierProvider, load_prompt
from app.models.api_models import Label
import os
from typing import Any, Dict, List, Optional

if os.getenv("ENV") != "production":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
import json
from app.utils.backoff import run_with_backoff_async
from app.utils.json_helpers import parse_json_from_llm_response
from app.utils.logging import get_logger
from app.providers.utils import build_basic_telemetry, build_messages
from app.providers.openai import build_async_openai_client

DEFAULT_MISTRAL_MODEL = "mistralai/mistral-small-3.2-24b-instruct"


logger = get_logger(__name__)


class MistralProvider(ClassifierProvider):
    """Classifier provider that accesses Mistral via OpenRouter-compatible API."""

    def __init__(self):
        """Initialize the Mistral provider and client from env credentials."""
        super().__init__("mistral")
        self.model_name = (
            os.getenv("OPENAI_MISTRAL_MODEL")
            or os.getenv("MISTRAL_MODEL")
            or DEFAULT_MISTRAL_MODEL
        ).strip()
        self.client = build_async_openai_client(
            api_key_env="OPENROUTER_API_KEY",
            api_key_fallback_env="OPENAI_API_KEY",
            base_url_env="OPENAI_BASE_URL_MISTRAL",
            base_url_fallback_env=(
                "" if os.getenv("OPENROUTER_API_KEY") else "OPENAI_BASE_URL"
            ),
            default_base_url=(
                "https://openrouter.ai/api/v1"
                if os.getenv("OPENROUTER_API_KEY")
                else None
            ),
            max_retries=0,
        )

    async def classify(
        self,
        problem_description: str,
        taxonomy: Any,
        custom_prompt: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        followup_answers: Optional[List[Any]] = None,
    ) -> Dict[str, List[Any]]:
        """Classify a description using Mistral, expecting JSON output.

        Args:
          problem_description: The input text to classify.
          taxonomy: The taxonomy DataFrame for prompt context.
          custom_prompt: Optional fully-formed system prompt to use.
          reasoning_effort: Unused. Present for API compatibility.
          followup_answers: Optional list of FollowUpAnswer objects for multi-turn refinement.

        Returns:
          Dict containing keys "labels" and "questions". List elements may be
          `Label` objects or plain dicts.
        """
        last_response: Optional[Any] = None

        # reasoning_effort accepted for API compatibility but not used by Mistral
        if custom_prompt:
            prompt = custom_prompt
        else:
            prompt, _ = load_prompt(self.provider_type, taxonomy)

        try:
            # Use async client with backoff
            response = await run_with_backoff_async(
                self.client.chat.completions.create,
                model=self.model_name,
                messages=build_messages(prompt, problem_description, followup_answers),
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            last_response = response

            try:
                content = response.choices[0].message.content
                # Use robust JSON parsing that handles fenced code blocks and common LLM JSON errors
                parsed_response = parse_json_from_llm_response(content)

                # Ensure parsed_response is a dict; if it's a list, treat as error
                if not isinstance(parsed_response, dict):
                    logger.warning(
                        f"[MistralProvider] Expected dict but got {type(parsed_response).__name__} from parse_json_from_llm_response"
                    )
                    parsed_response = {}

                labels = []
                questions = []
                likely_no_legal_problem = parsed_response.get(
                    "likely_no_legal_problem", False
                )

                if not likely_no_legal_problem:
                    if parsed_response.get("categories") is not None:
                        labels.extend(
                            [
                                Label(label=cat, confidence=1.0)
                                for cat in parsed_response.get("categories", [])
                            ]
                        )
                    if parsed_response.get("labels") is not None:
                        for item in parsed_response.get("labels", []):
                            if isinstance(item, str):
                                labels.append(Label(label=item, confidence=1.0))
                            elif isinstance(item, dict) and item.get("label"):
                                labels.append(
                                    Label(
                                        label=item.get("label"),
                                        confidence=item.get("confidence") or 1.0,
                                    )
                                )
                    if parsed_response.get("followup_questions") is not None:
                        questions.extend(
                            [
                                q
                                for q in parsed_response.get("followup_questions", [])
                                if isinstance(q, dict) and q.get("question")
                            ]
                        )
                    if parsed_response.get("questions") is not None:
                        questions.extend(
                            [
                                q if isinstance(q, dict) else {"question": str(q)}
                                for q in parsed_response.get("questions", [])
                                if (isinstance(q, dict) and q.get("question"))
                                or isinstance(q, str)
                            ]
                        )

                telemetry_payload = build_basic_telemetry(
                    provider=self.provider_type,
                    instance_name=self.instance_name,
                    model_name=self.model_name,
                    response=last_response,
                    raw_content=content,
                )
                return {
                    "labels": labels,
                    "questions": questions,
                    "likely_no_legal_problem": likely_no_legal_problem,
                    "telemetry": telemetry_payload,
                }
            except (json.JSONDecodeError, ValueError) as e:
                error_message = (
                    f"Invalid JSON response from Mistral ({type(e).__name__})"
                )
                logger.error(error_message)
                telemetry_payload = build_basic_telemetry(
                    provider=self.provider_type,
                    instance_name=self.instance_name,
                    model_name=self.model_name,
                    response=last_response,
                    raw_content=content,
                )
                return {
                    "labels": [],
                    "questions": [],
                    "error": error_message,
                    "telemetry": telemetry_payload,
                }
        except Exception as e:
            error_message = (
                f"An unexpected error occurred with Mistral: {type(e).__name__}"
            )
            logger.error(error_message)
            rate_limited = any(
                s in str(e).lower() for s in ["rate limit", "429", "too many requests"]
            )
            telemetry_payload = build_basic_telemetry(
                provider=self.provider_type,
                instance_name=self.instance_name,
                model_name=self.model_name,
                response=last_response,
                raw_content=None,
            )
            result = {
                "labels": [],
                "questions": [],
                "error": error_message,
                "telemetry": telemetry_payload,
            }
            if rate_limited:
                result["rate_limited"] = True
            return result
