from app.data.list_taxonomy import (
    load_list_taxonomy_simple,
    get_list_title_to_code_mapping,
    get_list_code_to_title_mapping,
    get_osb_to_list_mapping,
)


def test_load_list_taxonomy_simple_has_category_key():
    rows = load_list_taxonomy_simple()
    assert isinstance(rows, list)
    assert all(isinstance(r, dict) for r in rows)
    # Each row should have a 'Category' key
    assert all("Category" in r for r in rows)


def test_title_to_code_mapping_and_back():
    mapping = get_list_title_to_code_mapping()
    # mapping keys should be lowercase strings and values non-empty
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())

    code_to_title = get_list_code_to_title_mapping()
    assert all(
        isinstance(k, str) and isinstance(v, str) for k, v in code_to_title.items()
    )


def test_osb_to_list_mapping():
    osb_map = get_osb_to_list_mapping()
    # Should be a dict mapping strings to strings (keys lowercase)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in osb_map.items())
