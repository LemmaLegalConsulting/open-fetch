"""Utilities for parsing JSON from LLM responses with various formatting."""

import re
import json
from typing import Any, Dict


def extract_json_from_fenced_code(content: str) -> str:
    """Extract JSON from markdown code fences if present.

    Some LLM providers (e.g., Gemini, Mistral) wrap JSON responses in markdown
    code fences like ```json ... ```. This function extracts the inner JSON.
    If no fences are found, returns the content as-is.

    Handles various fence formats:
    - ```json ... ```
    - ``` ... ```
    - ```\njson\n ... \n```
    - Multiple newlines around content
    - No newlines around content

    Args:
        content: Raw content that may be fenced

    Returns:
        The extracted JSON string or original content (stripped)

    Examples:
        >>> extract_json_from_fenced_code('```json\\n{"key": "value"}\\n```')
        '{"key": "value"}'

        >>> extract_json_from_fenced_code('```json{"key":"value"}```')
        '{"key":"value"}'

        >>> extract_json_from_fenced_code('{"key": "value"}')
        '{"key": "value"}'
    """
    stripped = content.strip()

    # Try multiple fence patterns with increasing flexibility
    # Pattern 1: ```[json]\s*\n...\n``` (newline after opening, newline before closing)
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Pattern 2: ```[json]..``` (no newlines around content, flexible whitespace)
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if match:
        extracted = match.group(1).strip()
        # Skip if we extracted only whitespace or the word "json"
        if extracted and extracted.lower() != "json":
            return extracted

    # Pattern 3: ````[json]..```` (backtick fence variation)
    match = re.search(r"````(?:json)?\s*(.*?)\s*````", stripped, re.DOTALL)
    if match:
        extracted = match.group(1).strip()
        if extracted and extracted.lower() != "json":
            return extracted

    return stripped


def parse_json_from_llm_response(content: str) -> Dict[str, Any]:
    """Parse JSON from an LLM response, handling fenced code blocks and common issues.

    This function:
    1. Extracts JSON from code fences if present
    2. Handles common JSON formatting issues (trailing commas, extra whitespace)
    3. Attempts safe repairs for minor malformations

    Args:
        content: Raw LLM response content

    Returns:
        Parsed JSON as a dict

    Raises:
        json.JSONDecodeError: If the extracted content is not valid JSON after repair attempts
    """
    extracted = extract_json_from_fenced_code(content)

    # First attempt: try parsing as-is
    try:
        return json.loads(extracted)
    except json.JSONDecodeError:
        pass

    # Second attempt: try fixing common issues
    # This is a best-effort repair mechanism for robustness
    repaired = extracted

    # Fix trailing commas in arrays/objects (common LLM error)
    # e.g., [1, 2, 3,] -> [1, 2, 3]
    # Careful to not modify strings containing comma
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)

    # Try parsing the repaired version
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Third attempt: try to extract just the first valid JSON object/array
    # Look for { ... } or [ ... ] at the top level
    for match in re.finditer(r"[\{\[]", extracted):
        start = match.start()
        # Try to find matching closing bracket
        brace_count = 0
        bracket_count = 0
        in_string = False
        escape_next = False

        for i in range(start, len(extracted)):
            char = extracted[i]

            if escape_next:
                escape_next = False
                continue

            if char == "\\":
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if not in_string:
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                elif char == "[":
                    bracket_count += 1
                elif char == "]":
                    bracket_count -= 1

                # Check if we've closed all braces/brackets
                if brace_count == 0 and bracket_count == 0 and i > start:
                    potential_json = extracted[start : i + 1]
                    try:
                        return json.loads(potential_json)
                    except json.JSONDecodeError:
                        break

    # If all else fails, raise the original error
    raise json.JSONDecodeError(
        f"Unable to parse JSON from LLM response after repair attempts", extracted, 0
    )
