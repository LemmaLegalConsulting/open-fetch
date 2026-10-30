from __future__ import annotations

from typing import Any, Dict, List, Optional

USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
)


def build_messages(
    prompt: str,
    problem_description: str,
    followup_answers: Optional[List[Any]] = None,
) -> List[Dict[str, str]]:
    """Build the message list for chat completions.

    Constructs a conversation with:
    1. System message with taxonomy/instructions
    2. User message with initial problem description
    3. If followup_answers provided: alternating assistant/user messages for Q&A
    4. Final user message requesting refined classification

    Args:
      prompt: The system prompt with taxonomy.
      problem_description: The user's initial problem description.
      followup_answers: Optional list of FollowUpAnswer objects or dicts for multi-turn.

    Returns:
      List of message dicts with 'role' and 'content' keys.
    """
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": problem_description},
    ]

    # Add follow-up Q&A pairs if provided
    if followup_answers:
        # Collect the questions that have been answered
        answered_questions: List[str] = []

        for answer in followup_answers:
            # Handle both FollowUpAnswer objects and dicts
            if hasattr(answer, "question"):
                q, a = answer.question, answer.answer
            else:
                q, a = answer.get("question", ""), answer.get("answer", "")
            answered_questions.append(q)
            # Assistant asks the question
            messages.append({"role": "assistant", "content": q})
            # User provides their answer
            messages.append({"role": "user", "content": a})

        # Add final instruction to re-classify, explicitly listing questions not to repeat
        messages.append(
            {
                "role": "user",
                "content": (
                    "Based on my original problem and all my answers above, "
                    "please provide your final classification as JSON. "
                    "Do NOT ask any of these questions again, as I have already answered them:\n"
                    + "\n".join(f"- {q}" for q in answered_questions)
                ),
            }
        )

    return messages


def extract_usage_details(response: Any) -> Optional[Dict[str, int]]:
    """Best-effort extraction of token usage metrics from OpenAI-compatible responses."""
    usage = (
        response.get("usage")
        if isinstance(response, dict)
        else getattr(response, "usage", None)
    )
    if usage is None:
        return None

    usage_data: Dict[str, Any] = {}

    for attr in ("model_dump", "to_dict", "dict"):
        if hasattr(usage, attr):
            try:
                usage_data = getattr(usage, attr)()
                break
            except Exception:  # pragma: no cover - defensive
                usage_data = {}

    if not usage_data and isinstance(usage, dict):
        usage_data = usage

    if not usage_data:
        for key in USAGE_KEYS:
            value = getattr(usage, key, None)
            if isinstance(value, (int, float)):
                usage_data[key] = value

    normalized: Dict[str, int] = {}
    for key, value in usage_data.items():
        if isinstance(value, (int, float)):
            normalized[key] = int(value)

    return normalized or None


def build_basic_telemetry(
    *,
    provider: str,
    instance_name: str,
    model_name: Optional[str],
    response: Any,
    raw_content: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a telemetry payload shared by multiple providers."""
    payload: Dict[str, Any] = {
        "provider": provider,
        "instance_name": instance_name,
        "model": model_name,
    }
    usage_details = extract_usage_details(response)
    if usage_details:
        payload["usage_details"] = usage_details

    response_id = getattr(response, "id", None)
    if response_id:
        payload["response_id"] = response_id

    if isinstance(raw_content, str):
        payload["raw_response_preview"] = raw_content[:500]

    return payload


def extract_response_text(response):
    """Read Responses API output consistently after initial and retried calls."""
    if getattr(response, "output_text", None):
        return response.output_text
    return "".join(
        part.text
        for item in getattr(response, "output", []) or []
        for part in getattr(item, "content", []) or []
        if getattr(part, "type", None) in {"output_text", "text"}
        and isinstance(getattr(part, "text", None), str)
    )
