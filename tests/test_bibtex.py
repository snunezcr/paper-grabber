import pytest

from paper_grabber.bibtex import (
    citation_key,
    entry_type,
    escape,
    format_author,
    to_bib_file,
    to_bibtex,
)


def view(**kw):
    base = {"title": "A Paper", "authors": ["A Author"], "year": 2026,
            "venue": None, "doi": None, "pdf_url": None, "source_url": None}
    base.update(kw)
    return base


# --- escaping -----------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("A & B", r"A \& B"),
    ("100%", r"100\%"),
    ("cost $5", r"cost \$5"),
    ("a_b", r"a\_b"),
    ("#hash", r"\#hash"),
    ("{braced}", r"\{braced\}"),
])
def test_special_characters_are_escaped(raw, expected):
    # An unescaped & breaks the whole .bib file it is pasted into.
    assert escape(raw) == expected


def test_ampersand_in_a_real_title_is_escaped():
    entry = to_bibtex(view(title="Quantum Computing & Machine Learning"))
    assert r"\& Machine" in entry
    # No bare ampersand anywhere: one would end the entry early in BibTeX.
    assert "Computing & " not in entry


def test_accents_are_rendered_as_latex_in_fields():
    # UTF-8 compiles under biber but not under classic BibTeX; commands work
    # under both.
    entry = to_bibtex(view(title="Schrödinger Cat States", authors=["Émilie Dupont"]))
    assert r'Schr\"{o}dinger' in entry
    assert r"Dupont, \'{E}milie" in entry
    assert not any(ord(c) > 127 for c in entry)


# --- authors ------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("AM Hafiz", "Hafiz, AM"),
    ("Abdul Mueed Hafiz", "Hafiz, Abdul Mueed"),
    ("Madonna", "Madonna"),
    ("  J   Bergli  ", "Bergli, J"),
])
def test_author_formatting(name, expected):
    assert format_author(name) == expected


def test_authors_are_joined_with_and():
    entry = to_bibtex(view(authors=["A One", "B Two", "C Three"]))
    assert "author = {One, A and Two, B and Three, C}" in entry


def test_no_authors_omits_the_field():
    assert "author" not in to_bibtex(view(authors=[]))


# --- citation keys ------------------------------------------------------------


def test_citation_key_uses_surname_year_and_a_real_word():
    assert citation_key(["AM Hafiz"], 2026, "A Review of the Quantum Machine") == "hafiz2026quantum"


def test_citation_key_skips_generic_words():
    # "review", "of", "the" identify nothing.
    assert citation_key(["X Yang"], 2026, "A Study of Entanglement") == "yang2026entanglement"


def test_citation_key_folds_accents():
    # BibTeX keys must be plain ASCII.
    assert citation_key(["Émilie Dupont"], 2026, "Étude") == "dupont2026etude"


def test_citation_key_survives_missing_everything():
    assert citation_key([], None, "") == "paper"


# --- entry types --------------------------------------------------------------


@pytest.mark.parametrize("venue,expected", [
    ("IEEE Access", "article"),
    ("2026 7th International Conference on Bio", "inproceedings"),
    ("Proceedings of the ACM", "inproceedings"),
    ("Workshop on Quantum Software", "inproceedings"),
    (None, "misc"),
])
def test_entry_type(venue, expected):
    assert entry_type(venue, None) == expected


def test_no_venue_is_misc_not_a_broken_article():
    # @article without a journal is a malformed record.
    assert to_bibtex(view(venue=None)).startswith("@misc{")


def test_conference_uses_booktitle():
    entry = to_bibtex(view(venue="Proceedings of QCE"))
    assert "booktitle = {Proceedings of QCE}" in entry
    assert "journal" not in entry


# --- urls ---------------------------------------------------------------------


def test_doi_resolver_url_is_not_duplicated():
    entry = to_bibtex(view(doi="10.1/x", source_url="https://doi.org/10.1/x"))
    assert "doi = {10.1/x}" in entry
    assert "url" not in entry


def test_pdf_url_is_kept_alongside_a_doi():
    entry = to_bibtex(view(doi="10.1/x", pdf_url="https://arxiv.org/pdf/1"))
    assert "url = {https://arxiv.org/pdf/1}" in entry


# --- whole entries ------------------------------------------------------------


def test_title_is_braced_to_protect_capitalisation():
    assert "title = {{FPGA Control}}" in to_bibtex(view(title="FPGA Control"))


def test_entry_is_syntactically_closed():
    entry = to_bibtex(view())
    assert entry.startswith("@") and entry.rstrip().endswith("}")
    assert entry.count("{") == entry.count("}")


def test_bib_file_disambiguates_duplicate_keys():
    # BibTeX silently keeps only the last of a repeated key.
    v = view(title="Quantum Control", authors=["A Author"], year=2026)
    text = to_bib_file([v, v])
    assert "author2026quantum," in text
    assert "author2026quantuma," in text
    assert "author2026quantumb," not in text   # only two entries


# --- LaTeX rendering ----------------------------------------------------------

from paper_grabber.bibtex import to_latex


@pytest.mark.parametrize("raw,expected", [
    # Accents, rebuilt from the decomposed form.
    ("Schrödinger", r'Schr\"{o}dinger'),
    ("Rényi", r"R\'{e}nyi"),
    ("Erdős", r"Erd\H{o}s"),
    ("François", r"Fran\c{c}ois"),
    ("Müller", r'M\"{u}ller'),
    ("Ů", r"\r{U}"),
    ("ñ", r"\~{n}"),
    ("ê", r"\^{e}"),
])
def test_accented_letters_become_commands(raw, expected):
    assert to_latex(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    # A letter with its own command takes it rather than an accent.
    ("Ångström", r"\AA{}ngstr\"{o}m"),
    ("Ørsted", r"\O{}rsted"),
    ("Straße", r"Stra\ss{}e"),
    ("Łódź", r"\L{}\'{o}d\'{z}"),
])
def test_special_letters_use_their_own_command(raw, expected):
    assert to_latex(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    # U+2010 is the commonest non-ASCII character in a real alert queue.
    ("Quantum‐Safe", "Quantum-Safe"),
    ("Bose–Einstein", "Bose--Einstein"),
    ("temperature—a review", "temperature---a review"),
    ("and so on…", r"and so on\ldots{}"),
    ("“quoted”", "``quoted''"),
    ("it’s", "it's"),
])
def test_unicode_punctuation_becomes_latex(raw, expected):
    assert to_latex(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("α-decay", r"$\alpha$-decay"),
    ("10°", r"10\textdegree{}"),
    ("± 2", r"$\pm$ 2"),
    ("3 × 4", r"3 $\times$ 4"),
])
def test_symbols_become_latex(raw, expected):
    assert to_latex(raw) == expected


def test_bibtex_specials_are_still_escaped_first():
    # Escaping runs before any command is emitted, so a literal backslash in
    # the source cannot be mistaken for one of ours.
    assert to_latex("A & B") == r"A \& B"
    assert to_latex("50%") == r"50\%"
    assert r"\textbackslash{}" in to_latex("a\\b")


def test_ascii_is_untouched():
    plain = "Quantum Error Correction on FPGAs"
    assert to_latex(plain) == plain


def test_unmapped_characters_are_kept_not_dropped():
    # A visible glyph the user can fix beats a silently deleted one.
    assert "中" in to_latex("中 text")


def test_entry_fields_are_rendered(monkeypatch):
    entry = to_bibtex(view(title="Schrödinger Cats", authors=["Émile Müller"],
                           venue="Zeitschrift für Physik"))
    assert r'Schr\"{o}dinger' in entry
    assert r'M\"{u}ller, \'{E}mile' in entry
    assert r'f\"{u}r' in entry
    assert not any(ord(c) > 127 for c in entry)
