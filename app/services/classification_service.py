import os
import time
import hashlib
from app.core.identity import current_scope, current_client
from uuid import uuid4
import json
from app.models.api_models import (
    ClassificationRequest,
    ClassificationResponse,
    Label,
    FollowUpQuestion,
    FollowUpAnswer,
    ScreeningResolution,
)
from app.services.screening_protocols import (
    answers_for_model,
    build_effective_categories,
    build_mandatory_categories,
    candidate_label_strings,
    evaluate_answers,
    llm_resolution_contracts,
    merge_narrative_resolutions,
    narrative_results,
    resolve_narrative,
    select_questions,
    validate_llm_resolutions,
    validate_answers,
)
from app.providers.base import ClassifierProvider, load_prompt
from app.providers.compatible import CompatibleProvider, configured_endpoints
from app.providers.openai import OpenAIProvider
from app.providers.gemini import GeminiProvider
from app.providers.mistral import MistralProvider
from app.providers.keyword import KeywordClassifierProvider
from app.providers.spot import SpotProvider
from app.providers.openai import GPT41NanoProvider
from app.providers.openai import GPT5Provider
from app.providers.openai import GPT52Provider
from app.providers.openai import build_async_openai_client
from app.providers.utils import extract_usage_details
from app.utils.backoff import run_with_backoff_async
from app.utils.json_helpers import parse_json_from_llm_response
from app.core.config import (
    CLASSIFIER_WEIGHTS,
    ENABLED_CLASSIFIERS,
    TAXONOMY_MAPPING,
    DECISION_MODE,
    GPT_5_REASONING_EFFORT,
    CLASSIFIER_TIMEOUT_SECONDS,
    SEMANTIC_MERGE_TIMEOUT_SECONDS,
    OPENAI_SUPPORTS_REASONING_OBJECT,
    OPENAI_SUPPORTS_REASONING_EFFORT,
    OPENAI_SDK_VERSION,
)
from app.data.list_taxonomy import (
    load_list_taxonomy_simple,
    lookup_list_code_from_title,
    convert_osb_label_to_list,
    format_list_label,
    get_list_code_to_title_mapping,
)
from app.utils.csv_helpers import read_csv_as_list_of_dicts, dedupe_and_clean_rows
import asyncio
from collections import defaultdict
from typing import List, Dict, Union, Optional, Tuple, Any
import re
from diskcache import Cache
from app.utils.logging import get_logger
import inspect
from threading import Lock
from app.telemetry import (
    finalize_provider_generation,
    finalize_request_trace,
    flush_telemetry_async,
    start_provider_generation,
    start_request_trace,
)


def _env_flag_enabled(name: str) -> bool:
    """Return True when the env var is set to a truthy value."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


logger = get_logger(__name__)

DEFAULT_LABEL_SELECTION_MAX_LABELS = 3
DEFAULT_LABEL_SELECTION_SCORE_WINDOW = 0.9

# Provider types excluded from refinement (follow-up-answer) calls. These are
# not LLMs and cannot make use of the extra conversational context, unlike
# every LLM-backed provider (openai/gpt-5, gemini, mistral, ...) which should
# remain in the vote. Keyed on ClassifierProvider.provider_type rather than
# isinstance(..., LLMClassifierProvider), because Gemini and Mistral are real
# LLMs that happen not to subclass that ABC.
FOLLOWUP_EXCLUDED_PROVIDER_TYPES = {"keyword", "spot"}


class ClassificationUnavailable(RuntimeError):
    """No configured classifier succeeded; callers should retry later."""


class ClassificationService:
    """Coordinates multiple classifier providers and aggregates results."""

    def __init__(
        self,
        enabled_providers_override: Optional[List[str]] = None,
        cache_enabled: bool = False,
        cache_dir: str = "./cache",
    ):
        """Create a new service instance.

        Args:
          enabled_providers_override: Optional list of provider instance names to enable.
          cache_enabled: Whether to cache provider responses to disk.
          cache_dir: Path to the directory for caching.
        """
        # Offline mode disables external providers and semantic merge (network calls)
        self.offline_mode = _env_flag_enabled("OFFLINE_MODE") or _env_flag_enabled(
            "DISABLE_EXTERNAL_PROVIDERS"
        )
        self.model_endpoints = configured_endpoints()
        configured = os.getenv("FETCH_ENABLED_PROVIDERS")
        if configured is None and self.model_endpoints:
            configured = ",".join(self.model_endpoints)
        self.allowed_providers = (
            [name.strip() for name in configured.split(",") if name.strip()]
            if configured is not None
            else None
        )
        if self.allowed_providers is not None and not self.allowed_providers:
            raise ValueError("FETCH_ENABLED_PROVIDERS must not be empty")
        # Cache of all initialized provider instances (keyed by instance_name)
        self._all_providers: Dict[str, ClassifierProvider] = {}
        self.providers = self._init_providers(
            enabled_providers_override=enabled_providers_override
        )
        if not self.offline_mode:
            self._auxiliary_provider()
        # Lock to protect taxonomy cache updates if we ever load on-miss at runtime
        self._tax_lock = Lock()
        # Preload all taxonomies to reduce per-request latency
        self.taxonomies = self._load_all_taxonomies()
        # Initialize OpenAI client for semantic merging (required dependency)
        self.openai_client = None
        self.semantic_merge_model_name = os.getenv(
            "OPENAI_GPT_5_2_MODEL", "gpt-5.2"
        ).strip()
        if (
            not self.offline_mode
            and (os.getenv("OPENAI_GPT_5_2_API_KEY") or os.getenv("OPENAI_API_KEY"))
            and (
                self.allowed_providers is None
                or any(
                    name in {"openai", "gpt-5", "gpt-5.2", "gpt-41-nano"}
                    for name in self.allowed_providers
                )
            )
        ):
            self.openai_client = build_async_openai_client(
                api_key_env="OPENAI_GPT_5_2_API_KEY",
                api_key_fallback_env="OPENAI_API_KEY",
                base_url_env="OPENAI_BASE_URL_GPT_5_2",
            )
        self.cache_enabled = cache_enabled
        self._client_closed = False
        if self.cache_enabled:
            cache_path = os.path.join(cache_dir, "provider_responses_cache")
            logger.info(f"Initializing cache at: {os.path.abspath(cache_path)}")
            if os.path.exists(cache_path):
                logger.info(
                    f"Cache directory exists. Contents: {os.listdir(cache_path)}"
                )
            else:
                logger.info("Cache directory does not exist.")
            self.cache = Cache(cache_path)
            logger.info(f"Caching enabled. Cache directory: {self.cache.directory}")
        else:
            self.cache = {}
            logger.info("Caching disabled.")

    @staticmethod
    def _extract_top_level(label_str: str) -> str:
        """Extract the top-level category from a label string."""
        if not label_str:
            return ""
        if ">" in label_str:
            return label_str.split(">")[0].strip()
        if "," in label_str:
            return label_str.split(",")[0].strip()
        return label_str.strip()

    def _looks_multi_issue(
        self,
        problem_description: Optional[str],
        sorted_labels: List[Tuple[str, float]],
    ) -> bool:
        """Heuristic detector for prompts that likely contain multiple legal issues."""
        if not problem_description or not sorted_labels:
            return False

        text = problem_description.lower()
        clause_candidates = [
            chunk.strip()
            for chunk in re.split(
                r"[.;!?]+|\b(?:also|plus|another|separately|meanwhile)\b", text
            )
            if chunk and chunk.strip()
        ]
        clause_count = sum(1 for chunk in clause_candidates if len(chunk) >= 12)
        has_listy_punctuation = (
            ";" in problem_description or problem_description.count(".") >= 2
        )
        top_level_scores = defaultdict(float)
        leader_score = sorted_labels[0][1]
        strength_floor = max(leader_score * 0.35, 0.8)

        for label, score in sorted_labels[:6]:
            if score < strength_floor:
                continue
            top_level_scores[self._extract_top_level(label)] += score

        strong_top_levels = len(top_level_scores)
        return strong_top_levels >= 2 and (clause_count >= 3 or has_listy_punctuation)

    def _should_apply_singleton_rescue(
        self,
        problem_description: Optional[str],
        sorted_labels: List[Tuple[str, float]],
    ) -> bool:
        """Looser gate for rescuing one additional issue on mixed prompts."""
        if not problem_description or len(sorted_labels) < 3:
            return False

        text = problem_description.lower()
        clause_candidates = [
            chunk.strip()
            for chunk in re.split(
                r"[.;!?]+|\b(?:also|plus|another|separately|meanwhile|and then)\b",
                text,
            )
            if chunk and chunk.strip()
        ]
        clause_count = sum(1 for chunk in clause_candidates if len(chunk) >= 10)
        leader_score = sorted_labels[0][1]
        strength_floor = max(leader_score * 0.25, 0.4)
        strong_top_levels = {
            self._extract_top_level(label)
            for label, score in sorted_labels[:8]
            if score >= strength_floor
        }
        return clause_count >= 2 and len(strong_top_levels) >= 3

    def _get_semantic_merge_model_name(self) -> str:
        """Choose the semantic-merge backend model or deployment name."""
        configured_model = os.getenv("OPENAI_SEMANTIC_MERGE_MODEL", "").strip()
        if configured_model:
            return configured_model

        return self.semantic_merge_model_name

    def _select_top_labels(
        self,
        sorted_labels: List[Tuple[str, float]],
        problem_description: Optional[str],
    ) -> List[Label]:
        """Select final labels using the tuned multi-issue singleton-rescue logic."""
        if not sorted_labels:
            return []

        max_labels = max(1, DEFAULT_LABEL_SELECTION_MAX_LABELS)
        score_window = DEFAULT_LABEL_SELECTION_SCORE_WINDOW
        leader_score = sorted_labels[0][1]

        if not self._looks_multi_issue(problem_description, sorted_labels):
            selected = sorted_labels[:max_labels]
        else:
            window_candidates = [
                item
                for item in sorted_labels
                if (leader_score - item[1]) <= score_window
            ]
            if not window_candidates:
                window_candidates = sorted_labels[:max_labels]

            selected = []
            seen_top_levels = set()
            for label, score in window_candidates:
                top_level = self._extract_top_level(label)
                if top_level in seen_top_levels:
                    continue
                selected.append((label, score))
                seen_top_levels.add(top_level)
                if len(selected) >= max_labels:
                    break

            if len(selected) < min(max_labels, len(window_candidates)):
                selected_labels = {label for label, _ in selected}
                for label, score in window_candidates:
                    if label in selected_labels:
                        continue
                    selected.append((label, score))
                    selected_labels.add(label)
                    if len(selected) >= max_labels:
                        break

            selected = selected[:max_labels]

        if self._should_apply_singleton_rescue(problem_description, sorted_labels):
            selected_top_levels = [
                self._extract_top_level(label) for label, _ in selected
            ]
            top_level_counts = defaultdict(int)
            for top_level in selected_top_levels:
                top_level_counts[top_level] += 1

            leader_score = sorted_labels[0][1]
            rescue_floor = max(leader_score * 0.25, 0.4)
            rescue_candidates = [
                (label, score)
                for label, score in sorted_labels[:8]
                if self._extract_top_level(label) not in top_level_counts
                and score >= rescue_floor
            ]

            if rescue_candidates:
                rescue_label, rescue_score = rescue_candidates[0]
                duplicate_indexes = [
                    index
                    for index, (label, _score) in enumerate(selected)
                    if top_level_counts[self._extract_top_level(label)] > 1
                ]
                if len(selected) < max_labels:
                    selected.append((rescue_label, rescue_score))
                elif duplicate_indexes:
                    replace_index = min(
                        duplicate_indexes, key=lambda index: selected[index][1]
                    )
                    selected[replace_index] = (rescue_label, rescue_score)

        return [
            Label(label=label, confidence=score)
            for label, score in selected[:max_labels]
        ]

    def _auxiliary_provider(self):
        name = os.getenv("FETCH_AUXILIARY_MODEL")
        if name:
            provider = self._all_providers.get(name)
            if (
                not isinstance(provider, CompatibleProvider)
                or provider not in self.providers
            ):
                raise ValueError(
                    "FETCH_AUXILIARY_MODEL must name an enabled portable endpoint"
                )
            return provider
        return next(
            (p for p in self.providers if isinstance(p, CompatibleProvider)), None
        )

    async def _call_semantic_merge_model(
        self, final_prompt: str, questions_json: str
    ) -> Tuple[str, Any, Optional[str]]:
        """Run semantic merge against the first available merge model."""

        compatible = self._auxiliary_provider()
        if compatible is not None:
            response, content = await compatible.complete(final_prompt, questions_json)
            return compatible.model_name, response, content

        attempts: List[Tuple[str, Any]] = []

        if self.openai_client is not None:
            reasoning_effort = GPT_5_REASONING_EFFORT or "minimal"
            merge_model = self._get_semantic_merge_model_name()
            params: Dict[str, Any] = {
                "model": merge_model,
                "input": f"SYSTEM:\n{final_prompt}\n\nUSER:\n{questions_json}",
            }
            if OPENAI_SUPPORTS_REASONING_OBJECT:
                params["reasoning"] = {"effort": reasoning_effort}
            elif OPENAI_SUPPORTS_REASONING_EFFORT:
                params["reasoning_effort"] = reasoning_effort
            attempts.append(
                (
                    merge_model,
                    ("responses", self.openai_client.responses.create, params),
                )
            )

        gemini_provider = self._all_providers.get("gemini")
        gemini_client = getattr(gemini_provider, "client", None)
        if gemini_client is not None:
            attempts.append(
                (
                    "gemini-2.5-flash-lite",
                    (
                        "chat",
                        gemini_client.chat.completions.create,
                        {
                            "model": "gemini-2.5-flash-lite",
                            "messages": [
                                {"role": "system", "content": final_prompt},
                                {"role": "user", "content": questions_json},
                            ],
                            "temperature": 0.0,
                            "response_format": {"type": "json_object"},
                        },
                    ),
                )
            )

        mistral_provider = self._all_providers.get("mistral")
        mistral_client = getattr(mistral_provider, "client", None)
        mistral_model_name = getattr(
            mistral_provider,
            "model_name",
            "mistralai/mistral-small-3.2-24b-instruct",
        )
        if mistral_client is not None:
            attempts.append(
                (
                    mistral_model_name,
                    (
                        "chat",
                        mistral_client.chat.completions.create,
                        {
                            "model": mistral_model_name,
                            "messages": [
                                {"role": "system", "content": final_prompt},
                                {"role": "user", "content": questions_json},
                            ],
                            "temperature": 0.0,
                            "response_format": {"type": "json_object"},
                        },
                    ),
                )
            )

        errors: List[str] = []
        for model_name, (api_style, call_fn, params) in attempts:
            try:
                response = await run_with_backoff_async(call_fn, **params)
                content: Optional[str] = None
                if api_style == "responses":
                    if hasattr(response, "output_text") and response.output_text:
                        content = response.output_text
                    elif hasattr(response, "output") and response.output:
                        for item in response.output:
                            if hasattr(item, "text"):
                                content = item.text
                                break
                            if hasattr(item, "content"):
                                for part in item.content:
                                    if getattr(part, "type", "") in (
                                        "output_text",
                                        "text",
                                    ) and hasattr(part, "text"):
                                        content = part.text
                                        break
                                if content:
                                    break
                else:
                    content = response.choices[0].message.content

                if content and str(content).strip():
                    return model_name, response, str(content).strip()

                errors.append(f"{model_name}: empty content")
            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

        raise RuntimeError(
            "Semantic merge failed across all models: " + " | ".join(errors)
        )

    async def _resolve_screening_options_with_llm(
        self,
        problem_description: str,
        candidate_labels: List[str],
    ) -> List[ScreeningResolution]:
        """Use one lightweight, constrained pass to supplement keyword resolution."""
        if self.offline_mode:
            return []
        contracts = llm_resolution_contracts(problem_description, candidate_labels)
        if not contracts or (
            self.openai_client is None and self._auxiliary_provider() is None
        ):
            return []

        model = os.getenv("OPENAI_SCREENING_MODEL", "gpt-4.1-nano").strip()
        try:
            timeout_seconds = float(
                os.getenv("SCREENING_RESOLUTION_TIMEOUT_SECONDS", "8")
            )
            if not 0 < timeout_seconds <= 60:
                raise ValueError
        except ValueError:
            logger.warning(
                "Invalid screening timeout; skipping optional model extraction"
            )
            return []
        system_prompt = (
            "You are a strict information-extraction component. Review only the "
            "applicant narrative and the supplied eligible screening protocols. "
            "For a choice, return status 'disclosed' only when the narrative "
            "explicitly states that choice is true. Do not rule out choices. Omit all "
            "unresolved choices. Do not infer facts from demographics, names, "
            "language, relationships, category labels, or general context. Do not "
            'invent IDs. Return JSON only as {"resolutions": [{"protocol_id": '
            '..., "question_id": ..., "choice_id": ..., "status": '
            '"disclosed"|"ruled_out"}]}.'
        )
        input_payload = {
            "narrative": problem_description,
            "protocols": contracts,
        }
        try:
            compatible = self._auxiliary_provider()
            if compatible is not None:
                _, content = await asyncio.wait_for(
                    compatible.complete(
                        system_prompt, json.dumps(input_payload, ensure_ascii=False)
                    ),
                    timeout=timeout_seconds,
                )
            else:
                response = await asyncio.wait_for(
                    run_with_backoff_async(
                        self.openai_client.chat.completions.create,
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    input_payload, ensure_ascii=False
                                ),
                            },
                        ],
                        temperature=0.0,
                        response_format={"type": "json_object"},
                    ),
                    timeout=timeout_seconds,
                )
                content = response.choices[0].message.content or "{}"
            parsed = parse_json_from_llm_response(content)
            raw_resolutions = (
                parsed.get("resolutions", []) if isinstance(parsed, dict) else []
            )
            return validate_llm_resolutions(
                raw_resolutions,
                problem_description,
                candidate_labels,
            )
        except Exception as exc:
            logger.warning(
                "Screening option LLM check failed; using keyword fallback: %s",
                type(exc).__name__,
            )
            return []

    async def cleanup(self) -> None:
        """Cleanup async resources, particularly the OpenAI client.

        This should be called when the service is no longer needed to ensure
        proper cleanup of HTTP connections before the event loop closes.
        """
        if not self._client_closed and self.openai_client is not None:
            try:
                await self.openai_client.close()
                self._client_closed = True
                logger.debug("OpenAI client closed successfully")
            except RuntimeError as e:
                if "Event loop is closed" not in str(e):
                    logger.debug(f"Error closing OpenAI client: {type(e).__name__}")
            except Exception as e:
                logger.debug(f"Error closing OpenAI client: {type(e).__name__}")

        # Close provider clients to prevent httpx/anyio cleanup after loop shutdown.
        seen: set[int] = set()
        for provider in list(self._all_providers.values()):
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                await provider.aclose()
            except RuntimeError as e:
                if "Event loop is closed" not in str(e):
                    logger.debug(
                        f"Error closing provider {provider.instance_name}: {type(e).__name__}"
                    )
            except Exception as e:
                logger.debug(
                    f"Error closing provider {provider.instance_name}: {type(e).__name__}"
                )

    def _load_all_taxonomies(self) -> Dict[str, List[Dict[str, Any]]]:
        """Load all taxonomies from the mapping into memory as lists of dicts."""
        taxonomies = {}
        for name, file_path in TAXONOMY_MAPPING.items():
            try:
                rows = read_csv_as_list_of_dicts(file_path)
                rows = dedupe_and_clean_rows(rows)
                taxonomies[name] = rows
                logger.info(f"Successfully loaded taxonomy '{name}' from {file_path}")
            except FileNotFoundError:
                logger.warning(f"Taxonomy file not found for '{name}': {file_path}")
            except Exception as e:
                logger.error(
                    f"Error loading taxonomy file for '{name}' at {file_path}: {type(e).__name__}"
                )
        return taxonomies

    def _init_providers(
        self, enabled_providers_override: Optional[List[str]] = None
    ) -> List[ClassifierProvider]:
        """Instantiate and filter providers based on config or override.

        Uses cached provider instances when available to avoid re-initialization overhead.

        Args:
          enabled_providers_override: Restrict enabled providers to these instance names.

        Returns:
          List of enabled `ClassifierProvider` instances.
        """
        if self.allowed_providers is not None:
            if enabled_providers_override is not None:
                unknown = set(enabled_providers_override) - set(self.allowed_providers)
                if unknown:
                    raise ValueError(
                        "Requested providers are not enabled by this deployment"
                    )
            else:
                enabled_providers_override = self.allowed_providers
        if self.offline_mode:
            if enabled_providers_override is not None:
                enabled_providers = [
                    self._all_providers[name]
                    for name in enabled_providers_override
                    if name in self._all_providers
                ]
                if enabled_providers:
                    return enabled_providers

                if "keyword" in enabled_providers_override:
                    if "keyword" not in self._all_providers:
                        try:
                            self._all_providers["keyword"] = KeywordClassifierProvider()
                        except Exception as e:
                            logger.error(
                                f"Could not initialize KeywordClassifierProvider in offline mode: {type(e).__name__}"
                            )
                            return []
                    return [self._all_providers["keyword"]]

            if "keyword" not in self._all_providers:
                try:
                    self._all_providers["keyword"] = KeywordClassifierProvider()
                except Exception as e:
                    logger.error(
                        f"Could not initialize KeywordClassifierProvider in offline mode: {type(e).__name__}"
                    )
                    return []
            return [self._all_providers["keyword"]]

        # Initialize all providers once and cache them
        if not self._all_providers:
            provider_classes = [
                ("compatible", CompatibleProvider),
                ("openai", OpenAIProvider),
                ("gemini", GeminiProvider),
                ("mistral", MistralProvider),
                ("keyword", KeywordClassifierProvider),
                ("spot", SpotProvider),
                ("gpt-41-nano", GPT41NanoProvider),
                ("gpt-5", GPT5Provider),
                ("gpt-5.2", GPT52Provider),
            ]
            provider_classes.extend(
                (
                    name,
                    lambda name=name, config=config: CompatibleProvider(name, config),
                )
                for name, config in self.model_endpoints.items()
            )
            if self.allowed_providers is not None:
                unknown = set(self.allowed_providers) - {
                    name for name, _ in provider_classes
                }
                if unknown:
                    raise ValueError(
                        "Unknown FETCH_ENABLED_PROVIDERS entries: "
                        + ", ".join(sorted(unknown))
                    )
            for name, provider_class in provider_classes:
                if (
                    self.allowed_providers is not None
                    and name not in self.allowed_providers
                ):
                    continue
                if (
                    name == "compatible"
                    and self.allowed_providers is None
                    and not os.getenv("COMPATIBLE_MODEL")
                ):
                    continue
                try:
                    provider = provider_class()
                    # Cache by configured name (e.g., "openai") so ENABLED_CLASSIFIERS lookups work
                    self._all_providers[name] = provider
                    # Also cache by instance_name when it differs, for backward compatibility
                    if provider.instance_name != name:
                        self._all_providers[provider.instance_name] = provider
                except Exception as e:
                    if self.allowed_providers is not None:
                        raise RuntimeError(
                            f"Configured provider {name} could not initialize"
                        ) from e
                    logger.error(
                        f"Could not initialize {provider_class.__name__}: {type(e).__name__}"
                    )

        if enabled_providers_override is not None:
            enabled_classifier_names = enabled_providers_override
        else:
            enabled_classifier_names = ENABLED_CLASSIFIERS

        # Filter cached providers based on enabled list
        enabled_providers = [
            self._all_providers[name]
            for name in enabled_classifier_names
            if name in self._all_providers
        ]
        return enabled_providers

    async def _semantically_merge_questions(
        self,
        questions: List[FollowUpQuestion],
        *,
        request_span,
        request_id: Optional[str],
        taxonomy_name: Optional[str],
        conversation_id: Optional[str] = None,
        problem_description: Optional[str] = None,
        candidate_labels: Optional[List[str]] = None,
        question_support: Optional[Dict[str, float]] = None,
    ) -> List[FollowUpQuestion]:
        """Merge similar follow-up questions using an LLM call.

        Args:
          questions: A list of candidate follow-up questions.

        Returns:
          A deduplicated list of merged questions.
        """
        if self.offline_mode:
            return questions

        if not questions:
            return []

        questions_data = [q.model_dump() for q in questions]
        merge_input = {
            "problem_description": problem_description or "",
            "candidate_labels": candidate_labels or [],
            "weighted_question_scores": question_support or {},
            "questions": questions_data,
        }
        questions_json = json.dumps(merge_input, indent=2)

        final_prompt, _ = load_prompt("semantic_merge", [])

        generation = start_provider_generation(
            request_span,
            name="semantic_merge",
            model="semantic-merge",
            input_payload={
                "problem_preview": self._truncate_text(problem_description or ""),
                "candidate_labels": (candidate_labels or [])[:5],
                "questions_preview": questions_data[:5],
                "question_count": len(questions_data),
                "taxonomy": taxonomy_name,
                "conversation_id": conversation_id,
            },
            metadata={
                "provider": "semantic_merge",
                "request_id": request_id,
                "taxonomy": taxonomy_name,
                **({"conversation_id": conversation_id} if conversation_id else {}),
            },
        )

        start_time = time.perf_counter()
        response = None
        parsed_response: Any = None
        selected_model_name: Optional[str] = None
        telemetry_error: Optional[BaseException] = None

        try:
            try:
                model_name, response, content = await asyncio.wait_for(
                    self._call_semantic_merge_model(final_prompt, questions_json),
                    timeout=SEMANTIC_MERGE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "semantic_merge timed out after "
                    f"{SEMANTIC_MERGE_TIMEOUT_SECONDS:.1f}s"
                ) from exc
            logger.info(
                "[ClassificationService] semantic_merge completed with model=%s",
                model_name,
            )
            selected_model_name = model_name
            logger.info(
                "[ClassificationService] semantic_merge latency_ms=%.2f model=%s",
                (time.perf_counter() - start_time) * 1000.0,
                model_name,
            )

            if not content or not str(content).strip():
                logger.error("Semantic merge returned no text")
                return questions

            content = str(content).strip()
            # Use robust JSON parsing that handles fenced code blocks and common LLM JSON errors
            parsed_response = parse_json_from_llm_response(content)

            if isinstance(parsed_response, list):
                merged_questions_data = parsed_response
            elif isinstance(parsed_response, dict):
                merged_questions_data = parsed_response.get("merged_questions", [])
            else:
                merged_questions_data = []

            merged_questions = []
            for q_data in merged_questions_data:
                if not isinstance(q_data, dict):
                    logger.warning("Merged question item is not an object; skipping")
                    continue

                if "question" in q_data:
                    # Normalize keys from the merge output: many prompts/models
                    # use 'type' while our API model expects 'format'.
                    if "format" not in q_data and "type" in q_data:
                        q_data["format"] = q_data.pop("type")

                    # If options are present but no format was provided, default to 'radio'
                    # to ensure UI widgets render correctly.
                    if q_data.get("format") in (None, "") and q_data.get("options"):
                        q_data["format"] = "radio"

                    try:
                        merged_questions.append(FollowUpQuestion(**q_data))
                    except Exception as e:
                        logger.warning(
                            "Invalid merged question; using original questions"
                        )
                        merged_questions.append(
                            FollowUpQuestion(
                                question=str(q_data.get("question", "")),
                                format=(
                                    q_data.get("format")
                                    or ("radio" if q_data.get("options") else None)
                                ),
                                options=q_data.get("options"),
                            )
                        )
                else:
                    logger.warning("Merged question is missing question key")

            return merged_questions or questions

        except (json.JSONDecodeError, ValueError) as e:
            telemetry_error = e
            error_message = f"Invalid semantic merge response ({type(e).__name__})"
            logger.error(error_message)
            return questions
        except Exception as e:
            telemetry_error = e
            error_message = f"An unexpected error occurred during semantic merging: {type(e).__name__}"
            logger.error(error_message)
            return questions
        finally:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            metadata = {
                "provider": "semantic_merge",
                "request_id": request_id,
                "taxonomy": taxonomy_name,
                "latency_ms": latency_ms,
            }
            if selected_model_name:
                metadata["selected_model"] = selected_model_name
            if isinstance(parsed_response, list):
                merged_questions_for_log = parsed_response[:3]
                raw_keys = []
            elif isinstance(parsed_response, dict):
                merged_questions_for_log = parsed_response.get("merged_questions", [])[
                    :3
                ]
                raw_keys = list(parsed_response.keys())
            else:
                merged_questions_for_log = []
                raw_keys = []

            output_payload = {
                "merged_questions": [
                    q.get("question")
                    for q in merged_questions_for_log
                    if isinstance(q, dict)
                ],
                "raw_keys": raw_keys,
            }
            usage_details = extract_usage_details(response)
            finalize_provider_generation(
                generation,
                output_payload=output_payload if parsed_response else None,
                metadata=metadata,
                usage_details=usage_details,
                error=telemetry_error,
            )

    async def _get_voted_results(
        self,
        results_with_providers: List[Tuple[str, Union[Dict[str, Any], Exception]]],
        include_debug_details: bool,
        *,
        request_span=None,
        request_id: Optional[str] = None,
        taxonomy_name: Optional[str] = None,
        skip_followups: bool = False,
        skip_semantic_merge: bool = False,
        conversation_id: Optional[str] = None,
        problem_description: Optional[str] = None,
    ) -> ClassificationResponse:
        """Combine multiple provider results by weighted voting.

        Args:
          results_with_providers: A list of (provider_instance_name, result_or_exception) tuples.
          include_debug_details: Whether to include raw and intermediate scoring data.
          skip_semantic_merge: If True, skip semantic merging of follow-up questions for lower latency.

        Returns:
          A `ClassificationResponse` with aggregated labels and questions.
        """
        label_scores = defaultdict(float)
        question_scores = defaultdict(float)
        no_legal_problem_scores = 0.0
        total_provider_weight = 0.0
        raw_provider_results = {}

        for provider_instance_name, result in results_with_providers:
            base_weight = CLASSIFIER_WEIGHTS.get(
                provider_instance_name, 1.0
            )  # Default to 1.0 if weight not defined
            if isinstance(result, Exception) or (
                isinstance(result, dict) and result.get("error")
            ):
                logger.error(
                    f"Classifier provider '{provider_instance_name}' failed: {type(result).__name__}"
                )
                raw_provider_results[provider_instance_name] = {
                    "error": type(result).__name__
                }
            else:
                total_provider_weight += base_weight
                # Serialize labels within the raw result to ensure JSON compatibility
                serialized_result = result.copy()
                if "labels" in serialized_result and isinstance(
                    serialized_result["labels"], list
                ):
                    serialized_result["labels"] = [
                        label.model_dump() if isinstance(label, Label) else label
                        for label in serialized_result["labels"]
                    ]
                if "questions" in serialized_result and isinstance(
                    serialized_result["questions"], list
                ):
                    serialized_result["questions"] = [
                        q.model_dump() if isinstance(q, FollowUpQuestion) else q
                        for q in serialized_result["questions"]
                    ]
                raw_provider_results[provider_instance_name] = serialized_result

                # Aggregate the likely_no_legal_problem vote
                voted_no_legal = result.get("likely_no_legal_problem", False)
                if voted_no_legal:
                    no_legal_problem_scores += base_weight
                    logger.info(
                        f"Provider {provider_instance_name} voted 'likely_no_legal_problem=True' with weight {base_weight}"
                    )
                else:
                    logger.info(
                        f"Provider {provider_instance_name} voted 'likely_no_legal_problem=False' with weight {base_weight}"
                    )

                for label_entry in result.get("labels", []):
                    if isinstance(label_entry, Label):
                        label_str = label_entry.label
                        confidence = (
                            label_entry.confidence
                            if label_entry.confidence is not None
                            else 1.0
                        )
                    else:  # It's a dict
                        label_str = label_entry.get("label")
                        confidence = label_entry.get("confidence", 1.0)

                    confidence = max(0.0, min(float(confidence), 1.0))
                    weighted_score = base_weight * confidence

                    if label_str:
                        label_scores[label_str] += weighted_score

                for question_entry in result.get("questions", []):
                    if isinstance(question_entry, dict):
                        question_text = question_entry.get("question")
                    else:  # Assume it's a string
                        question_text = question_entry

                    if question_text:
                        question_scores[question_text] += base_weight

        # Sort labels and questions by their scores in descending order
        sorted_labels = sorted(
            label_scores.items(), key=lambda item: item[1], reverse=True
        )

        # For questions, we need to preserve format and options, so we'll collect them directly
        # from the raw results, prioritizing questions from higher-weighted providers if there are conflicts.
        all_questions_with_details = {}
        for provider_instance_name, result in results_with_providers:
            provider_weight = CLASSIFIER_WEIGHTS.get(provider_instance_name, 1.0)
            if not isinstance(result, Exception):
                for question_entry in result.get("questions", []):
                    if isinstance(question_entry, dict):
                        question_text = question_entry.get("question")
                        if question_text:
                            # If question doesn't exist or current provider has higher weight, update it
                            if (
                                question_text not in all_questions_with_details
                                or provider_weight
                                > CLASSIFIER_WEIGHTS.get(
                                    all_questions_with_details[question_text].get(
                                        "provider", ""
                                    ),
                                    0,
                                )
                            ):
                                all_questions_with_details[question_text] = {
                                    "question_obj": FollowUpQuestion(
                                        question=question_text,
                                        format=question_entry.get("format")
                                        or question_entry.get("type"),
                                        options=question_entry.get("options"),
                                    ),
                                    "provider": provider_instance_name,
                                }
                    elif isinstance(question_entry, str):
                        if (
                            question_entry not in all_questions_with_details
                            or provider_weight
                            > CLASSIFIER_WEIGHTS.get(
                                all_questions_with_details[question_entry].get(
                                    "provider", ""
                                ),
                                0,
                            )
                        ):
                            all_questions_with_details[question_entry] = {
                                "question_obj": FollowUpQuestion(
                                    question=question_entry
                                ),
                                "provider": provider_instance_name,
                            }

        # Skip follow-up question processing entirely when skip_followups is True
        if skip_followups:
            final_top_questions = []
        else:
            all_collected_questions = list(all_questions_with_details.values())
            all_question_objs = [
                item["question_obj"] for item in all_collected_questions
            ]

            # Semantic merge handles both deduplication and ranking
            # Skip if requested or if we have 3 or fewer unique questions (no benefit from merge)
            if skip_semantic_merge or len(all_question_objs) <= 3:
                # Simple deduplication by question text without LLM call
                seen_questions = set()
                merged_questions = []
                for q in all_question_objs:
                    if q.question not in seen_questions:
                        seen_questions.add(q.question)
                        merged_questions.append(q)
            else:
                merged_questions = await self._semantically_merge_questions(
                    all_question_objs,
                    request_span=request_span,
                    request_id=request_id,
                    taxonomy_name=taxonomy_name,
                    conversation_id=conversation_id,
                    problem_description=problem_description,
                    candidate_labels=[label for label, _ in sorted_labels[:5]],
                    question_support=dict(question_scores),
                )

            # Take the top 3 questions from the semantic merge result
            # The semantic merge is responsible for both combining similar questions
            # and ranking them by relevance
            final_top_questions = merged_questions[:3] if merged_questions else []

            # Final safety normalization: if a question has options but no format,
            # default to 'radio' so clients can render it correctly.
            for q in final_top_questions:
                if getattr(q, "options", None) and not getattr(q, "format", None):
                    q.format = "radio"

        top_labels = self._select_top_labels(
            sorted_labels,
            problem_description=problem_description,
        )

        # Determine if there's likely no legal problem based on:
        # 1. Strong majority of providers voting "no legal problem"
        # 2. Very low agreement on the problem category (high entropy), but
        #    treat parent/child (top-level/subcategory) matches as agreement
        likely_no_legal_problem = False
        disagreement_score = 0.0

        # Check consensus on no legal problem (strict majority of attempted weight)
        attempted_weight = sum(
            CLASSIFIER_WEIGHTS.get(name, 1.0) for name, _ in results_with_providers
        )
        no_legal_problem_threshold = attempted_weight * 0.5
        if (
            no_legal_problem_scores > no_legal_problem_threshold
            and no_legal_problem_scores > 0
        ):
            likely_no_legal_problem = True
            logger.info(
                f"Setting likely_no_legal_problem=True: {no_legal_problem_scores}/{total_provider_weight} providers voted 'no legal problem'"
            )

        # Helper: extract the top-level category from a label string.
        # Supports formats like 'Top > Sub' or 'Top,Sub' or just 'Top'
        # Helper: hierarchy distance (0 = exact same, 1 = same top-level, 2 = different top-level)
        def _hierarchy_distance(a: str, b: str) -> int:
            if not a or not b:
                return 2
            if a.strip() == b.strip():
                return 0
            if self._extract_top_level(a) == self._extract_top_level(b):
                return 1
            return 2

        # Check for very low agreement on categories (if labels exist)
        # Aggregate scores at the top-level category first. This avoids
        # penalizing cases where providers disagree on subcategories but
        # agree on the broader area of law.
        if sorted_labels and not likely_no_legal_problem:
            # Compute top-level aggregated scores
            top_level_scores = defaultdict(float)
            for label_str, score in label_scores.items():
                top = self._extract_top_level(label_str)
                top_level_scores[top] += score

            sorted_top_levels = sorted(
                top_level_scores.items(), key=lambda item: item[1], reverse=True
            )

            top_level_score = sorted_top_levels[0][1] if sorted_top_levels else 0

            # Compute disagreement score: 1.0 - (top category weight / total weight)
            # Higher score indicates more disagreement (less consensus on top category)
            disagreement_score = (
                max(0.0, 1.0 - (top_level_score / total_provider_weight))
                if total_provider_weight > 0
                else 0.0
            )

        # Build structured likely_no_legal_problem info
        lnlp_obj = {
            "value": likely_no_legal_problem,
            "weighted_result": (
                (no_legal_problem_scores / total_provider_weight)
                if total_provider_weight > 0
                else 0.0
            ),
            "disagreement_score": disagreement_score,
        }

        response_data = {
            "labels": top_labels,
            "follow_up_questions": final_top_questions,
            "likely_no_legal_problem": lnlp_obj,
        }

        if include_debug_details:
            response_data["raw_provider_results"] = raw_provider_results
            response_data["weighted_label_scores"] = dict(label_scores)
            response_data["weighted_question_scores"] = dict(question_scores)

        logger.info("_get_voted_results completed")
        return ClassificationResponse(**response_data)

    async def _get_first_result(
        self,
        results_with_providers: List[Tuple[str, Union[Dict[str, Any], Exception]]],
        include_debug_details: bool,
        skip_followups: bool = False,
    ) -> ClassificationResponse:
        """Return the first successful provider result, optionally with debug data.

        Args:
          results_with_providers: A list of (provider_instance_name, result_or_exception) tuples.
          include_debug_details: Whether to include raw results for debugging.
          skip_followups: Whether to skip follow-up questions in the response.

        Returns:
          A `ClassificationResponse` based on the first non-error provider output,
          or a response indicating that no providers succeeded.
        """
        raw_provider_results = {}
        for provider_instance_name, result in results_with_providers:
            if isinstance(result, Exception):
                logger.error(
                    f"Classifier provider '{provider_instance_name}' failed: {type(result).__name__}"
                )
                raw_provider_results[provider_instance_name] = {
                    "error": type(result).__name__
                }
            elif result and isinstance(result, dict) and "error" in result:
                logger.error(
                    f"Classifier provider '{provider_instance_name}' returned an error"
                )
                raw_provider_results[provider_instance_name] = {
                    "error": "Provider returned an error"
                }
            else:
                # Serialize labels within the raw result to ensure JSON compatibility
                serialized_result = result.copy()
                if "labels" in serialized_result and isinstance(
                    serialized_result["labels"], list
                ):
                    serialized_result["labels"] = [
                        label.model_dump() if isinstance(label, Label) else label
                        for label in serialized_result["labels"]
                    ]
                if "questions" in serialized_result and isinstance(
                    serialized_result["questions"], list
                ):
                    serialized_result["questions"] = [
                        q.model_dump() if isinstance(q, FollowUpQuestion) else q
                        for q in serialized_result["questions"]
                    ]
                raw_provider_results[provider_instance_name] = serialized_result

                # Build follow-up questions list, or empty if skip_followups is True
                if skip_followups:
                    follow_up_questions = []
                else:
                    follow_up_questions = [
                        (
                            FollowUpQuestion(
                                question=str(q.get("question")),
                                format=(q.get("format") or q.get("type")),
                                options=q.get("options"),
                            )
                            if isinstance(q, dict)
                            else FollowUpQuestion(question=str(q))
                        )
                        for q in result.get("questions", [])
                    ]
                    # Final safety normalization for first-result path as well
                    for fq in follow_up_questions:
                        if getattr(fq, "options", None) and not getattr(
                            fq, "format", None
                        ):
                            fq.format = "radio"

                # Build likely_no_legal_problem from provider result
                lnlp_value = result.get("likely_no_legal_problem", False)
                lnlp_obj = {
                    "value": lnlp_value if isinstance(lnlp_value, bool) else False,
                    "weighted_result": 1.0 if lnlp_value else 0.0,
                    "disagreement_score": 0.0,
                }

                response_data = {
                    "labels": result.get("labels", []),
                    "follow_up_questions": follow_up_questions,
                    "likely_no_legal_problem": lnlp_obj,
                }
                if include_debug_details:
                    response_data["raw_provider_results"] = raw_provider_results
                logger.info("_get_first_result completed")
                return ClassificationResponse(**response_data)

        fallback_questions = []

        # Default likely_no_legal_problem for fallback (no providers succeeded)
        fallback_lnlp = {
            "value": False,
            "weighted_result": 0.0,
            "disagreement_score": 0.0,
        }

        logger.info(
            f"_get_first_result response_data: {{'labels': [], 'follow_up_questions': {fallback_questions}}}"
        )
        return ClassificationResponse(
            labels=[],
            follow_up_questions=fallback_questions,
            likely_no_legal_problem=fallback_lnlp,
            raw_provider_results=(
                raw_provider_results if include_debug_details else None
            ),
        )

    def _get_cache_key(
        self, problem_description: str, provider_instance_name: str, prompt: str
    ) -> str:
        scope = current_scope.get()
        identity = scope.scope_id if scope else current_client.get()
        material = json.dumps(
            [identity, problem_description, provider_instance_name, prompt]
        )
        return hashlib.sha256(material.encode()).hexdigest()

    async def classify(
        self,
        request: ClassificationRequest,
        enabled_models: Optional[List[str]] = None,
        client_id: Optional[str] = None,
    ) -> ClassificationResponse:
        """Run classification with enabled providers and aggregate results."""

        screening_answers = request.screening_answers or []
        validate_answers(screening_answers)
        screening_results = evaluate_answers(screening_answers)
        combined_followup_answers = list(request.followup_answers or [])
        combined_followup_answers.extend(answers_for_model(screening_answers))
        provider_request = request.model_copy(
            update={"followup_answers": combined_followup_answers or None}
        )
        has_refinement_answers = bool(combined_followup_answers)

        request_id = str(uuid4())
        trace_input = {
            "request_id": request_id,
            "problem_description": self._truncate_text(request.problem_description),
            "taxonomy_name": request.taxonomy_name,
            "taxonomy_format": request.taxonomy_format,
            "decision_mode": request.decision_mode,
            "include_debug_details": request.include_debug_details,
            "skip_followups": request.skip_followups,
            "legacy_screening": request.legacy_screening,
            "enabled_models": enabled_models or request.enabled_models,
            "conversation_id": request.conversation_id,
            "followup_questions_answers": [
                answer.model_dump() for answer in combined_followup_answers
            ],
            "client_id": client_id,
        }

        # Use the user's query (truncated) as the request span name so it is
        # recognizable in whichever telemetry backend is configured.
        span_name = (
            self._truncate_text(request.problem_description, limit=200)
            or "classification.request"
        )

        metadata = {
            "request_id": request_id,
            "client_request_id": request.client_request_id,
            "user_id": request.user_id,
            "taxonomy": request.taxonomy_name,
            "taxonomy_format": request.taxonomy_format,
            "decision_mode": request.decision_mode,
            "skip_followups": request.skip_followups,
            "legacy_screening": request.legacy_screening,
            "client_id": client_id,
        }
        if request.conversation_id:
            metadata["conversation_id"] = request.conversation_id

        request_span = start_request_trace(
            name=span_name,
            input_payload=trace_input,
            metadata=metadata,
            full_query_text=request.problem_description,
        )

        response: Optional[ClassificationResponse] = None
        error: Optional[BaseException] = None
        request_start = time.perf_counter()

        try:
            taxonomy_file = TAXONOMY_MAPPING.get(request.taxonomy_name)
            if not taxonomy_file:
                raise ValueError("Unknown taxonomy")

            # For LIST taxonomy, use the simplified version (Title column only)
            # to avoid confusing LLMs with the codes
            if request.taxonomy_name == "list":
                taxonomy_df = load_list_taxonomy_simple()
                logger.info("Using simplified LIST taxonomy (Title column only)")
            else:
                taxonomy_df = self.taxonomies.get(request.taxonomy_name)
                if taxonomy_df is None:
                    logger.info(
                        f"Taxonomy '{request.taxonomy_name}' not preloaded; attempting to load from disk: {taxonomy_file}"
                    )
                    taxonomy_df = self._load_taxonomy(taxonomy_file)
                    if taxonomy_df is None:
                        raise RuntimeError("Configured taxonomy is unavailable")
                    with self._tax_lock:
                        self.taxonomies[request.taxonomy_name] = taxonomy_df

            explicit_enabled_models = (
                enabled_models if enabled_models is not None else request.enabled_models
            )
            if explicit_enabled_models is not None:
                current_providers = self._init_providers(
                    enabled_providers_override=explicit_enabled_models
                )
            else:
                current_providers = self.providers
                # For refinement calls with follow-up or screening answers,
                # exclude providers that cannot use the extra conversational
                # context (keyword and SPOT). Every LLM-backed provider stays
                # in the vote, including Gemini/Mistral, which are not
                # LLMClassifierProvider subclasses but can consume context.
                if has_refinement_answers:
                    current_providers = [
                        p
                        for p in current_providers
                        if p.provider_type not in FOLLOWUP_EXCLUDED_PROVIDER_TYPES
                    ]
                    logger.info(
                        "refinement answers present: excluding non-context-aware "
                        f"providers (types={sorted(FOLLOWUP_EXCLUDED_PROVIDER_TYPES)})"
                    )

            tasks = []
            results_with_providers: List[
                Tuple[str, Union[Dict[str, Any], Exception]]
            ] = []
            prompts_for_caching: Dict[str, str] = {}

            for provider in current_providers:
                final_prompt, prompt_template = load_prompt(
                    provider.provider_type,
                    taxonomy_df,
                    skip_followups=request.skip_followups,
                    taxonomy_name=request.taxonomy_name,
                )
                cache_context = json.dumps(
                    {
                        "prompt": final_prompt,
                        "taxonomy": request.taxonomy_name,
                        "format": request.taxonomy_format,
                        "model": provider.model_name,
                        "endpoint": str(
                            getattr(getattr(provider, "client", None), "base_url", "")
                        )
                        or getattr(provider, "messages_url", ""),
                    },
                    sort_keys=True,
                )
                prompts_for_caching[provider.instance_name] = cache_context

                if self.cache_enabled and not has_refinement_answers:
                    cache_key = self._get_cache_key(
                        request.problem_description,
                        provider.instance_name,
                        cache_context,
                    )
                    if cache_key in self.cache:
                        logger.info(f"Cache hit for {provider.instance_name}")
                        results_with_providers.append(
                            (provider.instance_name, self.cache.get(cache_key))
                        )
                        continue

                tasks.append(
                    asyncio.create_task(
                        self._classify_with_telemetry(
                            provider=provider,
                            request=provider_request,
                            taxonomy_df=taxonomy_df,
                            final_prompt=final_prompt,
                            request_span=request_span,
                            request_id=request_id,
                        ),
                        name=provider.instance_name,
                    )
                )

            new_results = await asyncio.gather(*tasks, return_exceptions=True)
            task_names = [task.get_name() for task in tasks]
            new_results_with_providers = [
                (task_name, result)
                for task_name, result in zip(task_names, new_results)
            ]

            verbose = os.getenv("DEBUG_LOG") in {"1", "true", "TRUE", "True"}
            logger.info("[ClassificationService] Results summary:")
            for name, res in new_results_with_providers:
                if isinstance(res, Exception):
                    logger.error(f" - {name}: Exception -> {type(res).__name__}")
                    continue

                status = "ok"
                if isinstance(res, dict) and res.get("error"):
                    status = "error"
                latency = None
                telemetry_payload = (
                    res.get("telemetry") if isinstance(res, dict) else None
                )
                if isinstance(telemetry_payload, dict):
                    latency = telemetry_payload.get("latency_ms")
                latency_str = (
                    f", latency_ms={latency:.2f}"
                    if isinstance(latency, (int, float))
                    else ""
                )
                if status == "ok" or verbose:
                    logger.info(f" - {name}: {status}{latency_str}")
                else:
                    logger.warning(f" - {name}: {status}{latency_str}")
            results_with_providers.extend(new_results_with_providers)
            failed_providers = [
                name
                for name, result in results_with_providers
                if isinstance(result, Exception)
                or (isinstance(result, dict) and result.get("error"))
            ]
            if not results_with_providers or len(failed_providers) == len(
                results_with_providers
            ):
                raise ClassificationUnavailable("No classifier is currently available")

            if self.cache_enabled and not has_refinement_answers:
                logger.info("Caching new results...")
                for task_name, result in new_results_with_providers:
                    if isinstance(result, dict) and not result.get("error"):
                        prompt_template = prompts_for_caching.get(task_name, "")
                        cache_key = self._get_cache_key(
                            request.problem_description, task_name, prompt_template
                        )
                        self.cache[cache_key] = result

            decision_mode = request.decision_mode or DECISION_MODE
            if decision_mode == "first":
                response = await self._get_first_result(
                    results_with_providers,
                    request.include_debug_details,
                    skip_followups=request.skip_followups,
                )
            else:
                response = await self._get_voted_results(
                    results_with_providers,
                    request.include_debug_details,
                    request_span=request_span,
                    request_id=request_id,
                    taxonomy_name=request.taxonomy_name,
                    skip_followups=request.skip_followups,
                    skip_semantic_merge=request.skip_semantic_merge,
                    conversation_id=request.conversation_id,
                    problem_description=request.problem_description,
                )

            response.degraded = bool(failed_providers)
            response.failed_providers = failed_providers

            # Convert labels to LIST format if requested
            # Automatically use LIST format if taxonomy_name is "list"
            should_convert_to_list = (
                request.taxonomy_format == "list" or request.taxonomy_name == "list"
            )
            if should_convert_to_list and response:
                response = self._convert_labels_to_list_format(
                    response,
                    taxonomy_name=request.taxonomy_name,
                )

            if response:
                all_candidate_labels = candidate_label_strings(results_with_providers)
                keyword_screening_resolutions = resolve_narrative(
                    request.problem_description, all_candidate_labels
                )
                if request.enable_screening_protocols and not request.skip_followups:
                    llm_screening_resolutions = (
                        await self._resolve_screening_options_with_llm(
                            request.problem_description,
                            all_candidate_labels,
                        )
                    )
                else:
                    llm_screening_resolutions = []
                screening_resolutions = merge_narrative_resolutions(
                    keyword_screening_resolutions,
                    llm_screening_resolutions,
                )
                structured_protocol_ids = {
                    answer.protocol_id for answer in screening_answers
                }
                narrative_screening_results = narrative_results(
                    screening_resolutions,
                    exclude_protocol_ids=structured_protocol_ids,
                )
                screening_results = [
                    *screening_results,
                    *narrative_screening_results,
                ]
                # A narrative disclosure resolves individual checkbox choices, not
                # necessarily the whole multi-select protocol. Only a submitted
                # structured answer marks the protocol completed.
                completed_protocol_ids = structured_protocol_ids
                if request.enable_screening_protocols and not request.skip_followups:
                    response.screening_questions = select_questions(
                        request.problem_description,
                        all_candidate_labels,
                        completed_protocol_ids,
                        language=request.screening_language,
                        limit=3,
                        resolutions=screening_resolutions,
                    )
                else:
                    response.screening_questions = []

                # Protocol questions have priority within the three-question round.
                remaining_llm_slots = max(0, 3 - len(response.screening_questions))
                llm_questions = response.follow_up_questions[:remaining_llm_slots]
                if request.legacy_screening:
                    legacy_questions = [
                        FollowUpQuestion(
                            question=question.question,
                            format="checkbox",
                            options=[choice.label for choice in question.choices],
                        )
                        for question in response.screening_questions
                    ]
                    response.follow_up_questions = [
                        *legacy_questions,
                        *llm_questions,
                    ]
                else:
                    response.follow_up_questions = llm_questions
                response.screening_results = screening_results
                response.screening_resolutions = screening_resolutions
                response.mandatory_categories = build_mandatory_categories(
                    screening_results
                )
                response.effective_categories = build_effective_categories(
                    response.labels, response.mandatory_categories
                )
                response.information_triggers = list(
                    dict.fromkeys(
                        result.information_trigger
                        for result in screening_results
                        if result.information_trigger
                    )
                )

            return response
        except Exception as exc:
            error = exc
            raise
        finally:
            if response is not None:
                response.request_id = request_id
                response.client_id = client_id
            output_payload = response.model_dump() if response else None
            finalize_request_trace(
                request_span,
                output_payload=output_payload,
                error=error,
            )
            flush_start = time.perf_counter()
            await flush_telemetry_async()
            logger.info(
                "[ClassificationService] request_id=%s telemetry_flush_latency_ms=%.2f",
                request_id,
                (time.perf_counter() - flush_start) * 1000.0,
            )
            logger.info(
                "[ClassificationService] request_id=%s total_latency_ms=%.2f error=%s",
                request_id,
                (time.perf_counter() - request_start) * 1000.0,
                type(error).__name__ if error else "",
            )

    def _load_taxonomy(self, taxonomy_file: str) -> Optional[list]:
        """Load taxonomy CSV as a list of dicts with basic cleaning.

        Args:
          taxonomy_file: Path to a CSV file containing taxonomy rows.

        Returns:
          A cleaned list of dict rows or None if loading fails.
        """
        try:
            rows = read_csv_as_list_of_dicts(taxonomy_file)
            rows = dedupe_and_clean_rows(rows)
            return rows
        except FileNotFoundError:
            logger.warning(f"Taxonomy file not found: {taxonomy_file}")
            return None
        except Exception as e:
            logger.error(
                f"Error loading taxonomy file {taxonomy_file}: {type(e).__name__}"
            )
            return None

    def _is_list_code_format(self, label: str) -> bool:
        """Check if a label appears to already be in LIST code format.

        LIST codes look like "FA-01-00-00-00 > Some Title" or just "FA-01-00-00-00".
        """
        import re

        # LIST code pattern: 2 letters, dash, 2 digits, repeated 4 more times
        pattern = r"^[A-Z]{2}-\d{2}-\d{2}-\d{2}-\d{2}"
        return bool(re.match(pattern, label.strip()))

    def _convert_labels_to_list_format(
        self,
        response: ClassificationResponse,
        taxonomy_name: str,
    ) -> ClassificationResponse:
        """Convert classification labels to LIST taxonomy format.

        Args:
            response: The classification response with labels to convert.
            taxonomy_name: The taxonomy that was used for classification.

        Returns:
            A new ClassificationResponse with labels converted to LIST codes.
        """
        if not response.labels:
            return response

        converted_labels = []
        for label in response.labels:
            original_label = label.label

            # Extract LIST code from already-formatted label if present
            list_code = None
            if self._is_list_code_format(original_label):
                # Extract code from "CODE > Title" format or just "CODE"
                list_code = original_label.split(" > ")[0].strip()
                converted_labels.append(
                    Label(
                        label=original_label,
                        confidence=label.confidence,
                        id=list_code,
                    )
                )
                continue

            if taxonomy_name == "list":
                # Labels are LIST taxonomy titles, convert to codes
                list_code = lookup_list_code_from_title(original_label)
                if list_code:
                    converted_labels.append(
                        Label(
                            label=format_list_label(list_code),
                            confidence=label.confidence,
                            id=list_code,
                        )
                    )
                else:
                    # Keep original if no mapping found (shouldn't happen often)
                    logger.warning("No LIST code found for a returned label")
                    converted_labels.append(label)
            else:
                # Labels are OSB taxonomy, convert via mapping
                result = convert_osb_label_to_list(original_label)
                if result:
                    list_code, list_title = result
                    converted_labels.append(
                        Label(
                            label=format_list_label(list_code),
                            confidence=label.confidence,
                            id=list_code,
                        )
                    )
                else:
                    # No mapping exists - check if label might be a LIST title directly
                    # (This can happen if a classifier returned LIST titles instead of OSB)
                    list_code = lookup_list_code_from_title(original_label)
                    if list_code:
                        converted_labels.append(
                            Label(
                                label=format_list_label(list_code),
                                confidence=label.confidence,
                                id=list_code,
                            )
                        )
                    else:
                        # True unmapped label
                        logger.info("No LIST mapping for a returned OSB label")
                        # Keep the original label but mark it as unmapped
                        converted_labels.append(
                            Label(
                                label=f"(unmapped) {original_label}",
                                confidence=label.confidence,
                            )
                        )

        return response.model_copy(update={"labels": converted_labels})

    async def _classify_with_telemetry(
        self,
        *,
        provider: ClassifierProvider,
        request: ClassificationRequest,
        taxonomy_df: Any,
        final_prompt: str,
        request_span,
        request_id: str,
    ) -> Dict[str, Any]:
        """Execute a provider classify call while emitting generation telemetry."""
        generation = start_provider_generation(
            request_span,
            name=provider.instance_name,
            model=getattr(provider, "model_name", provider.provider_type),
            input_payload={
                "prompt": self._truncate_text(final_prompt),
                "problem_description": self._truncate_text(request.problem_description),
                "taxonomy": request.taxonomy_name,
                "request_id": request_id,
                # Surface configured reasoning effort for telemetry to aid debugging
                "reasoning_effort": GPT_5_REASONING_EFFORT,
                "conversation_id": request.conversation_id,
            },
            metadata={
                "provider": provider.provider_type,
                "request_id": request_id,
                "taxonomy": request.taxonomy_name,
                **(
                    {"conversation_id": request.conversation_id}
                    if request.conversation_id
                    else {}
                ),
            },
        )

        start_time = time.perf_counter()
        try:
            # Build kwargs for classify: include reasoning_effort only when configured
            classify_kwargs: Dict[str, Any] = {"custom_prompt": final_prompt}
            if GPT_5_REASONING_EFFORT is not None:
                classify_kwargs["reasoning_effort"] = GPT_5_REASONING_EFFORT

            # Pass followup_answers if provided for multi-turn refinement
            if request.followup_answers:
                classify_kwargs["followup_answers"] = request.followup_answers

            # Pass taxonomy_format to SPOT provider so it can return LIST codes directly
            # Automatically use LIST format if taxonomy_name is "list"
            if provider.instance_name == "spot":
                taxonomy_format = (
                    "list"
                    if request.taxonomy_name == "list"
                    else request.taxonomy_format
                )
                classify_kwargs["taxonomy_format"] = taxonomy_format

            try:
                result = await asyncio.wait_for(
                    provider.classify(
                        request.problem_description,
                        taxonomy_df,
                        **classify_kwargs,
                    ),
                    timeout=CLASSIFIER_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"Provider '{provider.instance_name}' timed out after "
                    f"{CLASSIFIER_TIMEOUT_SECONDS:.1f}s"
                ) from exc
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError("Provider returned an error")
            self._attach_latency_metadata(result, latency_ms)
            finalize_provider_generation(
                generation,
                output_payload=self._summarize_provider_result(result),
                metadata={
                    "provider": provider.instance_name,
                    "request_id": request_id,
                    "taxonomy": request.taxonomy_name,
                    "latency_ms": latency_ms,
                },
                usage_details=self._extract_usage_details_from_result(result),
            )
            return result
        except Exception as exc:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            finalize_provider_generation(
                generation,
                metadata={
                    "provider": provider.instance_name,
                    "request_id": request_id,
                    "taxonomy": request.taxonomy_name,
                    "latency_ms": latency_ms,
                },
                error=exc,
            )
            raise

    def _attach_latency_metadata(
        self, provider_result: Union[Dict[str, Any], Exception], latency_ms: float
    ) -> None:
        if not isinstance(provider_result, dict):
            return
        telemetry = provider_result.get("telemetry")
        if not isinstance(telemetry, dict):
            telemetry = {}
            provider_result["telemetry"] = telemetry
        telemetry["latency_ms"] = latency_ms

    def _extract_usage_details_from_result(
        self, provider_result: Union[Dict[str, Any], Exception]
    ) -> Optional[Dict[str, int]]:
        if not isinstance(provider_result, dict):
            return None
        telemetry = provider_result.get("telemetry")
        if not isinstance(telemetry, dict):
            return None
        usage_details = telemetry.get("usage_details")
        if not isinstance(usage_details, dict):
            return None
        sanitized: Dict[str, int] = {}
        for key, value in usage_details.items():
            if isinstance(value, (int, float)):
                sanitized[key] = int(value)
        return sanitized or None

    def _summarize_provider_result(
        self, provider_result: Union[Dict[str, Any], Exception]
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(provider_result, dict):
            return None

        summary: Dict[str, Any] = {
            "likely_no_legal_problem": provider_result.get("likely_no_legal_problem")
        }

        labels_summary: List[str] = []
        for entry in provider_result.get("labels", []):
            if isinstance(entry, Label):
                labels_summary.append(entry.label)
            elif isinstance(entry, dict):
                label_value = entry.get("label") or entry.get("category")
                if label_value:
                    labels_summary.append(str(label_value))
        if labels_summary:
            summary["labels"] = labels_summary[:5]

        question_summary: List[str] = []
        for question in provider_result.get("questions", []):
            if isinstance(question, FollowUpQuestion):
                question_summary.append(question.question)
            elif isinstance(question, dict) and question.get("question"):
                question_summary.append(str(question["question"]))
            elif isinstance(question, str):
                question_summary.append(question)
        if question_summary:
            summary["questions"] = question_summary[:3]

        if provider_result.get("error"):
            summary["error"] = provider_result.get("error")

        telemetry = provider_result.get("telemetry")
        if isinstance(telemetry, dict):
            latency_value = telemetry.get("latency_ms")
            if isinstance(latency_value, (int, float)):
                summary["latency_ms"] = latency_value
        return summary

    @staticmethod
    def _truncate_text(value: str, limit: int = 1000) -> str:
        if not value:
            return ""
        if len(value) <= limit:
            return value
        trimmed = value[:limit]
        overflow = len(value) - limit
        return f"{trimmed}...(+{overflow} chars)"
