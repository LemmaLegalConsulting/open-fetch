import os
import httpx
from typing import Any, Dict, Optional, List
from typing import Any

if os.getenv("ENV") != "production":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
from app.utils.csv_helpers import read_csv_as_list_of_dicts

from app.providers.base import ClassifierProvider
from app.utils.backoff import run_with_backoff_async
from app.data.list_taxonomy import format_list_label
from app.core.config import (
    SPOT_BACKOFF_MAX_ATTEMPTS,
    SPOT_BACKOFF_MAX_WAIT_SECONDS,
    SPOT_TIMEOUT_SECONDS,
)

MINIMUM_CONFIDENCE_THRESHOLD = 0.4


class SpotProvider(ClassifierProvider):
    """Provider that calls the SPOT taxonomy classification API and maps results."""

    def __init__(self):
        """Initialize the SPOT provider with API credentials and mappings."""
        super().__init__("spot")
        self.api_key = os.environ.get("SPOT_API_KEY")
        self.api_url = "https://spot.suffolklitlab.org/v0/entities-terminal/"
        self.taxonomy_mapping = self._load_taxonomy_mapping()

    def _load_taxonomy_mapping(self):
        """Load mapping from SPOT list codes to local taxonomy categories."""
        script_dir = os.path.dirname(__file__)
        mapping_file_path = os.path.join(
            script_dir, "..", "data", "list_taxonomy_mapping.csv"
        )
        try:
            rows = read_csv_as_list_of_dicts(mapping_file_path)
            if not rows:
                print(f"Warning: Mapping file {mapping_file_path} is empty.")
                return {}
            mapping = {}
            for row in rows:
                list_code = row.get("list_code")
                if list_code is None:
                    continue
                mapping[list_code] = {
                    "category": row.get("mapped_category"),
                    "subcategory": row.get("mapped_subcategory"),
                }
            return mapping
        except FileNotFoundError:
            print(f"Error: Mapping file not found at {mapping_file_path}")
            return {}
        except Exception as e:
            print(f"Error loading taxonomy mapping file {mapping_file_path}: {e}")
            return {}

    async def classify(
        self,
        problem_description: str,
        taxonomy: Any,
        custom_prompt: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        taxonomy_format: Optional[str] = None,
        followup_answers: Optional[List[Any]] = None,
    ) -> Dict[str, List[Any]]:
        """Call the SPOT API and translate labels to the local taxonomy.

        Args:
          problem_description: The input text to classify.
          taxonomy: Unused. Present for API compatibility.
          custom_prompt: Unused. Present for API compatibility.
          reasoning_effort: Unused. Present for API compatibility.
          taxonomy_format: If 'list', return LIST codes directly without mapping to OSB.
          followup_answers: Unused. Present for API compatibility.

        Returns:
          Dict with a "labels" list of dicts with `label` and `confidence`, and an empty "questions" list.
        """
        # custom_prompt is not used by SpotProvider, but kept for API compatibility
        # reasoning_effort accepted for API compatibility but unused
        # followup_answers accepted for API compatibility but unused (SPOT API doesn't support conversation)
        # print(f"SpotProvider.classify: Received problem_description: {problem_description[:100]}...") # Debugging
        # print(f"SpotProvider.classify: Received taxonomy (first 5 rows):\n{taxonomy.head()}") # Debugging

        if not self.api_key:
            raise ValueError("SPOT_API_KEY not found in environment variables.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "text": problem_description,
            "save-text": 0,
            "cutoff-pred": 0.6,
        }

        async def _post_and_validate(client: httpx.AsyncClient) -> httpx.Response:
            """Make POST request and validate response."""
            r = await client.post(self.api_url, headers=headers, json=data)
            try:
                body = r.text or ""
            except Exception:
                body = ""
            if r.status_code == 400 and "rate limit" in body.lower():
                raise httpx.HTTPStatusError(
                    "Rate limit 400", request=r.request, response=r
                )
            r.raise_for_status()
            return r

        try:
            # Use async httpx client
            async with httpx.AsyncClient(timeout=SPOT_TIMEOUT_SECONDS) as client:
                response = await run_with_backoff_async(
                    _post_and_validate,
                    client,
                    max_attempts=SPOT_BACKOFF_MAX_ATTEMPTS,
                    max_wait_seconds=SPOT_BACKOFF_MAX_WAIT_SECONDS,
                )
            spot_response = response.json()

            # If LIST format is requested, return LIST codes directly without mapping
            if taxonomy_format == "list":
                list_labels = []
                for label_data in spot_response.get("labels", []):
                    spot_code = label_data["id"]
                    confidence = label_data.get("pred", 1.0)
                    # Format as "CODE > Title" for consistency
                    formatted_label = format_list_label(spot_code)
                    list_labels.append(
                        {
                            "label": formatted_label,
                            "confidence": min(confidence, 1.0),
                            "id": spot_code,  # Include the LIST code as the ID
                        }
                    )
                return {
                    "labels": list_labels,
                    "questions": [],
                    "likely_no_legal_problem": len(list_labels) == 0
                    or max((label["confidence"] for label in list_labels), default=0.0)
                    < MINIMUM_CONFIDENCE_THRESHOLD,
                    "is_list_format": True,  # Flag to indicate labels are already in LIST format
                }

            # Default: map to OSB taxonomy
            aggregated_labels: Dict[str, float] = {}
            for label_data in spot_response.get("labels", []):
                spot_code = label_data["id"]
                confidence = label_data.get("pred")
                if not isinstance(confidence, (int, float)):
                    continue
                if spot_code in self.taxonomy_mapping:
                    mapped_data = self.taxonomy_mapping[spot_code]
                    category = mapped_data.get("category")
                    subcategory = mapped_data.get("subcategory")
                    if category and subcategory:
                        mapped_label_str = f"{category} > {subcategory}"
                    elif category:
                        mapped_label_str = category
                    else:
                        print(
                            f"Warning: Mapped data for SPOT code {spot_code} is missing category. (from SpotProvider.classify)"
                        )
                        continue
                    aggregated_labels[mapped_label_str] = (
                        aggregated_labels.get(mapped_label_str, 0.0) + confidence
                    )
                else:
                    print(
                        f"Warning: SPOT code {spot_code} not found in taxonomy mapping. (from SpotProvider.classify)"
                    )

            mapped_labels = [
                {"label": label, "confidence": min(conf, 1.0)}
                for label, conf in aggregated_labels.items()
            ]
            max_confidence = max(
                (label["confidence"] for label in mapped_labels), default=0.0
            )
            return {
                "labels": mapped_labels,
                "questions": [],
                "likely_no_legal_problem": len(mapped_labels) == 0
                or max_confidence < MINIMUM_CONFIDENCE_THRESHOLD,
            }
        except httpx.HTTPStatusError as e:
            error_message = f"SPOT request failed ({type(e).__name__})"
            print(error_message)
            is_rate_limit = (
                e.response.status_code == 429 or "rate limit" in str(e).lower()
            )
            result: Dict[str, List[Any]] = {"labels": [], "questions": []}
            result["error"] = error_message
            if is_rate_limit:
                result["rate_limited"] = True
            return result
        except httpx.RequestError as e:
            error_message = f"SPOT request failed ({type(e).__name__})"
            print(error_message)
            result: Dict[str, List[Any]] = {"labels": [], "questions": []}
            result["error"] = error_message
            return result
