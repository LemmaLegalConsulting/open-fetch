from app.providers.spot import SpotProvider


def test_spot_mapping_loads():
    sp = SpotProvider()
    mapping = sp.taxonomy_mapping
    assert isinstance(mapping, dict)
    # If mapping file is present, expect at least one mapping; otherwise, mapping may be empty
    # But keys (if present) should be strings
    if mapping:
        assert all(isinstance(k, str) for k in mapping.keys())
        assert all(
            isinstance(v.get("category"), (str, type(None))) for v in mapping.values()
        )
