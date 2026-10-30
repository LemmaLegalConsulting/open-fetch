import json
import os
import re
import sys
import warnings
from typing import Any, Dict, List, Set, cast

# --- Optional deps ---
try:
    warnings.filterwarnings("ignore", category=UserWarning, message=".*pkg_resources.*")
    import nltk
    import textstat
    import scireadability
    from nltk.stem import PorterStemmer

    SCIREADABILITY_AVAILABLE = True
except ImportError:
    SCIREADABILITY_AVAILABLE = False
except Exception as e:
    # If nltk data download fails, still mark as available (we may fall back gracefully)
    SCIREADABILITY_AVAILABLE = True
    warnings.warn(f"NLTK initialization issue: {e}", stacklevel=1)

# Cache the combined easy words set (base: Dale-Chall + legal terms)
_EASY_WORDS_CACHE: "Set[str] | None" = None

# Cache the stemmer
_STEMMER: "PorterStemmer | None" = None


def _get_stemmer() -> "PorterStemmer":
    global _STEMMER
    if _STEMMER is None:
        _STEMMER = PorterStemmer()
    return _STEMMER


def _load_scireadability_dale_chall_easy_words() -> Set[str]:
    """
    Load scireadability's Dale-Chall EASY_WORDS from the resources file.

    Important:
      - This set is already lowercased and Porter-stemmed (per scireadability design).
      - Do NOT stem these again.
    """
    if not SCIREADABILITY_AVAILABLE:
        return set()

    try:
        import scireadability
        import os

        # Read the easy words file directly
        easy_words_path = os.path.join(
            os.path.dirname(scireadability.__file__),
            "resources",
            "en",
            "easy_words.txt",
        )
        with open(easy_words_path, "r", encoding="utf-8") as f:
            # Read lines, strip whitespace, filter out empty lines and comments
            words = {
                line.strip().lower()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            }

        # Apply Porter stemming as scireadability does
        stemmer = _get_stemmer()
        stemmed_words = {stemmer.stem(word) for word in words}

        return stemmed_words

    except Exception as e:
        # If file reading fails, we fail open with an empty set
        # (but that will inflate grades, so we warn loudly).
        warnings.warn(
            f"Could not load scireadability easy words file. Dale-Chall results may be inflated. Error: {e}",
            stacklevel=1,
        )
        return set()


def _tokenize_words(text: str) -> List[str]:
    """
    Tokenize to alphabetic tokens only, to avoid artifacts like "s" from "Meyer's"
    and to reduce punctuation/hyphen weirdness.
    """
    if not text:
        return []
    # Keep only letters (a-z). This turns "third-party" -> ["third", "party"] (fine).
    words = re.findall(r"\b[a-z]+\b", text.lower())
    # Drop 1-letter tokens (e.g., stray "s")
    return [w for w in words if len(w) > 1]


def _split_sentences(text: str) -> List[str]:
    # Simple sentence split good enough for DC (question prompts are short)
    return [s for s in re.split(r"[.!?]+", text) if s.strip()]


def _stem_words(words: Set[str]) -> Set[str]:
    stemmer = _get_stemmer()
    return {stemmer.stem(w.lower().strip()) for w in words if w and w.strip()}


def _build_easy_words_set(additional_words: Set[str] | None = None) -> Set[str]:
    """
    Build combined easy words set from:
      1) scireadability's Dale-Chall easy words (already stemmed)
      2) custom legal vocabulary from legal_easy_words.txt (stemmed to match)
      3) optional additional words (e.g., from user query) (stemmed to match)

    Note: We cache only the BASE set (Dale-Chall + legal). If additional_words is
    provided, we return a new union to avoid cache pollution.
    """
    global _EASY_WORDS_CACHE

    base_ready = _EASY_WORDS_CACHE is not None

    if additional_words is None and base_ready:
        return _EASY_WORDS_CACHE  # type: ignore[return-value]

    # --- Start with scireadability Dale-Chall set (stemmed already) ---
    base_easy = _load_scireadability_dale_chall_easy_words()

    # --- Add legal vocab (stemmed) ---
    legal_easy: Set[str] = set()
    try:
        stemmer = _get_stemmer()
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "legal_easy_words.txt"
        )
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                for line in f:
                    word = line.strip().lower()
                    if not word or word.startswith("#"):
                        continue
                    # Add ONLY the stem form to match scireadability's list
                    legal_easy.add(stemmer.stem(word))
        else:
            print(
                f"Warning: Custom easy words file not found at {config_path}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"Warning: Could not load custom easy words: {e}", file=sys.stderr)

    base_easy |= legal_easy

    # Cache the base set (no additional words)
    if additional_words is None:
        _EASY_WORDS_CACHE = base_easy
        return base_easy

    # If additional words provided: stem and union, without polluting base cache
    return base_easy | _stem_words(additional_words)


def _calculate_dale_chall_grade(text: str, easy_words: Set[str] | None = None) -> float:
    """
    Calculate Dale-Chall readability grade using the standard Dale-Chall formula,
    with an easy-word vocabulary of:
      Dale-Chall list (from scireadability, stemmed) ∪ legal ∪ user words (stemmed)

    Returns:
        Grade level (0-16). Note: Dale-Chall raw scores can exceed 16, but we clamp.
    """
    if not text or not text.strip():
        return 0.0

    if not SCIREADABILITY_AVAILABLE:
        return 0.0

    try:
        if easy_words is None:
            easy_words = _build_easy_words_set()

        stemmer = _get_stemmer()

        words = _tokenize_words(text)
        sentences = _split_sentences(text)

        if not words:
            return 0.0

        total_words = len(words)
        total_sentences = max(len(sentences), 1)
        asl = total_words / total_sentences

        # hard word if its stem is NOT in the easy set
        hard_words = 0
        for w in words:
            if stemmer.stem(w) not in easy_words:
                hard_words += 1

        pdw = (hard_words / total_words) * 100.0  # 0-100

        # Standard Dale–Chall (New) raw score formula:
        # Raw Score = 0.1579 * PDW + 0.0496 * ASL + 3.6365
        grade = 0.1579 * pdw + 0.0496 * asl + 3.6365

        # Some references apply an adjustment if PDW > 5%; the constant term above
        # already reflects the "new" formula constant. We keep your previous behavior.

        # Clamp to reasonable range for display/scoring
        grade = max(0.0, min(grade, 16.0))
        return grade
    except Exception as e:
        print(f"Warning: Error calculating Dale-Chall grade: {e}", file=sys.stderr)
        return 0.0


def _calculate_fkgl_grade(text: str) -> float:
    """
    Calculate Flesch-Kincaid Grade Level (FKGL) for annotation purposes.
    This is NOT used for Dale-Chall pass/fail decision, only informational display.
    """
    if not text or not text.strip():
        return 0.0

    if not SCIREADABILITY_AVAILABLE:
        return 0.0

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*not a zip file.*")
            warnings.filterwarnings("ignore", category=UserWarning)
            return float(textstat.flesch_kincaid_grade(text))
    except (FileNotFoundError, RuntimeError) as e:
        if "not a zip file" not in str(e):
            warnings.warn(f"Error calculating FKGL grade: {e}", stacklevel=1)
        return 0.0
    except Exception as e:
        warnings.warn(f"Error calculating FKGL grade: {e}", stacklevel=1)
        return 0.0


def _extract_all_question_texts(raw_output: str) -> List[str]:
    """
    Extract all follow-up question texts from the provider output.
    Returns a list of question strings. If no questions found, returns [].
    """
    raw_output = (raw_output or "").strip()
    questions: List[str] = []

    # Preferred: parse JSON and extract all questions
    try:
        data: Any = json.loads(raw_output)
        if isinstance(data, dict):
            data_dict = cast(Dict[str, Any], data)

            # Handle follow_up_questions array
            fus: Any = data_dict.get("follow_up_questions")
            if isinstance(fus, list) and fus:
                fus_list = cast(List[Any], fus)
                for item in fus_list:
                    if isinstance(item, dict):
                        q: Any = item.get("question")
                        if isinstance(q, str) and q.strip():
                            questions.append(q.strip())

            # Handle single question format (fallback)
            if not questions:
                q2: Any = data_dict.get("question")
                if isinstance(q2, str) and q2.strip():
                    questions.append(q2.strip())
    except Exception:
        pass

    # If JSON parsing failed, try heuristic extraction
    if not questions:
        matches = re.findall(r'"question"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw_output)
        for match in matches:
            try:
                questions.append(json.loads('"' + match + '"').strip())
            except Exception:
                questions.append(match.strip())

    return questions


def _extract_all_readable_texts(raw_output: str) -> List[str]:
    """
    Extract all texts that should be assessed for readability:
    - All question texts
    - All option texts (each option treated as separate unit)

    Returns a list of text strings that should be readable.
    """
    raw_output = (raw_output or "").strip()
    texts: List[str] = []

    # Preferred: parse JSON and extract all questions and options
    try:
        data: Any = json.loads(raw_output)
        if isinstance(data, dict):
            data_dict = cast(Dict[str, Any], data)

            # Handle follow_up_questions array
            fus: Any = data_dict.get("follow_up_questions")
            if isinstance(fus, list) and fus:
                fus_list = cast(List[Any], fus)
                for item in fus_list:
                    if isinstance(item, dict):
                        # Add question text
                        q: Any = item.get("question")
                        if isinstance(q, str) and q.strip():
                            texts.append(q.strip())

                        # Add each option as separate text unit
                        opts: Any = item.get("options")
                        if isinstance(opts, list):
                            for opt in opts:
                                if isinstance(opt, str) and opt.strip():
                                    texts.append(opt.strip())

            # Handle single question format (fallback)
            if not texts:
                q2: Any = data_dict.get("question")
                if isinstance(q2, str) and q2.strip():
                    texts.append(q2.strip())

                opts2: Any = data_dict.get("options")
                if isinstance(opts2, list):
                    for opt in opts2:
                        if isinstance(opt, str) and opt.strip():
                            texts.append(opt.strip())
    except Exception:
        pass

    # If JSON parsing failed, fall back to question-only extraction
    if not texts:
        texts = _extract_all_question_texts(raw_output)

    return texts


def _extract_question_text(raw_output: str) -> str:
    """Legacy: Extract the first follow-up question text."""
    qs = _extract_all_question_texts(raw_output)
    return qs[0] if qs else ""


def get_assert(output: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate readability of follow-up questions using Dale-Chall with enhanced legal vocabulary.

    Configuration:
        max_dale_chall_grade (float): Maximum acceptable grade level (default: 9.0)
        Also accepts legacy key: max_fk_grade

    Returns:
        Promptfoo assertion result dict with pass/fail, score (0-1), and reason.
    """
    try:
        if not SCIREADABILITY_AVAILABLE:
            return {
                "pass": True,
                "score": 1,
                "reason": "scireadability/nltk not available in worker environment; skipping readability check",
            }

        # Extract max_grade from various possible config locations
        max_grade = 9.0

        assertion = context.get("assertion", {})
        if isinstance(assertion, dict):
            value = assertion.get("value")
            if isinstance(value, (int, float)):
                max_grade = float(value)

        if max_grade == 9.0:
            config = context.get("config", {})
            if isinstance(config, dict):
                if "max_dale_chall_grade" in config:
                    max_grade = float(config.get("max_dale_chall_grade", 9.0))
                elif "max_fk_grade" in config:  # legacy
                    max_grade = float(config.get("max_fk_grade", 9.0))

        raw_output = output
        try:
            raw_json = (
                context.get("providerResponse", {}).get("metadata", {}).get("raw_json")
            )
            if isinstance(raw_json, str) and raw_json.strip():
                raw_output = raw_json
        except Exception:
            raw_output = output

        # Extract ALL readable texts (questions + options, each as separate units)
        readable_texts = _extract_all_readable_texts(raw_output)

        # Determine if there were any questions at all (optional feature)
        try:
            data: Any = json.loads(raw_output)
            if isinstance(data, dict):
                data_dict = cast(Dict[str, Any], data)
                q: Any = data_dict.get("question")
                has_direct_question = isinstance(q, str) and q.strip()
                fus: Any = data_dict.get("follow_up_questions")
                has_followup_questions = isinstance(fus, list) and len(fus) > 0
                if not (has_direct_question or has_followup_questions):
                    return {
                        "pass": True,
                        "score": 1,
                        "reason": "No follow-up questions generated (questions are optional)",
                    }
        except Exception:
            if not readable_texts or all(
                len(q.split()) < 3 or "label" in q.lower() for q in readable_texts
            ):
                return {
                    "pass": True,
                    "score": 1,
                    "reason": "No follow-up questions generated (questions are optional)",
                }

        if not readable_texts:
            return {
                "pass": True,
                "score": 1,
                "reason": "No follow-up questions generated (questions are optional)",
            }

        # Extract words from user query to add to easy words set
        user_query = ""
        try:
            user_query = context.get("vars", {}).get("problem_description", "")
        except Exception:
            pass

        user_words: Set[str] = set()
        if user_query:
            user_words = set(_tokenize_words(user_query))

        # Build easy words set with user query words included (stemmed to match DC set)
        easy_words = _build_easy_words_set(additional_words=user_words)

        failed_questions: List[str] = []
        grades_info: List[str] = []

        for i, text in enumerate(readable_texts, 1):
            grade = _calculate_dale_chall_grade(text, easy_words)
            fkgl_grade = _calculate_fkgl_grade(text)

            grades_info.append(f"T{i}: DC={grade:.2f}, FKGL={fkgl_grade:.2f}")

            if grade > max_grade:
                failed_questions.append(f"T{i} (grade {grade:.2f} > {max_grade})")

        if failed_questions:
            return {
                "pass": False,
                "score": 0.5,
                "reason": (
                    f"Readability violations in {len(failed_questions)}/{len(readable_texts)} texts: "
                    f"{'; '.join(failed_questions)}. All grades: {'; '.join(grades_info)}"
                ),
            }

        return {
            "pass": True,
            "score": 1,
            "reason": f"All {len(readable_texts)} texts meet readability criteria (DC ≤ {max_grade}). "
            f"Grades: {'; '.join(grades_info)}",
        }

    except Exception as e:
        return {
            "pass": False,
            "score": 0,
            "reason": f"Error evaluating readability: {str(e)}",
        }


def get_fkgl_assert(output: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Report FKGL as an absolute score for display. Pass if <= max_fkgl_grade (default: 14.0).
    Uses the WORST (highest) grade across all questions.
    """
    try:
        if not SCIREADABILITY_AVAILABLE:
            return {
                "pass": True,
                "score": 0,
                "reason": "scireadability/nltk not available in worker environment; skipping FKGL check",
            }

        max_grade = 14.0
        assertion = context.get("assertion", {})
        if isinstance(assertion, dict):
            value = assertion.get("value")
            if isinstance(value, (int, float)):
                max_grade = float(value)

        config = context.get("config", {})
        if isinstance(config, dict):
            if "max_fkgl_grade" in config:
                max_grade = float(config.get("max_fkgl_grade", max_grade))
            elif "max_fk_grade" in config:  # legacy
                max_grade = float(config.get("max_fk_grade", max_grade))

        raw_output = output
        try:
            raw_json = (
                context.get("providerResponse", {}).get("metadata", {}).get("raw_json")
            )
            if isinstance(raw_json, str) and raw_json.strip():
                raw_output = raw_json
        except Exception:
            raw_output = output

        readable_texts = _extract_all_readable_texts(raw_output)
        if not readable_texts:
            return {
                "pass": True,
                "score": 0,
                "reason": "No follow-up questions generated (questions are optional)",
            }

        grades: List[float] = []
        for text in readable_texts:
            grades.append(_calculate_fkgl_grade(text))

        worst_grade = max(grades) if grades else 0.0
        passed = worst_grade <= max_grade

        return {
            "pass": passed,
            "score": worst_grade,
            "reason": (
                f"Worst FKGL grade: {worst_grade:.2f} (max: {max_grade}) across {len(readable_texts)} texts. "
                f"Individual grades: {', '.join(f'{g:.2f}' for g in grades)}"
            ),
        }
    except Exception as e:
        return {
            "pass": False,
            "score": 0,
            "reason": f"Error evaluating FKGL: {str(e)}",
        }


def main() -> None:
    """CLI entry point for when run directly."""
    output = sys.argv[1]
    context_any: Any = json.loads(sys.argv[2])
    context: Dict[str, Any] = (
        cast(Dict[str, Any], context_any) if isinstance(context_any, dict) else {}
    )

    result = get_assert(output, context)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
