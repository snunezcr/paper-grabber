"""Enrichment tests.

All HTTP is mocked: the suite must not depend on OpenAlex being reachable, and
matching decisions are exactly the thing worth pinning down deterministically.
The fixtures below are trimmed copies of real OpenAlex responses.
"""

import httpx
import pytest

from paper_grabber.enrich import (
    DEFAULT_MATCH_THRESHOLD,
    Enrichment,
    OpenAlexClient,
    reconstruct_abstract,
    sanitize_search_value,
    title_similarity,
)
from paper_grabber.models import AlertPaper


def make_client(handler, **kw):
    """An OpenAlexClient whose transport is a callable, not a socket."""
    transport = httpx.MockTransport(handler)
    return OpenAlexClient(client=httpx.Client(transport=transport), **kw)


def works_response(*results):
    def handler(request):
        return httpx.Response(200, json={"results": list(results)})

    return handler


def work(
    *,
    title="A Title",
    year=2026,
    doi="https://doi.org/10.1/abc",
    is_oa=True,
    pdf_url="https://ex.org/a.pdf",
    abstract=None,
):
    return {
        "id": "https://openalex.org/W1",
        "display_name": title,
        "publication_year": year,
        "doi": doi,
        "open_access": {"is_oa": is_oa, "oa_status": "green" if is_oa else "closed", "oa_url": None},
        "best_oa_location": {"pdf_url": pdf_url, "landing_page_url": "https://ex.org/a"} if pdf_url else None,
        "abstract_inverted_index": abstract,
        "cited_by_count": 3,
        "authorships": [{"author": {"display_name": "A Author"}}],
    }


# --- abstract reconstruction --------------------------------------------------


def test_reconstruct_abstract_orders_by_position():
    inverted = {"Quantum": [0], "computing": [1], "is": [2], "hard": [3]}
    assert reconstruct_abstract(inverted) == "Quantum computing is hard"


def test_reconstruct_abstract_handles_repeated_words():
    inverted = {"the": [0, 2], "quantum": [1], "computer": [3]}
    assert reconstruct_abstract(inverted) == "the quantum the computer"


def test_reconstruct_abstract_empty_index_is_none():
    # Elsevier and others suppress abstracts: the key exists but is empty.
    # That is a legitimate "no abstract", not a failure.
    assert reconstruct_abstract({}) is None
    assert reconstruct_abstract(None) is None


# --- title similarity ---------------------------------------------------------


def test_identical_titles_score_one():
    assert title_similarity("Quantum Computing", "quantum computing!") == 1.0


def test_similarity_is_symmetric():
    a, b = "Quantum Machine Learning", "Quantum Machine Learning for Vision"
    assert title_similarity(a, b) == title_similarity(b, a)


def test_superset_title_is_penalised():
    # The real regression: an overlap coefficient scored this pair 1.0 and
    # attached a different paper's DOI. Dice must drop it below threshold.
    scholar = "Quantum Machine Learning: Bridging Quantum Computing & Machine Learning"
    other = "Quantum Machine Learning: Bridging Quantum Computing and Artificial Intelligence"
    assert title_similarity(scholar, other) < DEFAULT_MATCH_THRESHOLD


def test_unrelated_titles_score_low():
    assert title_similarity("Quantum error correction", "Schizoanalysis and subjectivity") < 0.2


# --- search value sanitizing --------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Commas are OpenAlex's AND separator; unsanitized they cause HTTP 400.
        ("Security: Challenges, Solutions, and Applications",
         "Security: Challenges Solutions and Applications"),
        ("A|B", "A B"),
        ("no punctuation here", "no punctuation here"),
    ],
)
def test_sanitize_search_value(raw, expected):
    assert sanitize_search_value(raw) == expected


def test_comma_titles_reach_the_api_sanitized():
    seen = {}

    def handler(request):
        seen["filter"] = request.url.params.get("filter")
        return httpx.Response(200, json={"results": []})

    c = make_client(handler)
    c.search_by_title("Challenges, Solutions, and More")
    assert "," not in seen["filter"]


# --- matching -----------------------------------------------------------------


def test_good_match_populates_fields():
    p = AlertPaper(title="Quantum Error Correction on FPGAs", year=2026)
    c = make_client(works_response(work(title="Quantum Error Correction on FPGAs")))
    e = c.enrich(p)
    assert e.matched
    assert e.doi == "10.1/abc"  # bare DOI, not the https:// form
    assert e.pdf_url == "https://ex.org/a.pdf"
    assert e.authors == ["A Author"]


def test_weak_match_is_refused():
    p = AlertPaper(title="Quantum Error Correction on FPGAs", year=2026)
    c = make_client(works_response(work(title="A completely different paper about birds")))
    e = c.enrich(p)
    assert not e.matched
    assert e.doi is None
    assert "scored" in e.note


def test_year_conflict_is_refused():
    # Same title, wrong year: almost always a different paper.
    p = AlertPaper(title="Quantum Machine Learning", year=2026)
    c = make_client(works_response(work(title="Quantum Machine Learning", year=2019)))
    e = c.enrich(p)
    assert not e.matched
    assert "year conflicts" in e.note


def test_year_conflict_skips_to_the_next_candidate():
    # The impostor sorts first; the right paper is one below it and must win.
    p = AlertPaper(title="Quantum Machine Learning", year=2026)
    c = make_client(
        works_response(
            work(title="Quantum Machine Learning", year=2019, doi="https://doi.org/10.1/wrong"),
            work(title="Quantum Machine Learning", year=2026, doi="https://doi.org/10.1/right"),
        )
    )
    e = c.enrich(p)
    assert e.matched
    assert e.doi == "10.1/right"


def test_one_year_drift_is_allowed():
    # Preprint in one year, published the next -- still the same paper.
    p = AlertPaper(title="Quantum Machine Learning", year=2026)
    c = make_client(works_response(work(title="Quantum Machine Learning", year=2025)))
    assert c.enrich(p).matched


def test_missing_year_does_not_block_a_match():
    p = AlertPaper(title="Quantum Machine Learning", year=None)
    c = make_client(works_response(work(title="Quantum Machine Learning", year=2019)))
    assert c.enrich(p).matched


def test_html_entities_in_openalex_title_are_unescaped():
    # OpenAlex stores some titles with entities still escaped; if that reaches
    # the filename we get "Computing &amp; Learning.pdf".
    p = AlertPaper(title="Bridging Computing & Learning", year=2026)
    c = make_client(works_response(work(title="Bridging Computing &amp; Learning")))
    e = c.enrich(p)
    assert e.title == "Bridging Computing & Learning"


# --- open access resolution ---------------------------------------------------


def test_scholar_pdf_link_rescues_a_closed_work():
    # OpenAlex says closed, but Scholar already pointed at an arXiv PDF.
    p = AlertPaper(
        title="Wireless millikelvin interconnects",
        year=2026,
        url="https://arxiv.org/pdf/2607.13834",
    )
    c = make_client(works_response(work(title="Wireless millikelvin interconnects", is_oa=False, pdf_url=None)))
    e = c.enrich(p)
    assert e.pdf_url == "https://arxiv.org/pdf/2607.13834"
    assert e.is_oa is False  # honest about OpenAlex's verdict
    assert "Scholar link" in e.note


def test_arxiv_abs_url_is_rewritten_to_pdf():
    p = AlertPaper(title="A quantum blockchain", year=2026, url="https://arxiv.org/abs/2607.12249")
    c = make_client(works_response(work(title="A quantum blockchain", is_oa=False, pdf_url=None)))
    assert c.enrich(p).pdf_url == "https://arxiv.org/pdf/2607.12249"


def test_closed_work_without_scholar_pdf_has_no_url():
    p = AlertPaper(title="A closed paper", year=2026, url="https://ieeexplore.ieee.org/abstract/document/1/")
    c = make_client(works_response(work(title="A closed paper", is_oa=False, pdf_url=None)))
    e = c.enrich(p)
    assert e.matched and e.pdf_url is None


# --- failure handling ---------------------------------------------------------


def test_no_candidates_is_reported_not_raised():
    c = make_client(works_response())
    e = c.enrich(AlertPaper(title="Nothing matches this", year=2026))
    assert not e.matched
    assert e.note == "no OpenAlex candidates"


def test_http_error_is_reported_not_raised():
    def handler(request):
        return httpx.Response(500)

    c = make_client(handler)
    e = c.enrich(AlertPaper(title="Anything", year=2026))
    assert not e.matched
    assert "lookup failed" in e.note


def test_timeout_is_reported_not_raised():
    def handler(request):
        raise httpx.ConnectTimeout("too slow")

    c = make_client(handler)
    e = c.enrich(AlertPaper(title="Anything", year=2026))
    assert not e.matched
    assert "lookup failed" in e.note
