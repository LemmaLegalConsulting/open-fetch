import os
from typing import Dict, Iterable, List

import yaml

from app.core.config import HINTS_MAPPING
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _normalize_label(label: str) -> str:
    return " ".join(label.lower().strip().split())


def load_hints_for_taxonomy(taxonomy_name: str) -> Dict[str, str]:
    """Load label->hint mapping for a taxonomy name.

    Supports two structures:
    1. general_hint: A single hint applied to all taxonomy terms
    2. hints: Key-by-key mapping for specific terms (optional, can be commented out)

    Returns a normalized mapping (lowercased, collapsed whitespace keys).
    """
    if not taxonomy_name:
        return {}

    file_path = HINTS_MAPPING.get(taxonomy_name)
    if not file_path or not os.path.exists(file_path):
        return {}

    try:
        with open(file_path, "r") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        logger.warning(f"Failed to load taxonomy hints from {file_path}: {exc}")
        return {}

    if not data:
        return {}

    normalized: Dict[str, str] = {}

    # Check for general_hint that applies to all terms
    general_hint = None
    if isinstance(data, dict) and "general_hint" in data:
        general_hint = str(data.get("general_hint", "")).strip()
        if general_hint:
            # Mark that we have a general hint (will be used differently than key-specific hints)
            normalized["__general__"] = general_hint

    # Check for key-specific hints
    if isinstance(data, dict) and "hints" in data and isinstance(data["hints"], dict):
        hints = data["hints"]
        for raw_label, raw_hint in hints.items():
            if raw_label is None or raw_hint is None:
                continue
            label = str(raw_label).strip()
            hint = str(raw_hint).strip()
            if not label or not hint:
                continue
            normalized[_normalize_label(label)] = hint

    return normalized


def build_taxonomy_hints_block(
    taxonomy_name: str,
    labels: Iterable[str],
) -> str:
    """Build a hints block for labels in the current taxonomy.

    If a general_hint exists, use it. Otherwise, include key-specific hints
    for labels that appear in the provided list.
    """
    hints = load_hints_for_taxonomy(taxonomy_name)
    if not hints:
        return ""

    # Check if we have a general hint
    if "__general__" in hints:
        general_hint = hints["__general__"]
        return f"HINTS:\n{general_hint}"

    # Otherwise, build key-specific hints block
    lines: List[str] = []
    seen = set()
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        normalized = _normalize_label(label)
        hint = hints.get(normalized)
        if hint:
            lines.append(f"- {label}: {hint}")

    if not lines:
        return ""

    return "TAXONOMY HINTS (do not include hints in the output labels):\n" + "\n".join(
        lines
    )
