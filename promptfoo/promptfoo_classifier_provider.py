import os
import sys
import asyncio
import warnings
from typing import List, Dict, Any, Optional

# Suppress pkg_resources deprecation warning from scireadability and other packages
warnings.filterwarnings("ignore", category=UserWarning, message=".*pkg_resources.*")

# Suppress asyncio event loop closed warnings that occur when httpx clients try to
# clean up after the event loop is already closed by PromptFoo's worker
warnings.filterwarnings("ignore", message=".*Event loop is closed.*")

# Ensure we can import from the repo root
# When Promptfoo runs this, __file__ is the full path to this file
# We want to add the parent of the promptfoo directory to sys.path
if os.path.isfile(__file__):
    # Normal case: running as a file
    promptfoo_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(promptfoo_dir)
else:
    # Fallback: running from module import
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Change to repo root for relative paths to work
os.chdir(repo_root)

if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from app.services.classification_service import ClassificationService
from app.models.api_models import ClassificationRequest
from app.providers.base import clear_all_prompt_caches


def _format_display(output_json: str) -> str:
    """Format labels and follow-up questions for display in Promptfoo.

    Embeds the raw JSON at the end in a special marker so transforms can extract it.
    """
    try:
        import json

        data = json.loads(output_json)
        lines: List[str] = []

        if "labels" in data and isinstance(data["labels"], list) and data["labels"]:
            lines.append("Classification:")
            for label in data["labels"]:
                label_text = label.get("label", "Unknown")
                confidence = label.get("confidence", 0)
                lines.append(f"  - {label_text} ({confidence:.2f})")

        questions = []
        if isinstance(data.get("follow_up_questions"), list):
            for q_item in data["follow_up_questions"]:
                if isinstance(q_item, dict) and q_item.get("question"):
                    questions.append(q_item)

        if questions:
            lines.append("")
            lines.append("Follow-up Questions:")
            for i, q_item in enumerate(questions, 1):
                question = q_item.get("question", "").strip()
                options = q_item.get("options", [])
                lines.append(f"Q{i}: {question}")
                if options and isinstance(options, list) and len(options) > 0:
                    for opt in options:
                        if isinstance(opt, str):
                            lines.append(f"  • {opt.strip()}")
                else:
                    lines.append("  (open-ended)")
        else:
            lines.append("")
            lines.append("(No follow-up questions)")

        # Embed raw JSON for assertion transforms to extract (hidden in HTML comment)
        # This is invisible in most displays but parseable by transforms
        lines.append("")
        lines.append(f"<!-- RAW_JSON:{output_json} -->")

        return "\n".join(lines).strip()
    except Exception as exc:
        return f"ERROR: {exc}"


async def call_api(
    prompt: str, options: Dict[str, Any], context: Dict[str, Any]
) -> Dict[str, Any]:
    """Main function for PromptFoo to call.

    NOTE: All prompt caches (LRU + rendered) are cleared at the start of each invocation.
    This is necessary because:
    1. Python's @lru_cache on _load_prompt_template() persists across test cases
    2. The _rendered_prompt_cache dict also persists and uses (path, taxonomy_hash) as key,
       which doesn't detect file content changes on disk
    Clearing both ensures fresh prompt files are read from disk before each evaluation.
    """

    # Clear ALL Python caches for prompts (LRU + rendered dict)
    clear_all_prompt_caches()

    config = options.get("config", {})
    enabled_providers = config.get("enabled_providers")
    decision_mode = config.get("decision_mode", "vote")
    taxonomy_name = config.get("taxonomy_name", "default")
    include_debug_details = config.get("include_debug_details", False)
    cache_enabled = config.get("cache_enabled", False)
    cache_dir = config.get("cache_dir", "./cache")
    display_format = bool(config.get("display_format", False))
    disable_telemetry = bool(config.get("disable_telemetry", False))
    enable_screening_protocols = bool(config.get("enable_screening_protocols", True))
    legacy_screening = bool(config.get("legacy_screening", True))

    problem_description = context.get("vars", {}).get("problem_description", prompt)

    if disable_telemetry:
        os.environ["TELEMETRY_ENABLED"] = "0"

    # Instantiate ClassificationService with specific overrides
    service = ClassificationService(
        enabled_providers_override=enabled_providers,
        cache_enabled=cache_enabled,
        cache_dir=cache_dir,
    )

    request = ClassificationRequest(
        problem_description=problem_description,
        taxonomy_name=taxonomy_name,
        decision_mode=decision_mode,
        include_debug_details=include_debug_details,
        enable_screening_protocols=enable_screening_protocols,
        legacy_screening=legacy_screening,
    )

    try:
        response = await service.classify(request)

        # Convert Label objects to dictionaries for JSON serialization
        labels_as_dicts = [label.model_dump() for label in response.labels]
        follow_up_questions_as_dicts = [
            q.model_dump() for q in response.follow_up_questions
        ]

        output = {
            "labels": labels_as_dicts,
            "follow_up_questions": follow_up_questions_as_dicts,
        }

        # Flatten the output for promptfoo
        if labels_as_dicts:
            output["label"] = labels_as_dicts[0]["label"]
            output["confidence"] = labels_as_dicts[0]["confidence"]
        if follow_up_questions_as_dicts:
            output["question"] = follow_up_questions_as_dicts[0]["question"]
            output["format"] = follow_up_questions_as_dicts[0]["format"]
            output["options"] = follow_up_questions_as_dicts[0]["options"]

        if include_debug_details:
            output["raw_provider_results"] = response.raw_provider_results
            output["weighted_label_scores"] = response.weighted_label_scores
            output["weighted_question_scores"] = response.weighted_question_scores
            # Add a compact debug summary for easier viewing in PromptFoo cells
            debug_summary = {}
            if response.raw_provider_results:
                for provider_name, res in response.raw_provider_results.items():
                    if isinstance(res, dict):
                        dbg = res.get("debug")
                        err = res.get("error")
                        if dbg or err:
                            debug_summary[provider_name] = {
                                "error": err,
                                "debug": dbg,
                            }
            if debug_summary:
                output["debug"] = debug_summary

        # Check for errors in the raw provider results
        errors = []
        if response.raw_provider_results:
            for provider, result in response.raw_provider_results.items():
                if result and isinstance(result, dict) and "error" in result:
                    errors.append(f"{provider}: {result['error']}")

        if errors:
            output["errors"] = errors
        elif not labels_as_dicts:
            # If no labels and no explicit errors, hint that no content parsed
            output.setdefault("errors", []).append(
                "No labels parsed from provider output."
            )

        import json

        # Ensure the output is JSON serializable by converting Label and FollowUpQuestion objects
        def serialize_obj(obj):
            """Convert non-serializable objects to dicts."""
            if hasattr(obj, "model_dump"):
                return obj.model_dump()
            elif hasattr(obj, "__dict__"):
                return obj.__dict__
            else:
                return str(obj)

        try:
            output_json = json.dumps(output, default=serialize_obj)
        except Exception as e:
            import json as json_module

            error_response = {
                "output": json_module.dumps({"error": f"Serialization error: {str(e)}"})
            }
            return error_response

        if display_format:
            return {
                "output": _format_display(output_json),
                "metadata": {"raw_json": output_json},
            }

        return {"output": output_json}
    except Exception as e:
        import json

        return {"output": json.dumps({"error": str(e)})}
    finally:
        # Cleanup async resources before event loop closes
        try:
            await service.cleanup()
        except Exception as e:
            # Suppress cleanup errors to avoid masking the actual exception
            pass
