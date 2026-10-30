import os
from app.utils.csv_helpers import read_csv_as_list_of_dicts, dedupe_and_clean_rows


def test_read_csv_as_list_of_dicts(tmp_path):
    p = tmp_path / "sample.csv"
    p.write_text("col1,col2\nA,1\nB,\n ,3\nA,1\n")

    rows = read_csv_as_list_of_dicts(str(p))
    # Expect 4 rows read, with cleaned values (empty -> None; whitespace stripped)
    assert len(rows) == 4
    assert rows[0]["col1"] == "A"
    assert rows[1]["col2"] is None
    assert rows[2]["col1"] is None

    cleaned = dedupe_and_clean_rows(rows)
    # dedupe should remove the duplicate A,1 and drop the all-None row
    assert len(cleaned) == 2
    assert {r["col1"] for r in cleaned} == {"A", "B"}
