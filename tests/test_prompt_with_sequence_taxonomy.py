from app.providers.base import load_prompt, _compute_taxonomy_hash


def test_load_prompt_with_sequence_taxonomy():
    # Build a small taxonomy as list of dicts
    taxonomy = [
        {"Category": "Real Property", "Subcategory": "Tenant (Residential)"},
        {"Category": "Family Law", "Subcategory": "Divorce"},
    ]

    final_prompt, template = load_prompt("default", taxonomy)
    assert "{{taxonomy}}" not in final_prompt
    # Expect the taxonomy to be present in the prompt
    assert (
        "Real Property > Tenant (Residential)" in final_prompt
        or "Family Law > Divorce" in final_prompt
    )

    # Hash should be stable and change when taxonomy content changes
    h1 = _compute_taxonomy_hash(taxonomy)
    taxonomy.append({"Category": "New Cat", "Subcategory": "Sub"})
    h2 = _compute_taxonomy_hash(taxonomy)
    assert h1 != h2
