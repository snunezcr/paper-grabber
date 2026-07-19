import pytest

from paper_grabber.clean import clean_title, strip_stray_quotes, unmangle_latex


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The real case, from the "quantum computer architecture" alert:
        # Scholar drops a space where the brace group was.
        (r'Schr\" odinger', "Schrödinger"),
        (r'Schr\"{o}dinger', "Schrödinger"),
        (r'Schr\"odinger', "Schrödinger"),
        (r"R\'enyi", "Rényi"),
        (r"Erd\H{o}s", "Erdős"),
        (r"Fran\c{c}ois", "François"),
        (r"\v{S}tefan", "Štefan"),
        (r"\r{A}ngstr\"om", "Ångström"),
        (r"Poincar\'e", "Poincaré"),
        (r"G\"odel", "Gödel"),
    ],
)
def test_latex_accents_resolve(raw, expected):
    assert unmangle_latex(raw) == expected


def test_accented_output_is_precomposed():
    # NFC matters: a decomposed "o" + combining diaeresis looks identical but
    # compares unequal against OpenAlex's precomposed titles.
    import unicodedata

    out = unmangle_latex(r'Schr\"{o}dinger')
    assert out == unicodedata.normalize("NFC", out)
    assert len(out) == len("Schrödinger")


def test_unknown_latex_command_is_left_alone():
    # Better an untouched oddity than a corrupted title.
    assert unmangle_latex(r"\textbf{quantum}") == r"\textbfquantum"


def test_bibtex_case_braces_are_dropped():
    assert unmangle_latex("Protein {DNA} binding") == "Protein DNA binding"


def test_strip_unbalanced_leading_quote():
    # The real case, from the third alert.
    assert strip_stray_quotes('" Navigating the Quantum Revolution') == (
        "Navigating the Quantum Revolution"
    )


def test_balanced_quotes_are_preserved():
    t = 'A study of "quantum supremacy" claims'
    assert strip_stray_quotes(t) == t


def test_strip_unbalanced_trailing_quote():
    assert strip_stray_quotes('Quantum supremacy"') == "Quantum supremacy"


def test_clean_title_collapses_whitespace():
    assert clean_title("Quantum   computing\n  for   vision") == "Quantum computing for vision"


def test_clean_title_leaves_spurious_period_space_alone():
    # ".NET 9" was mangled to ". NET 9" upstream, but no rule separates that
    # from a legitimate sentence break, so it must survive untouched.
    t = "An End-to-End ML-KEM Implementation in. NET 9 and Angular"
    assert clean_title(t) == t


def test_clean_title_is_idempotent():
    once = clean_title(r'" Schr\" odinger and Erd\H{o}s')
    assert clean_title(once) == once


def test_clean_title_leaves_ordinary_titles_untouched():
    t = "Qyn: FPGA-Based Quantum Error Correction with Integrated Quantum Machine Learning"
    assert clean_title(t) == t
