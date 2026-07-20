"""Destination suggestion tests, built around the failures found on real data."""

import pytest

from paper_grabber.suggest import build_idf, suggest_folder, tokens

FOLDERS = [
    {"id": "ION", "name": "Ion trap hardware"},
    {"id": "SPIN", "name": "Spin qubit platforms"},
    {"id": "QEC", "name": "Quantum error correction and mitigation"},
    {"id": "NET", "name": "Quantum networks"},
    {"id": "DATA", "name": "Quantum data"},
    {"id": "SWE", "name": "Quantum software engineering"},
]


def suggest(title, abstract=None, venue=None, folders=FOLDERS, **kw):
    return suggest_folder(title=title, abstract=abstract, venue=venue,
                          folders=folders, **kw)


# --- tokenising ---------------------------------------------------------------


def test_domain_words_are_ignored():
    # True of nearly every paper here, so they distinguish nothing.
    assert "quantum" not in tokens("Quantum Computing Architecture")
    assert "computing" not in tokens("Quantum Computing Architecture")


def test_plurals_are_normalised():
    assert tokens("qubits platforms") == tokens("qubit platform")


def test_accents_are_folded():
    assert "schrodinger" in tokens("Schrödinger cat states")


# --- the single-word folder trap ----------------------------------------------


def test_a_single_common_word_is_not_a_match():
    # "Quantum networks" reduces to {network}. A paper about neural networks
    # matched it perfectly before, sending chemistry papers to it.
    assert suggest("Physical Constraint Frameworks in Chemistry",
                   abstract="We use neural networks to model constraints.") is None


def test_a_single_word_folder_needs_corroboration():
    assert suggest("Next-Generation Data Science", abstract="Big data pipelines.") is None


def test_two_matched_words_do_suggest():
    s = suggest("Improving Dynamical Decoupling for Trapped-Ion QCCD Architectures")
    assert s is not None and s.folder_id == "ION"


# --- the abstract must not decide --------------------------------------------


def test_abstract_alone_cannot_carry_a_suggestion():
    # A long abstract mentions everything; scoring against it saturated every
    # folder and sent a silicon-spin paper to error correction.
    assert suggest(
        "Optimal operating temperature for silicon devices",
        abstract="We discuss error correction, networks, ion traps and software.",
    ) is None


def test_title_match_wins_over_abstract_noise():
    s = suggest("Spin qubit platforms in silicon",
                abstract="Also mentions ion trap hardware and networks.")
    assert s is not None and s.folder_id == "SPIN"


# --- prefix matching ----------------------------------------------------------


def test_prefix_matching_handles_word_forms():
    s = suggest("Trapped ions in scalable hardware")
    assert s is not None and s.folder_id == "ION"


def test_short_tokens_do_not_prefix_match():
    # "ion" must not match "ionic" or "iontophoresis".
    assert suggest("Ionic conduction in solid electrolytes") is None


# --- refusing to guess --------------------------------------------------------


def test_no_folders_means_no_suggestion():
    assert suggest("Anything at all", folders=[]) is None


def test_empty_title_yields_nothing():
    assert suggest("", abstract="Ion trap hardware everywhere") is None


def test_a_tie_is_refused():
    # Two folders matching equally means the signal does not distinguish them.
    folders = [{"id": "A", "name": "Ion trap hardware"},
               {"id": "B", "name": "Ion trap hardware"}]
    assert suggest("Ion trap hardware for qubits", folders=folders) is None


def test_unrelated_paper_gets_nothing():
    assert suggest("Schizoanalysis: Politics and Subjectivity") is None


# --- history ------------------------------------------------------------------


def test_history_can_carry_a_folder_its_name_does_not_describe():
    # "Spin qubit platforms" never says silicon or donor.
    history = {"SPIN": ["Silicon donor qubits at millikelvin temperatures"]}
    s = suggest("Silicon donor devices for scalable computing", history=history)
    assert s is not None and s.folder_id == "SPIN"


def test_history_also_needs_two_matches():
    history = {"SPIN": ["Something about silicon"]}
    assert suggest("Silicon wafers", history=history) is None


# --- idf ----------------------------------------------------------------------


def test_idf_ranks_rare_words_above_common_ones():
    corpus = ["machine learning " * 5, "machine learning models",
              "photonic interference"]
    idf = build_idf(corpus)
    assert idf["photonic"] > idf["machine"]


def test_scores_are_reported_for_display():
    s = suggest("Trapped-Ion hardware for quantum registers")
    assert s is not None
    assert 0.0 < s.score <= 1.0
    assert s.reason
