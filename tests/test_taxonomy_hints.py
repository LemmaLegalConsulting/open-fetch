from app.providers.base import clear_all_prompt_caches, load_prompt


def test_hints_omitted_when_commented_out_for_default_taxonomy() -> None:
    """Test that hints are not included when all hints are commented out.

    The taxonomy_hints_default.yaml file currently has all content commented out,
    so the TAXONOMY HINTS block should not appear in the prompt.
    """
    taxonomy = [{"Category": "Real Property", "Subcategory": "Tenant (Residential)"}]
    clear_all_prompt_caches()
    prompt, _ = load_prompt("openai", taxonomy, taxonomy_name="default")
    # Since all hints are commented out, no TAXONOMY HINTS block should be present
    assert "TAXONOMY HINTS" not in prompt
    # But the taxonomy should still be there
    assert "Tenant (Residential)" in prompt


def test_hints_omitted_for_list_taxonomy() -> None:
    taxonomy = [{"Category": "Real Property > Tenant (Residential)"}]
    clear_all_prompt_caches()
    prompt, _ = load_prompt("openai", taxonomy, taxonomy_name="list")
    assert "TAXONOMY HINTS" not in prompt
