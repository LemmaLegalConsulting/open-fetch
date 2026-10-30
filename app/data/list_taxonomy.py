"""LIST taxonomy utilities.

This module provides functions for working with the LIST (Legal Issues Taxonomy)
codes, including:
- Loading a simplified version of the LIST taxonomy for LLM prompts (title only)
- Mapping from LIST category titles back to LIST codes
- Reverse mapping from OSB taxonomy categories to LIST codes
"""

import os
from typing import Dict, Optional, Tuple, Any, List
from functools import lru_cache
from app.utils.csv_helpers import read_csv_as_list_of_dicts, dedupe_and_clean_rows

# Module-level paths
_DATA_DIR = os.path.dirname(__file__)
LIST_TAXONOMY_FILE = os.path.join(_DATA_DIR, "list-taxonomy.csv")
LIST_MAPPING_FILE = os.path.join(_DATA_DIR, "list_taxonomy_mapping.csv")


@lru_cache(maxsize=1)
def load_list_taxonomy_simple() -> List[Dict[str, Any]]:
    """Load the LIST taxonomy with only the Title column.

    Returns:
        A list of dicts where each dict has a single key 'Category'. Empty rows are removed.
    """
    rows = read_csv_as_list_of_dicts(LIST_TAXONOMY_FILE)
    # Keep only the Title column, renamed to Category, strip/clean duplicates and empty rows
    simple = []
    for r in rows:
        title = r.get("Title")
        if title is None:
            continue
        simple.append({"Category": title})
    return dedupe_and_clean_rows(simple)


@lru_cache(maxsize=1)
def get_list_title_to_code_mapping() -> Dict[str, str]:
    """Build a mapping from LIST category titles to their LIST codes.

    Returns:
        Dict mapping lowercase title strings to LIST codes.
    """
    rows = read_csv_as_list_of_dicts(LIST_TAXONOMY_FILE)
    mapping: Dict[str, str] = {}
    for row in rows:
        code = row.get("Code")
        title = row.get("Title")
        if code is not None and title is not None:
            mapping[str(title).lower().strip()] = str(code).strip()
    return mapping


def lookup_list_code_from_title(title: str) -> Optional[str]:
    """Look up the LIST code for a given category title.

    Handles various formatting issues like extra spaces, punctuation, and case variations.
    Uses fuzzy matching to handle common LLM variations.

    Args:
        title: The category title to look up (case-insensitive, punctuation-tolerant).

    Returns:
        The LIST code (e.g., "WO-11-00-00-00") or None if not found.
    """
    import re
    from difflib import SequenceMatcher

    mapping = get_list_title_to_code_mapping()

    # First, try exact match (after normalization)
    normalized_title = title.lower().strip()
    if normalized_title in mapping:
        return mapping[normalized_title]

    # Try with normalized whitespace (collapse multiple spaces)
    normalized_title = re.sub(r"\s+", " ", title).lower().strip()
    if normalized_title in mapping:
        return mapping[normalized_title]

    # Normalize both the input and keys for fuzzy matching
    def normalize_for_matching(text: str) -> str:
        """Normalize text for fuzzy matching."""
        # Replace common word variations
        text = text.replace("&", "and")
        text = re.sub(r"[\s\-_.()]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.lower().strip()

    normalized_input = normalize_for_matching(title)

    # Try exact match after normalization
    if normalized_input in mapping:
        return mapping[normalized_input]

    # Try fuzzy matching with sequence similarity
    best_match = None
    best_ratio = 0.75  # Require at least 75% similarity

    for key in mapping.keys():
        normalized_key = normalize_for_matching(key)

        # Check for exact match after normalization
        if normalized_input == normalized_key:
            return mapping[key]

        # Compute similarity ratio
        ratio = SequenceMatcher(None, normalized_input, normalized_key).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = mapping[key]

    return best_match


@lru_cache(maxsize=1)
def get_osb_to_list_mapping() -> Dict[str, str]:
    """Build a reverse mapping from OSB taxonomy labels to LIST codes.

    Returns:
        Dict mapping "Category > Subcategory" strings to LIST codes.
    """
    rows = read_csv_as_list_of_dicts(LIST_MAPPING_FILE)
    mapping: Dict[str, str] = {}
    for row in rows:
        list_code = row.get("list_code")
        category = row.get("mapped_category")
        subcategory = row.get("mapped_subcategory")

        if list_code is not None and category is not None:
            if subcategory is not None:
                osb_label = f"{category} > {subcategory}"
            else:
                osb_label = str(category)

            osb_key = osb_label.lower().strip()
            if osb_key not in mapping:
                mapping[osb_key] = str(list_code).strip()
    return mapping


def convert_osb_label_to_list(osb_label: str) -> Optional[Tuple[str, str]]:
    """Convert an OSB taxonomy label to a LIST code and title.

    Handles various formatting issues with fuzzy matching.

    Args:
        osb_label: OSB taxonomy label in "Category > Subcategory" format.

    Returns:
        Tuple of (list_code, list_title) or None if no mapping exists.
    """
    import re
    from difflib import SequenceMatcher

    mapping = get_osb_to_list_mapping()

    # Try exact match first
    normalized_input = osb_label.lower().strip()
    list_code = mapping.get(normalized_input)

    if not list_code:
        # Try with normalized whitespace
        normalized_input = re.sub(r"\s+", " ", osb_label).lower().strip()
        list_code = mapping.get(normalized_input)

    if not list_code:
        # Try fuzzy matching with punctuation normalization
        def normalize_for_matching(text: str) -> str:
            """Normalize text for fuzzy matching."""
            # Replace common word variations
            text = text.replace("&", "and")
            text = re.sub(r"[\s\-_.()]+", " ", text)
            text = re.sub(r"\s+", " ", text)
            return text.lower().strip()

        normalized_input = normalize_for_matching(osb_label)

        best_match_code = None
        best_ratio = 0.75  # Require at least 75% similarity

        for key in mapping.keys():
            normalized_key = normalize_for_matching(key)

            if normalized_input == normalized_key:
                list_code = mapping[key]
                break

            # Compute similarity ratio
            ratio = SequenceMatcher(None, normalized_input, normalized_key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match_code = mapping[key]

        if not list_code:
            list_code = best_match_code

    if list_code:
        # Get the title for this LIST code
        title_mapping = get_list_title_to_code_mapping()
        # Invert to get code->title
        code_to_title = {v: k for k, v in title_mapping.items()}
        title = code_to_title.get(list_code)
        if title:
            return (list_code, title.title())  # Return with title case
        return (list_code, list_code)  # Fall back to code as title

    return None


@lru_cache(maxsize=1)
def get_list_code_to_title_mapping() -> Dict[str, str]:
    """Build a mapping from LIST codes to their titles.

    Returns:
        Dict mapping LIST codes to their title strings.
    """
    rows = read_csv_as_list_of_dicts(LIST_TAXONOMY_FILE)
    mapping: Dict[str, str] = {}
    for row in rows:
        code = row.get("Code")
        title = row.get("Title")
        if code is not None and title is not None:
            mapping[str(code).strip()] = str(title).strip()
    return mapping


def format_list_label(list_code: str) -> str:
    """Format a LIST code with its title for display.

    Args:
        list_code: A LIST code like "WO-11-00-00-00".

    Returns:
        Formatted string like "WO-11-00-00-00 > Title" or just the code if title not found.
    """
    code_to_title = get_list_code_to_title_mapping()
    title = code_to_title.get(list_code)
    if title:
        return f"{list_code} > {title}"
    return list_code
