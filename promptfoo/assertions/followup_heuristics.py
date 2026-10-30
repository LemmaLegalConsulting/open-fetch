# assertions/followup_heuristics.py
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union

# -------------------------
# Helpers: parse output JSON
# -------------------------


def _safe_json_loads(s: str) -> Optional[dict]:
    try:
        return json.loads(s)
    except Exception:
        return None


def _is_display_only(context: Dict[str, Any]) -> bool:
    try:
        return bool(
            (context or {})
            .get("providerResponse", {})
            .get("metadata", {})
            .get("display_only")
        )
    except Exception:
        return False


def _raw_output(output: str, context: Dict[str, Any]) -> str:
    try:
        raw_json = (
            (context or {})
            .get("providerResponse", {})
            .get("metadata", {})
            .get("raw_json")
        )
        if isinstance(raw_json, str) and raw_json.strip():
            return raw_json
    except Exception:
        pass
    return output


def _extract_all_followups(output: str) -> Tuple[List[Tuple[str, List[str]]], str]:
    """
    Returns (list of (question, options) tuples, reason_if_missing)
    Supports either:
      { follow_up_questions: [{ question, options, format }] }
    or
      { question, options, format }
    """
    if not isinstance(output, str):
        return [], "Output is not a string"
    data = _safe_json_loads(output)
    if not isinstance(data, dict):
        return [], "Output is not valid JSON object"

    questions = []

    # Handle follow_up_questions array
    if isinstance(data.get("follow_up_questions"), list):
        for q_obj in data["follow_up_questions"]:
            if isinstance(q_obj, dict):
                question = q_obj.get("question")
                if isinstance(question, str) and question.strip():
                    opts = q_obj.get("options") or []
                    if not isinstance(opts, list):
                        opts = []
                    opts = [str(o).strip() for o in opts if str(o).strip()]
                    questions.append((question.strip(), opts))

    # Handle single question format (fallback)
    if not questions:
        q_obj = data
        if isinstance(q_obj, dict):
            question = q_obj.get("question") or data.get("question")
            if isinstance(question, str) and question.strip():
                opts = q_obj.get("options") or data.get("options") or []
                if not isinstance(opts, list):
                    opts = []
                opts = [str(o).strip() for o in opts if str(o).strip()]
                questions.append((question.strip(), opts))

    if not questions:
        return [], "No follow-up questions generated"

    return questions, ""


def _words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9']+", text)


def _sentences(text: str) -> List[str]:
    # Lightweight sentence splitter
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def _sentence_length_stats(q: str) -> Dict[str, float]:
    sents = _sentences(q)
    if not sents:
        # fallback: treat as one sentence
        sents = [q]
    lengths = [len(_words(s)) for s in sents]
    avg_len = sum(lengths) / max(1, len(lengths))
    return {
        "avg_sentence_words": float(avg_len),
        "max_sentence_words": float(max(lengths) if lengths else 0.0),
        "num_sentences": float(len(sents)),
    }


# -------------------------
# Promptfoo assertion functions
# -------------------------


def sentence_length_ok(output: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Complements FKGL by catching long, clause-heavy questions.
    Checks ALL follow-up questions - ALL must meet the criteria.
    Defaults:
      - avg sentence length <= 28 words
      - max sentence length <= 40 words
    """
    if _is_display_only(context):
        return {"pass": True, "score": 1.0, "reason": "SKIP: display-only provider"}

    raw = _raw_output(output, context)
    questions, err = _extract_all_followups(raw)
    if not questions:
        return {"pass": True, "score": 1.0, "reason": f"SKIP: {err}"}

    cfg = (context or {}).get("config") or {}
    max_avg = float(cfg.get("max_avg_sentence_words", 28.0))
    max_max = float(cfg.get("max_max_sentence_words", 40.0))

    failed_questions = []
    for i, (q, _) in enumerate(questions, 1):
        stats = _sentence_length_stats(q)
        avg_ok = stats["avg_sentence_words"] <= max_avg
        max_ok = stats["max_sentence_words"] <= max_max
        if not (avg_ok and max_ok):
            failed_questions.append(
                f"Q{i}: avg={stats['avg_sentence_words']:.1f}, max={stats['max_sentence_words']:.0f}"
            )

    if failed_questions:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"Sentence length violations in {len(failed_questions)}/{len(questions)} questions: {'; '.join(failed_questions)} (max avg: {max_avg}, max max: {max_max})",
        }

    return {
        "pass": True,
        "score": 1.0,
        "reason": f"All {len(questions)} questions meet sentence length criteria (avg ≤ {max_avg}, max ≤ {max_max})",
    }
