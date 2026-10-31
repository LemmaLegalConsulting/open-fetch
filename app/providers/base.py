from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Iterable
from functools import lru_cache
import hashlib
import os
import csv
import io
import inspect
from app.utils.logging import get_logger
from app.data.taxonomy_hints import build_taxonomy_hints_block

logger = get_logger(__name__)


# Cache for prompt templates (file content only, not taxonomy-rendered)
@lru_cache(maxsize=32)
def _load_prompt_template(prompt_path: str) -> str:
    """Load and cache a prompt template file.

    Args:
      prompt_path: Path to the prompt template file.

    Returns:
      The raw prompt template content.
    """
    with open(prompt_path, "r") as f:
        return f.read()


def clear_prompt_template_cache():
    """Clear the prompt template LRU cache.

    Call this before each evaluation run to ensure prompt file changes
    are properly reflected. This is critical for iterative prompt testing.
    """
    _load_prompt_template.cache_clear()


def clear_rendered_prompt_cache():
    """Clear the rendered prompt cache dictionary.

    This cache stores (final_prompt, prompt_template) tuples keyed by
    (prompt_path, taxonomy_hash). Must be cleared when prompt FILES change
    on disk, otherwise the old rendered prompts will be returned.
    """
    _rendered_prompt_cache.clear()


def clear_all_prompt_caches():
    """Clear both the LRU cache and rendered prompt cache.

    Call this at the start of each test case invocation to ensure
    prompt file changes are reflected in evaluations.
    """
    clear_prompt_template_cache()
    clear_rendered_prompt_cache()


def _compute_taxonomy_hash(taxonomy: Any) -> str:
    """Compute a hash of the taxonomy for caching purposes.

    Accepts either a pandas DataFrame (kept for backwards compatibility) or a
    sequence of dicts/lists. For sequences, this serializes to a CSV-like
    string to produce a stable hash.
    """
    # Prefer pandas' to_csv if available
    if hasattr(taxonomy, "to_csv"):
        try:
            taxonomy_str = taxonomy.to_csv(index=False)
            return hashlib.blake2b(taxonomy_str.encode()).hexdigest()
        except Exception:
            pass

    # Serialize generic sequences to CSV string using csv module
    buf = io.StringIO()
    writer = csv.writer(buf)

    # If taxonomy is a sequence of dicts, write header and rows
    try:
        first = next(iter(taxonomy))
    except Exception:
        first = None

    if isinstance(first, dict):
        headers = list(first.keys())
        writer.writerow(headers)
        for row in taxonomy:
            writer.writerow(
                [row.get(h) if row.get(h) is not None else "" for h in headers]
            )
    else:
        # Assume rows are iterable sequences
        for row in taxonomy:
            if row is None:
                continue
            writer.writerow([c if c is not None else "" for c in row])

    taxonomy_str = buf.getvalue()
    return hashlib.blake2b(taxonomy_str.encode()).hexdigest()


# Cache for rendered prompts (template + taxonomy)
_rendered_prompt_cache: Dict[Tuple[str, str, str], Tuple[str, str]] = {}


def load_prompt(
    provider_type: str,
    taxonomy: Any,
    skip_followups: bool = False,
    taxonomy_name: Optional[str] = None,
) -> tuple[str, str]:
    """Load and render a prompt template for a provider.

    Args:
      provider_type: Provider key used to select a prompt template file, e.g., "openai".
      taxonomy: DataFrame whose rows are joined into a taxonomy string to inject in the template.
      skip_followups: If True, use the no-followups variant of the prompt template.

    Returns:
      A tuple of (final_prompt, prompt_template) where `final_prompt` has the
      taxonomy injected. If the rendered prompt is effectively empty, a default
      fallback prompt string is returned as `final_prompt` while preserving
      the original `prompt_template` content in the second element.
    """
    # Determine the prompt file to use
    if skip_followups:
        # Try provider-specific no-followups prompt first, then fall back to default
        prompt_path = f"app/prompts/{provider_type}_no_followups.txt"
        if not os.path.exists(prompt_path):
            prompt_path = "app/prompts/default_no_followups.txt"
    else:
        prompt_path = f"app/prompts/{provider_type}.txt"
        if not os.path.exists(prompt_path):
            prompt_path = "app/prompts/default.txt"

    # Check cache with taxonomy hash and hint hash
    taxonomy_hash = _compute_taxonomy_hash(taxonomy)
    cache_key = (prompt_path, taxonomy_hash, "no-hints")
    if cache_key in _rendered_prompt_cache:
        return _rendered_prompt_cache[cache_key]

    # Load template from file cache
    prompt_template = _load_prompt_template(prompt_path)
    if "{{classification_instructions}}" in prompt_template:
        prompt_template = prompt_template.replace(
            "{{classification_instructions}}",
            _load_prompt_template("app/prompts/classification_instructions.txt"),
        )

    # Build taxonomy string from either a DataFrame or a generic sequence of rows/dicts
    parts: List[str] = []
    labels: List[str] = []

    if hasattr(taxonomy, "iterrows"):
        for _, row in taxonomy.iterrows():
            # Drop Nones/NaNs and convert to strings
            values = [str(v) for v in row.tolist() if v is not None and str(v) != ""]
            if values:
                label = " > ".join(values)
                parts.append(label)
                labels.append(label)
    else:
        # taxonomy may be a list of dicts or list of sequences
        for row in taxonomy:
            if row is None:
                continue
            if isinstance(row, dict):
                values = [
                    str(v) for v in row.values() if v is not None and str(v) != ""
                ]
            else:
                # assume iterable
                values = [str(v) for v in row if v is not None and str(v) != ""]
            if values:
                label = " > ".join(values)
                parts.append(label)
                labels.append(label)

    taxonomy_str = "\n".join(parts)
    if not taxonomy_str:
        taxonomy_str = "No specific legal taxonomy categories were provided or loaded."

    hints_block = build_taxonomy_hints_block(taxonomy_name or "", labels)
    if hints_block:
        hints_hash = hashlib.blake2b(hints_block.encode()).hexdigest()
        cache_key = (prompt_path, taxonomy_hash, hints_hash)
    else:
        cache_key = (prompt_path, taxonomy_hash, "no-hints")
    if cache_key in _rendered_prompt_cache:
        return _rendered_prompt_cache[cache_key]

    final_prompt = prompt_template.replace("{{taxonomy}}", taxonomy_str)
    if "{{taxonomy_hints}}" in final_prompt:
        final_prompt = final_prompt.replace("{{taxonomy_hints}}", hints_block)
    elif hints_block:
        final_prompt = f"{final_prompt}\n\n{hints_block}"

    if (
        not final_prompt.strip()
    ):  # Check if the prompt is effectively empty (only whitespace)
        print(
            f"Warning: Generated prompt for {provider_type} is empty or only whitespace. Taxonomy: '{taxonomy_str}'"
        )
        fallback = (
            "Please classify the legal problem based on the provided information."
        )
        result = (fallback, prompt_template)
    else:
        result = (final_prompt, prompt_template)

    # Cache the result
    _rendered_prompt_cache[cache_key] = result
    return result


class ClassifierProvider(ABC):
    """Abstract classifier provider.

    Subclasses implement a `classify` coroutine that returns labels and optional
    follow-up questions for a given problem description, optionally using a
    taxonomy-aware prompt.
    """

    def __init__(self, provider_type: str, model_name: Optional[str] = None):
        """Initialize the provider.

        Args:
          provider_type: Provider identifier, e.g., "openai", "gemini", "keyword".
          model_name: Optional concrete model name for LLM providers.
        """
        self.provider_type = provider_type  # e.g., 'openai', 'gemini', 'keyword'
        self.model_name = (
            model_name  # Specific model name for LLMs, e.g., 'gpt-4.1-mini'
        )
        self.instance_name = (
            model_name if model_name else provider_type
        )  # Unique identifier for this instance

    async def aclose(self) -> None:
        """Close any underlying async client if present."""
        client = getattr(self, "client", None)
        close_fn = getattr(client, "close", None) if client is not None else None
        if close_fn is None:
            return
        try:
            result = close_fn()
            if inspect.isawaitable(result):
                await result
        except RuntimeError as exc:
            # Ignore loop-closed errors during shutdown (common in worker teardown).
            if "Event loop is closed" in str(exc):
                return
            raise
        except Exception as exc:
            logger.debug(
                f"Error closing client for {self.instance_name}: {type(exc).__name__}"
            )

    @abstractmethod
    async def classify(
        self,
        problem_description: str,
        taxonomy: Any,
        custom_prompt: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        followup_answers: Optional[List[Any]] = None,
    ) -> Dict[str, List[Any]]:
        """Classify a problem into taxonomy categories.

        Args:
          problem_description: Natural language description of the legal problem.
          taxonomy: DataFrame representing the taxonomy to consider.
          custom_prompt: Optional pre-rendered prompt to use instead of the default template.
          reasoning_effort: Optional reasoning effort level for supported models.
          followup_answers: Optional list of FollowUpAnswer objects containing Q&A pairs
            from a previous classification. When provided, builds a multi-turn conversation
            to refine the classification based on user responses.

        Returns:
          A mapping with keys like "labels" and "questions" whose values are lists.
          Implementations may return Pydantic objects or plain dicts/strings inside the lists.
        """

    pass


class LLMClassifierProvider(ClassifierProvider, ABC):
    """Base class for LLM-backed classifier providers."""

    def __init__(self, provider_type: str, model_name: str):
        """Initialize an LLM provider and its API client.

        Args:
          provider_type: Provider identifier, e.g., "openai".
          model_name: Concrete model name for the LLM.
        """
        super().__init__(provider_type, model_name=model_name)
        self.client = self._get_client()

    @abstractmethod
    def _get_client(self):
        """Return an API client instance for the underlying LLM SDK."""
        pass
