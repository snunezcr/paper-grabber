from pathlib import Path

import pytest

from paper_grabber.models import normalize_title, split_author_venue
from paper_grabber.parse import dedupe, parse_alert_email, unwrap_scholar_url

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def papers():
    return parse_alert_email((DATA / "quantum-computer-architecture.eml").read_bytes())


def test_finds_every_result(papers):
    # The alert's tracking pixel lists trs=0..9, so ten results is the truth.
    assert len(papers) == 10


def test_title_bold_tags_are_flattened(papers):
    assert papers[0].title == "Quantum computing for computer vision: A comprehensive literature survey"


def test_title_bold_boundary_does_not_invent_a_space(papers):
    # "<b>quantum</b>-dot" must not become "quantum -dot".
    assert "quantum-dot interface" in papers[2].title


def test_title_bold_boundary_keeps_a_real_space(papers):
    # "<b>Quantum Computation </b>with" -- the space lives inside the tag.
    assert papers[4].title.startswith("Universal Quantum Computation with Multi-Mode")


def test_title_keeps_internal_punctuation(papers):
    assert papers[3].title.startswith("Qyn: FPGA-Based Quantum Error Correction")


def test_byline_survives_the_share_table(papers):
    # Entry 0 is the one that retains Scholar's social-share table between the
    # snippet and the next result; its byline must still be found.
    assert papers[0].authors == ["AM Hafiz", "M Hassaballah"]
    assert papers[0].venue == "Image and Vision Computing"
    assert papers[0].year == 2026


def test_byline_absent_venue(papers):
    p = papers[5]
    assert p.authors == ["H Ishida", "A Elsokary", "MJC Henshaw", "S Ji"]
    assert p.venue is None
    assert p.year is None


def test_truncated_author_list_drops_ellipsis(papers):
    assert papers[2].authors == ["E Bargel", "A Medeiros", "IM de Buy Wenniger", "S Wein"]


def test_arxiv_venue_and_year(papers):
    assert papers[1].venue == "arXiv preprint arXiv:2607.13699"
    assert papers[1].year == 2026


def test_urls_are_unwrapped(papers):
    assert papers[0].url == "https://www.sciencedirect.com/science/article/pii/S0262885626002271"
    assert papers[1].url == "https://arxiv.org/pdf/2607.13699"


def test_pdf_badge_detected(papers):
    assert [p.has_pdf_badge for p in papers[:6]] == [False, True, False, False, True, True]


def test_snippet_captured(papers):
    assert "This thesis investigates" in papers[1].snippet


def test_snippets_do_not_leak_across_results(papers):
    # papers[5] has no venue; a naive sibling walk would attach the wrong text.
    assert "position paper" in papers[5].snippet


def test_alert_metadata(papers):
    assert all(p.alert_query == "quantum computer architecture" for p in papers)
    assert all(p.alert_id == "_sFMcxpod9sJ" for p in papers)
    assert all(p.message_id for p in papers)


def test_positions_are_sequential(papers):
    assert [p.position for p in papers] == list(range(10))


def test_dedupe_collapses_case_and_punctuation_variants(papers):
    twin = papers[0]
    # Same words, different case and separators -- Scholar really does vary
    # these between alerts, so they must fold to one key.
    variant = type(twin)(
        title="quantum computing for COMPUTER vision - a comprehensive literature survey!"
    )
    assert normalize_title(variant.title) == normalize_title(twin.title)
    assert len(dedupe([twin, variant])) == 1


def test_dedupe_keeps_genuinely_different_titles(papers):
    assert len(dedupe(papers)) == len(papers)


def test_dedupe_is_first_wins(papers):
    a, b = papers[0], papers[1]
    assert dedupe([a, b, a])[0] is a


@pytest.mark.parametrize(
    "line,expected",
    [
        ("A B\xa0- Journal of Things, 2024", (["A B"], "Journal of Things", 2024)),
        ("A B, C D", (["A B", "C D"], None, None)),
        ("A B\xa0- Some Venue", (["A B"], "Some Venue", None)),
        ("A B\xa0- Proc. of X - Y, 2020", (["A B"], "Proc. of X - Y", 2020)),
    ],
)
def test_split_author_venue(line, expected):
    assert split_author_venue(line) == expected


def test_unwrap_passes_through_plain_urls():
    assert unwrap_scholar_url("https://arxiv.org/abs/1234") == "https://arxiv.org/abs/1234"


# --- second alert: "quantum abstract machine" ---------------------------------


@pytest.fixture(scope="module")
def qam():
    return parse_alert_email((DATA / "quantum-abstract-machine.eml").read_bytes())


def test_qam_result_count(qam):
    # Tracking pixel says trs=0,1.
    assert len(qam) == 2


def test_qam_bylines_without_venue(qam):
    assert qam[0].authors == ["Z Zhu", "S Zhao", "D Tang", "J Guo", "L Shen"]
    assert qam[0].venue is None and qam[0].year is None
    assert qam[1].authors == ["DU Hur"]


def test_qam_percent_encoded_inner_url_is_decoded_exactly_once(qam):
    # Scholar percent-encodes the inner query string (books%3Fhl%3Den%26...).
    # parse_qs decodes it; a second unquote would corrupt literal escapes.
    assert qam[1].url == (
        "https://books.google.com/books?hl=en&lr=lang_en&id=DiHzEQAAQBAJ"
        "&oi=fnd&pg=PR5&dq=quantum+abstract+machine&ots=WFE5AjvVQ6"
        "&sig=qasklPs85w-4r9qIHYU9Mr73dVs"
    )


def test_qam_alert_metadata(qam):
    assert all(p.alert_query == "quantum abstract machine" for p in qam)
    assert all(p.alert_id == "jsaKBQ1wChoJ" for p in qam)


def test_qam_snippet_with_bold_abstract_label(qam):
    # The snippet literally starts "… Abstract: Machine learning has …"
    assert qam[0].snippet.startswith("… Abstract: Machine learning has become")


def test_alert_ids_differ_between_alerts(papers, qam):
    assert papers[0].alert_id != qam[0].alert_id


def test_unwrap_does_not_double_decode():
    wrapped = "https://scholar.google.com/scholar_url?url=https://ex.org/a%2520b&hl=en"
    assert unwrap_scholar_url(wrapped) == "https://ex.org/a%20b"


# --- third alert: same query, later date ---------------------------------------
#
# NOTE: this fixture is ABRIDGED. The real email carried ten results; four are
# kept here (the ones with distinctive shapes) and the tracking pixel was
# adjusted to match. Treat it as a constructed case, not a verbatim capture.


@pytest.fixture(scope="module")
def qca2():
    return parse_alert_email((DATA / "quantum-computer-architecture-2.eml").read_bytes())


def test_qca2_result_count(qca2):
    assert len(qca2) == 4


def test_qca2_accented_venue_survives_charset_decoding(qca2):
    assert qca2[0].venue == "Journal Européen des Systèmes Automatisés"
    assert qca2[0].year == 2026


def test_qca2_missing_snippet_is_none_not_stolen(qca2):
    # This result has a byline but no gse_alrt_sni div at all. It must come
    # back None rather than borrowing the next result's snippet.
    p = qca2[1]
    assert p.title.startswith("Quantum Machine Learning: Bridging")
    assert p.snippet is None
    assert p.authors[0] == "W Zhang"


def test_qca2_missing_snippet_does_not_shift_later_results(qca2):
    # The result after the snippet-less one must keep its own snippet.
    assert "This paper investigates the integration" in qca2[2].snippet


def test_qca2_ampersand_entity_in_title(qca2):
    assert qca2[1].title.endswith("Quantum Computing & Machine Learning")


def test_qca2_leading_quote_entity_is_cleaned(qca2):
    # Scholar emits a stray leading &quot; on this record; clean_title drops it.
    assert qca2[2].title.startswith("Navigating the Quantum Revolution")


def test_qca2_bare_year_byline(qca2):
    # "L Muller - 2026": plain ASCII hyphen, and the venue field is only a
    # year. Getting this wrong names the file "???? - Title.pdf".
    p = qca2[3]
    assert p.authors == ["L Muller"]
    assert p.venue is None
    assert p.year == 2026


def test_qca2_same_alert_id_as_first_fixture(papers, qca2):
    # Same saved alert, different send date -- the id is stable over time.
    assert qca2[0].alert_id == papers[0].alert_id


@pytest.mark.parametrize(
    "line,expected",
    [
        ("L Muller - 2026", (["L Muller"], None, 2026)),
        ("A B\xa0- 2026 7th Intl Conf on Bio\xa0…, 2026", (["A B"], "2026 7th Intl Conf on Bio", 2026)),
        ("A B\xa0- Journal 2026 Edition", (["A B"], "Journal 2026 Edition", None)),
    ],
)
def test_split_author_venue_year_edge_cases(line, expected):
    assert split_author_venue(line) == expected


def test_latex_mangled_title_is_cleaned_end_to_end(papers):
    # 'Schr\" odinger' in the raw email must reach us as 'Schrödinger'.
    assert papers[4].title == (
        "Universal Quantum Computation with Multi-Mode Schrödinger Cat States "
        "Stabilized by Non-Local Dissipation Engineering"
    )
