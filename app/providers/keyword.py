from app.providers.base import ClassifierProvider
from app.models.api_models import Label
from typing import Any, Dict, Optional, List
import re
import yaml


class KeywordClassifierProvider(ClassifierProvider):
    """Simple keyword-based classifier that matches taxonomy terms in text."""

    def __init__(self):
        """Load keyword mappings and negative indicators from the YAML taxonomy file."""
        super().__init__("keyword")
        self.keyword_mappings = self._load_keyword_mappings()
        self.level1_keywords, self.level2_keywords = self._load_negative_indicators()

    def _load_negative_indicators(self) -> tuple:
        """Load Level1 and Level2 negative indicator keywords from the YAML taxonomy file.

        Returns:
          A tuple of (level1_keywords_list, level2_keywords_list).
        """
        level1 = []
        level2 = []
        try:
            with open("app/data/taxonomy_keywords.yaml", "r") as f:
                taxonomy_data = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError) as e:
            print(f"Error loading negative indicators from YAML: {e}")
            return level1, level2

        if "negative_indicators" in taxonomy_data:
            indicators = taxonomy_data["negative_indicators"]
            if isinstance(indicators, dict):
                level1 = indicators.get("level1", []) or []
                level2 = indicators.get("level2", []) or []

        return level1, level2

    def _load_keyword_mappings(self) -> Dict[str, List[str]]:
        """Load mapping of lowercase keywords to taxonomy labels.

        Returns:
          Dict mapping each keyword to a list of "Category > Subcategory" labels.
        """
        mappings = {}
        try:
            with open("app/data/taxonomy_keywords.yaml", "r") as f:
                taxonomy_data = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError) as e:
            print(f"Error loading keyword mappings: {e}")
            return mappings

        for category, subcategories in taxonomy_data.items():
            if not isinstance(subcategories, dict):
                continue
            for subcategory, keywords in subcategories.items():
                if not isinstance(keywords, list):
                    continue

                combined_label = f"{category} > {subcategory}"
                for keyword in keywords:
                    if isinstance(keyword, str):
                        lower_keyword = keyword.lower()
                        if lower_keyword not in mappings:
                            mappings[lower_keyword] = []
                        mappings[lower_keyword].append(combined_label)
                    # else:
                    # print(f"[WARN] Skipping non-string keyword: {keyword} in {combined_label}")
        return mappings

    async def classify(
        self,
        problem_description: str,
        taxonomy: Any,
        custom_prompt: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        followup_answers: Optional[List[Any]] = None,
    ) -> Dict[str, List[Any]]:
        """Classify using simple keyword presence with word boundaries and negative indicators.

        Args:
          problem_description: The input text to classify.
          taxonomy: Unused. Present for API compatibility.
          custom_prompt: Unused. Present for API compatibility.
          reasoning_effort: Unused. Present for API compatibility.
          followup_answers: Unused. Present for API compatibility.

        Returns:
            Dict with a "labels" list of Label-like objects, empty "questions" list, and likely_no_legal_problem flag.

        Level 1 keywords (if present) immediately signal "likely no legal problem".
        Level 2 keywords signal "likely no legal problem" only if no labels or all labels match Level 2.
        """
        # custom_prompt is not used by KeywordClassifierProvider, but kept for API compatibility
        # reasoning_effort accepted for API compatibility but unused
        # followup_answers accepted for API compatibility but unused (keyword matching doesn't use conversation history)
        found_labels = set()
        lower_description = problem_description.lower()

        # Check for Level 1 keywords: if found, immediately return "likely no legal problem"
        found_level1 = [k for k in self.level1_keywords if k in lower_description]
        if found_level1:
            return {
                "labels": [],
                "questions": [],
                "likely_no_legal_problem": True,
            }

        # Otherwise, look for taxonomy labels
        for keyword, labels in self.keyword_mappings.items():
            try:
                pattern = r"\b" + re.escape(keyword) + r"\b"
                if re.search(pattern, lower_description):
                    for label in labels:
                        found_labels.add(label)
            except re.error as e:
                # print(f"[ERROR] Invalid regex pattern for keyword: '{keyword}'. Error: {e}")
                continue

        # Determine likely_no_legal_problem status
        likely_no_legal_problem = False

        if len(found_labels) == 0:
            # No taxonomy labels found; check for Level 2 keywords
            found_level2 = [k for k in self.level2_keywords if k in lower_description]
            if found_level2:
                likely_no_legal_problem = True
        else:
            # Labels were found; check if they all match Level 2 keywords
            found_level2 = [k for k in self.level2_keywords if k in lower_description]
            if found_level2:
                # Check if all labels contain a Level 2 keyword substring
                if all(
                    any(k in label.lower() for k in self.level2_keywords)
                    for label in found_labels
                ):
                    likely_no_legal_problem = True

        return {
            "labels": [
                Label(label=label, confidence=1.0) for label in list(found_labels)
            ],
            "questions": [],
            "likely_no_legal_problem": likely_no_legal_problem,
        }
