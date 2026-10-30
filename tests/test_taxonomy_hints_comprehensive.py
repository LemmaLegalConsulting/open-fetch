"""
Unit tests for taxonomy hints loading and building.

Tests cover:
- Loading hints with everything commented out
- Loading hints with general_hint active
- Loading hints with key-specific hints
- Non-default taxonomies
- Missing/non-existent taxonomy files
- Edge cases (empty files, malformed YAML, etc.)
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

from app.data.taxonomy_hints import (
    load_hints_for_taxonomy,
    build_taxonomy_hints_block,
    _normalize_label,
)


class TestNormalizeLabel:
    """Test label normalization."""

    def test_simple_label(self):
        assert _normalize_label("Simple Label") == "simple label"

    def test_label_with_extra_whitespace(self):
        assert _normalize_label("  Spaced   Label  ") == "spaced label"

    def test_label_with_special_chars(self):
        assert _normalize_label("Label > SubLabel") == "label > sublabel"


class TestLoadHintsForTaxonomy:
    """Test loading hints from YAML files."""

    def test_load_hints_all_commented_out(self, monkeypatch):
        """Test loading when all hints are commented out (returns empty)."""
        # Use the actual default taxonomy file which is now all commented out
        hints = load_hints_for_taxonomy("default")
        assert isinstance(hints, dict)
        assert len(hints) == 0  # Should be empty since everything is commented out

    def test_load_hints_missing_taxonomy(self):
        """Test loading a non-existent taxonomy."""
        hints = load_hints_for_taxonomy("non_existent_taxonomy")
        assert hints == {}

    def test_load_hints_none_taxonomy_name(self):
        """Test loading with None taxonomy name."""
        hints = load_hints_for_taxonomy(None)
        assert hints == {}

    def test_load_hints_empty_string_taxonomy_name(self):
        """Test loading with empty string taxonomy name."""
        hints = load_hints_for_taxonomy("")
        assert hints == {}

    def test_load_hints_with_general_hint(self, tmp_path):
        """Test loading when general_hint is active."""
        # Create a temp YAML file with general_hint
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""general_hint: |
  This is a general hint for all categories.
  It provides context about the taxonomy.
""")

        # Patch the config to use our temp file
        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_general": str(hints_file)}
        ):
            hints = load_hints_for_taxonomy("test_general")
            assert "__general__" in hints
            assert "This is a general hint" in hints["__general__"]

    def test_load_hints_with_key_specific_hints(self, tmp_path):
        """Test loading when key-specific hints are active."""
        # Create a temp YAML file with hints section
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""hints:
  "Real Property > Tenant": "Hint for tenants"
  "Real Property > Landlord": "Hint for landlords"
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_specific": str(hints_file)}
        ):
            hints = load_hints_for_taxonomy("test_specific")
            assert "real property > tenant" in hints
            assert hints["real property > tenant"] == "Hint for tenants"
            assert "real property > landlord" in hints
            assert hints["real property > landlord"] == "Hint for landlords"

    def test_load_hints_with_both_general_and_specific(self, tmp_path):
        """Test loading when both general_hint and hints are present."""
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""general_hint: |
  General hint applies to all.
hints:
  "Category A": "Specific hint for A"
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_both": str(hints_file)}
        ):
            hints = load_hints_for_taxonomy("test_both")
            # general_hint takes precedence
            assert "__general__" in hints
            assert "General hint applies" in hints["__general__"]
            # But specific hints should also be loaded
            assert "category a" in hints

    def test_load_hints_malformed_yaml(self, tmp_path):
        """Test loading malformed YAML file."""
        hints_file = tmp_path / "bad_hints.yaml"
        hints_file.write_text("invalid: yaml: content: [")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_malformed": str(hints_file)}
        ):
            # Should return empty dict and log warning
            hints = load_hints_for_taxonomy("test_malformed")
            assert hints == {}

    def test_load_hints_with_none_values(self, tmp_path):
        """Test loading with None values in hints."""
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""hints:
  "Category A": "Valid hint"
  "Category B": null
  null: "Null key"
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_nones": str(hints_file)}
        ):
            hints = load_hints_for_taxonomy("test_nones")
            # Only valid entries should be included
            assert "category a" in hints
            assert "category b" not in hints  # null value skipped
            assert len(hints) == 1


class TestBuildTaxonomyHintsBlock:
    """Test building hints blocks for prompts."""

    def test_build_hints_empty_hints(self):
        """Test building when no hints are available."""
        block = build_taxonomy_hints_block("default", ["Category A", "Category B"])
        assert block == ""

    def test_build_hints_with_general_hint(self, tmp_path):
        """Test building block with general_hint."""
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""general_hint: |
  Remember the context of each category.
  Always consider who is asking.
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING",
            {"test_build_general": str(hints_file)},
        ):
            block = build_taxonomy_hints_block("test_build_general", ["Category A"])
            assert "HINTS:" in block
            assert "Remember the context" in block
            assert "Always consider who is asking" in block

    def test_build_hints_with_specific_hints(self, tmp_path):
        """Test building block with key-specific hints."""
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""hints:
  "Real Property > Tenant": "Tenant perspective hint"
  "Real Property > Landlord": "Landlord perspective hint"
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING",
            {"test_build_specific": str(hints_file)},
        ):
            block = build_taxonomy_hints_block(
                "test_build_specific",
                ["Real Property > Tenant", "Real Property > Landlord"],
            )
            assert "TAXONOMY HINTS" in block
            assert "Real Property > Tenant: Tenant perspective hint" in block
            assert "Real Property > Landlord: Landlord perspective hint" in block

    def test_build_hints_empty_labels(self):
        """Test building with empty labels list."""
        block = build_taxonomy_hints_block("default", [])
        assert block == ""

    def test_build_hints_duplicate_labels(self, tmp_path):
        """Test building with duplicate labels (should only appear once)."""
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""hints:
  "Category A": "Hint for A"
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_dup": str(hints_file)}
        ):
            block = build_taxonomy_hints_block(
                "test_dup", ["Category A", "Category A", "Category A"]
            )
            # Should only include Category A once
            assert block.count("Category A") == 1

    def test_build_hints_no_matching_hints(self, tmp_path):
        """Test building when labels don't match any hints."""
        hints_file = tmp_path / "test_hints.yaml"
        hints_file.write_text("""hints:
  "Category A": "Hint for A"
  "Category B": "Hint for B"
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_no_match": str(hints_file)}
        ):
            # Request hints for categories that don't have any
            block = build_taxonomy_hints_block(
                "test_no_match", ["Category C", "Category D"]
            )
            assert block == ""  # No matching hints


class TestIntegration:
    """Integration tests for hints in the classification workflow."""

    def test_default_taxonomy_with_commented_out_hints(self):
        """Test that default taxonomy works even with all hints commented out."""
        hints = load_hints_for_taxonomy("default")
        assert hints == {}

        block = build_taxonomy_hints_block(
            "default",
            [
                "Real Property > Tenant (Residential)",
                "Labor & Employment > General - Employee",
            ],
        )
        assert block == ""  # No hints means empty block

    def test_list_taxonomy_no_hints_defined(self):
        """Test that list taxonomy (no hints mapping) works gracefully."""
        hints = load_hints_for_taxonomy("list")
        assert hints == {}

        block = build_taxonomy_hints_block("list", ["Some Category"])
        assert block == ""

    def test_hints_can_be_enabled_later(self, tmp_path):
        """Test that hints file can be uncommented and activated without code changes."""
        # Start with fully commented file
        hints_file = tmp_path / "hints_initially_disabled.yaml"
        hints_file.write_text("""# All commented out initially
# general_hint: |
#   Hint for everything
# hints:
#   "Category A": "Specific hint"
""")

        with patch(
            "app.data.taxonomy_hints.HINTS_MAPPING", {"test_enable": str(hints_file)}
        ):
            # Initially no hints
            hints = load_hints_for_taxonomy("test_enable")
            assert hints == {}

            # Now uncomment the file
            hints_file.write_text("""general_hint: |
  Hint for everything
hints:
  "Category A": "Specific hint"
""")

            # Should now have hints without any code changes
            hints = load_hints_for_taxonomy("test_enable")
            assert "__general__" in hints
            assert "Hint for everything" in hints["__general__"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
