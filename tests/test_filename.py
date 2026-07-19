import pytest

from paper_grabber.filename import (
    deduplicate_filename,
    pdf_filename,
    sanitize_title,
    truncate_title,
)


def test_colon_becomes_spaced_dash():
    # The reported case.
    assert sanitize_title("Test: this is a test") == "Test - this is a test"


def test_colon_rewrite_in_full_filename():
    assert pdf_filename("Test: this is a test", 2026) == "2026 - Test - this is a test.pdf"


def test_colon_without_trailing_space():
    assert sanitize_title("Qyn:FPGA-Based Correction") == "Qyn - FPGA-Based Correction"


def test_colon_with_extra_spaces():
    assert sanitize_title("Qyn  :  FPGA-Based") == "Qyn - FPGA-Based"


def test_multiple_colons_do_not_pile_up_dashes():
    assert sanitize_title("A: B: C") == "A - B - C"


def test_real_title_with_colon():
    assert pdf_filename(
        "Qyn: FPGA-Based Quantum Error Correction with Integrated Quantum Machine Learning",
        2026,
    ) == (
        "2026 - Qyn - FPGA-Based Quantum Error Correction with "
        "Integrated Quantum Machine Learning.pdf"
    )


@pytest.mark.parametrize("ch", ["*", "?", '"', "<", ">", "|"])
def test_illegal_characters_are_removed(ch):
    assert ch not in sanitize_title(f"Quantum{ch}Computing")


def test_slash_becomes_hyphen_not_a_directory():
    # "AI/ML" must not create a folder level.
    assert sanitize_title("A Comparative Analysis of AI/ML") == "A Comparative Analysis of AI-ML"
    assert "/" not in pdf_filename("AI/ML methods", 2026)


def test_backslash_is_handled_too():
    assert sanitize_title(r"C:\path\like") == "C - path-like"


def test_control_characters_are_stripped():
    assert sanitize_title("Quantum\x00Computing\x1f") == "QuantumComputing"


def test_unicode_is_preserved():
    # Accented titles are legal and must survive intact.
    assert pdf_filename("Schrödinger Cat States", 2026) == "2026 - Schrödinger Cat States.pdf"


def test_missing_year_is_visible():
    assert pdf_filename("A Title", None) == "Unknown - A Title.pdf"


def test_leading_and_trailing_dashes_are_trimmed():
    assert sanitize_title(": Leading colon") == "Leading colon"
    assert sanitize_title("Trailing colon:") == "Trailing colon"


def test_trailing_dot_is_removed():
    # Windows silently drops these and Drive gets confused by them.
    assert sanitize_title("A sentence title.") == "A sentence title"


def test_whitespace_is_collapsed():
    assert sanitize_title("Quantum   \n  Computing") == "Quantum Computing"


def test_truncation_does_not_split_a_word():
    long = "Quantum " * 60
    out = truncate_title(long.strip(), limit=50)
    assert len(out) <= 50
    assert not out.endswith("Quantu")


def test_long_title_is_truncated_in_filename():
    name = pdf_filename("Word " * 100, 2026)
    assert len(name) < 220
    assert name.endswith(".pdf")


def test_deduplicate_leaves_free_names_alone():
    assert deduplicate_filename("2026 - A.pdf", set()) == "2026 - A.pdf"


def test_deduplicate_appends_counter():
    existing = {"2026 - A.pdf"}
    assert deduplicate_filename("2026 - A.pdf", existing) == "2026 - A (2).pdf"


def test_deduplicate_skips_taken_counters():
    existing = {"2026 - A.pdf", "2026 - A (2).pdf", "2026 - A (3).pdf"}
    assert deduplicate_filename("2026 - A.pdf", existing) == "2026 - A (4).pdf"


def test_sanitize_is_idempotent():
    once = sanitize_title("Test: this is a test")
    assert sanitize_title(once) == once
