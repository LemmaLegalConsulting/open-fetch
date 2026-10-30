import os
from typing import Dict, List

# Define default weights for each classifier provider.
# These should ideally be determined by empirical performance (e.g., F1-score on a validation set).
# The sum of weights doesn't necessarily need to be 1, but relative values matter.
CLASSIFIER_WEIGHTS: Dict[str, float] = {
    "gemini": 0.8,
    "gpt-4.1-mini": 0.8,
    "gpt-4.1-nano": 0.75,
    "gpt-5": 0.9,
    "gpt-5.2": 0.9,
    "spot": 0.6,
    "keyword": 0.5,
    "mistral": 0.8,
}

# List of classifier instance names to enable.
# Use the instance_name defined in the ClassifierProvider (e.g., "openai", "gemini", "keyword").
# You can specify specific model names for LLMs if you have multiple instances.
ENABLED_CLASSIFIERS: List[str] = [
    # "gpt-4.1-mini",
    # "gpt-4.1-nano",
    "gemini",
    "mistral",
    "keyword",
    "spot",
    # "gpt-5",
    "gpt-5.2",
]

_ALLOWED_GPT_5_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
_configured_reasoning_effort = (
    os.getenv("GPT_5_REASONING_EFFORT", "low").strip().lower()
)
GPT_5_REASONING_EFFORT = (
    _configured_reasoning_effort
    if _configured_reasoning_effort in _ALLOWED_GPT_5_REASONING_EFFORTS
    else "low"
)

try:
    _configured_timeout = float(os.getenv("CLASSIFIER_TIMEOUT_SECONDS", "17").strip())
except ValueError:
    _configured_timeout = 17.0
CLASSIFIER_TIMEOUT_SECONDS = _configured_timeout if _configured_timeout > 0 else 17.0

try:
    _configured_semantic_merge_timeout = float(
        os.getenv("SEMANTIC_MERGE_TIMEOUT_SECONDS", "12").strip()
    )
except ValueError:
    _configured_semantic_merge_timeout = 12.0
SEMANTIC_MERGE_TIMEOUT_SECONDS = (
    _configured_semantic_merge_timeout
    if _configured_semantic_merge_timeout > 0
    else 12.0
)

try:
    _configured_telemetry_flush_timeout_ms = int(
        os.getenv("TELEMETRY_FLUSH_TIMEOUT_MS", "1000").strip()
    )
except ValueError:
    _configured_telemetry_flush_timeout_ms = 1000
TELEMETRY_FLUSH_TIMEOUT_MS = (
    _configured_telemetry_flush_timeout_ms
    if _configured_telemetry_flush_timeout_ms > 0
    else 1000
)

try:
    _configured_spot_timeout = float(os.getenv("SPOT_TIMEOUT_SECONDS", "8").strip())
except ValueError:
    _configured_spot_timeout = 8.0
SPOT_TIMEOUT_SECONDS = _configured_spot_timeout if _configured_spot_timeout > 0 else 8.0

try:
    _configured_spot_backoff_max_attempts = int(
        os.getenv("SPOT_BACKOFF_MAX_ATTEMPTS", "2").strip()
    )
except ValueError:
    _configured_spot_backoff_max_attempts = 2
SPOT_BACKOFF_MAX_ATTEMPTS = (
    _configured_spot_backoff_max_attempts
    if _configured_spot_backoff_max_attempts > 0
    else 2
)

try:
    _configured_spot_backoff_max_wait = float(
        os.getenv("SPOT_BACKOFF_MAX_WAIT_SECONDS", "2").strip()
    )
except ValueError:
    _configured_spot_backoff_max_wait = 2.0
SPOT_BACKOFF_MAX_WAIT_SECONDS = (
    _configured_spot_backoff_max_wait
    if _configured_spot_backoff_max_wait > 0
    else 2.0
)

# Mapping of taxonomy names to their respective file paths
TAXONOMY_MAPPING: Dict[str, str] = {
    "default": "app/data/taxonomy.csv",
    "list": "app/data/list-taxonomy.csv",
}

# Optional mapping of taxonomy names to hint files (YAML).
# Missing or unreadable files are treated as "no hints".
HINTS_MAPPING: Dict[str, str] = {
    "default": "app/data/taxonomy_hints_default.yaml",
}

# Decision mode for combining classifier results: "vote" or "first"
DECISION_MODE: str = "vote"

# Detect installed OpenAI SDK version and runtime reasoning parameter support.
# We do a cheap, deterministic check at import time (version-based) so
# other modules can consult these flags rather than re-inspecting on every
# request. This avoids repeated introspection overhead.
OPENAI_SDK_VERSION = "0.0.0"
OPENAI_SUPPORTS_REASONING_OBJECT = False
OPENAI_SUPPORTS_REASONING_EFFORT = False

try:
    import openai as _openai

    OPENAI_SDK_VERSION = getattr(_openai, "__version__", "0.0.0")
    # Simple major-version heuristic: OpenAI Python SDK v2+ uses the newer
    # Responses API shapes (nested `reasoning` object). Older v1.x SDKs used
    # flatter parameter names like `reasoning_effort` in some releases.
    try:
        ver_parts = [int(p) for p in OPENAI_SDK_VERSION.split(".") if p.isdigit()]
        major = ver_parts[0] if len(ver_parts) > 0 else 0
    except Exception:
        major = 0

    if major >= 2:
        OPENAI_SUPPORTS_REASONING_OBJECT = True
        OPENAI_SUPPORTS_REASONING_EFFORT = False
    else:
        OPENAI_SUPPORTS_REASONING_OBJECT = False
        OPENAI_SUPPORTS_REASONING_EFFORT = True
except Exception:
    # If we can't import the SDK at config-import time, fall back to safest
    # defaults (do not assume flat support).
    OPENAI_SDK_VERSION = "0.0.0"
    OPENAI_SUPPORTS_REASONING_OBJECT = False
    OPENAI_SUPPORTS_REASONING_EFFORT = False
