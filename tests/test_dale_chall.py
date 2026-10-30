import pytest
from promptfoo.assertions.followup_textstat_grade import (
    _build_easy_words_set,
    _calculate_dale_chall_grade,
    _tokenize_words,
    _split_sentences,
    _stem_words,
    _get_stemmer,
)


class TestDaleChallList:
    """Test the Dale-Chall easy words list and readability calculations."""

    def test_easy_words_set_includes_base_dale_chall(self):
        """Test that the easy words set includes the base Dale-Chall words."""
        easy_words = _build_easy_words_set()

        # Test some known Dale-Chall easy words (should be stemmed)
        stemmer = _get_stemmer()
        known_easy_words = [
            "the",
            "and",
            "is",
            "it",
            "in",
            "said",
            "for",
            "on",
            "but",
            "with",
        ]

        for word in known_easy_words:
            stemmed = stemmer.stem(word)
            assert (
                stemmed in easy_words
            ), f"Expected '{word}' (stemmed: '{stemmed}') to be in easy words"

    def test_easy_words_set_includes_legal_terms(self):
        """Test that legal terms from legal_easy_words.txt are included."""
        easy_words = _build_easy_words_set()

        stemmer = _get_stemmer()
        legal_terms = [
            "attorney",
            "court",
            "divorce",
            "custody",
            "alimony",
            "abuse",
            "arrest",
        ]

        for term in legal_terms:
            stemmed = stemmer.stem(term)
            assert (
                stemmed in easy_words
            ), f"Expected legal term '{term}' (stemmed: '{stemmed}') to be in easy words"

    def test_additional_words_are_included(self):
        """Test that additional words passed to _build_easy_words_set are included."""
        base_words = _build_easy_words_set()
        additional_words = {"testword", "anotherword"}
        extended_words = _build_easy_words_set(additional_words)

        stemmer = _get_stemmer()
        for word in additional_words:
            stemmed = stemmer.stem(word)
            assert (
                stemmed in extended_words
            ), f"Additional word '{word}' (stemmed: '{stemmed}') should be included"
            # Ensure base words are still there
            assert len(extended_words) > len(
                base_words
            ), "Extended set should be larger than base set"

    def test_stemming_consistency(self):
        """Test that stemming is applied consistently."""
        stemmer = _get_stemmer()

        test_cases = [
            ("running", "run"),
            ("attorneys", "attorney"),
            ("courts", "court"),
            ("divorces", "divorc"),
            ("custodies", "custodi"),
        ]

        for original, expected_stem in test_cases:
            actual_stem = stemmer.stem(original)
            assert (
                actual_stem == expected_stem
            ), f"Stemming '{original}' should give '{expected_stem}', got '{actual_stem}'"

    def test_tokenization(self):
        """Test word tokenization."""
        text = "This is a test sentence with some punctuation!"
        words = _tokenize_words(text)

        expected = ["this", "is", "test", "sentence", "with", "some", "punctuation"]
        assert (
            words == expected
        ), f"Tokenization failed: expected {expected}, got {words}"

        # Test filtering of short words
        text_with_short = "This is a test with s and a"
        words_filtered = _tokenize_words(text_with_short)
        assert "s" not in words_filtered, "Single letter words should be filtered out"
        assert "a" not in words_filtered, "Single letter words should be filtered out"

    def test_sentence_splitting(self):
        """Test sentence splitting."""
        text = "This is sentence one. This is sentence two? This is sentence three!"
        sentences = _split_sentences(text)

        expected = [
            "This is sentence one",
            "This is sentence two",
            "This is sentence three",
        ]
        # Strip whitespace from actual results for comparison (regex may leave leading spaces)
        actual_stripped = [s.strip() for s in sentences]
        assert (
            actual_stripped == expected
        ), f"Sentence splitting failed: expected {expected}, got {actual_stripped}"

    def test_dale_chall_calculation_simple_text(self):
        """Test Dale-Chall calculation with simple text."""
        # Very simple text with mostly easy words
        simple_text = "The cat sat on the mat."
        grade = _calculate_dale_chall_grade(simple_text)

        # Should be a low grade (easy to read)
        assert grade >= 0, "Grade should be non-negative"
        assert grade < 5, f"Simple text should have low grade, got {grade}"

    def test_dale_chall_calculation_complex_text(self):
        """Test Dale-Chall calculation with complex text."""
        # Complex text with many hard words
        complex_text = "The plaintiff alleges that the defendant's negligence proximately caused substantial damages."
        grade = _calculate_dale_chall_grade(complex_text)

        # Should be a higher grade (harder to read)
        assert grade > 5, f"Complex legal text should have higher grade, got {grade}"

    def test_dale_chall_with_legal_terms(self):
        """Test that legal terms are treated as easy words."""
        # Text with legal terms that should be easy
        legal_text = "The attorney filed a motion in court for child custody."
        grade_legal = _calculate_dale_chall_grade(legal_text)

        # Text with similar complexity but non-legal terms
        non_legal_text = (
            "The lawyer submitted a request in tribunal for juvenile guardianship."
        )
        grade_non_legal = _calculate_dale_chall_grade(non_legal_text)

        # Legal text should have lower grade due to legal terms being in easy words list
        assert (
            grade_legal <= grade_non_legal
        ), f"Legal text grade ({grade_legal}) should be <= non-legal ({grade_non_legal})"

    def test_dale_chall_empty_text(self):
        """Test Dale-Chall calculation with empty text."""
        grade = _calculate_dale_chall_grade("")
        assert grade == 0.0, "Empty text should return grade 0.0"

        grade_whitespace = _calculate_dale_chall_grade("   \n\t   ")
        assert grade_whitespace == 0.0, "Whitespace-only text should return grade 0.0"

    def test_dale_chall_clamping(self):
        """Test that grades are clamped to reasonable range."""
        # Create text that would theoretically give very high grade
        # (This is hard to construct precisely, but we can test the clamping logic exists)
        grade = _calculate_dale_chall_grade("This is some text to test.")

        assert grade <= 16.0, f"Grade should be clamped to max 16.0, got {grade}"
        assert grade >= 0.0, f"Grade should be clamped to min 0.0, got {grade}"

    def test_stem_words_function(self):
        """Test the _stem_words utility function."""
        words = {"running", "jumped", "attorneys", "courts"}
        stemmed = _stem_words(words)

        stemmer = _get_stemmer()
        expected_stems = {stemmer.stem(w) for w in words}

        assert (
            stemmed == expected_stems
        ), f"Stemming failed: expected {expected_stems}, got {stemmed}"
