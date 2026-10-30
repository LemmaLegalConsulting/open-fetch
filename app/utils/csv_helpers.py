"""Small CSV helpers to replace pandas where we only need simple read + normalize.

Functions:
- read_csv_as_list_of_dicts(path): reads a CSV and returns a list of Ordered dicts (csv.DictReader preserves header order);
  strips whitespace and replaces empty strings with None.
- dedupe_and_clean_rows(rows): removes duplicate rows and rows that are all None.

This keeps behavior conservative compared to pandas for existing use cases (no type inference, simple string-based cleaning).
"""

from __future__ import annotations

import csv
from typing import List, Dict, Iterable, Any


def read_csv_as_list_of_dicts(path: str) -> List[Dict[str, Any]]:
    """Read a CSV into list of dicts (header order preserved).

    - Strips surrounding whitespace from each cell.
    - Converts empty strings to None.
    """
    rows: List[Dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {}
            for k, v in row.items():
                if v is None:
                    cleaned[k] = None
                else:
                    v2 = v.strip()
                    cleaned[k] = v2 if v2 != "" else None
            rows.append(cleaned)
    return rows


def dedupe_and_clean_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicate rows and rows where every value is None.

    Deduplication preserves first-seen order.
    """
    seen = set()
    result: List[Dict[str, Any]] = []
    for row in rows:
        values_tuple = tuple((k, row.get(k)) for k in row.keys())
        # skip rows that are all None
        if all(v is None for _, v in values_tuple):
            continue
        # skip rows where the first column is missing/None (treat as incomplete)
        first_value = next(iter(row.values()), None)
        if first_value is None:
            continue
        if values_tuple in seen:
            continue
        seen.add(values_tuple)
        result.append(row)
    return result
