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
