"""Fallback chain tests. All HTTP mocked."""

import httpx
import pytest

from paper_grabber.chain import EnrichmentChain, build_chain
from paper_grabber.enrich import Enrichment, RateLimited
from paper_grabber.models import AlertPaper
from paper_grabber.providers import (
    CrossrefClient,
    UnpaywallClient,
    clean_jats_abstract,
)


class FakeSource:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def enrich(self, paper):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or Enrichment()

    def close(self):
        pass


class FakeUnpaywall:
    def __init__(self, urls=None):
        self.urls = urls or []
        self.asked = []

    def pdf_locations(self, doi):
        self.asked.append(doi)
        return list(self.urls)

    def close(self):
        pass


PAPER = AlertPaper(title="Quantum Error Correction", year=2026)


# --- ordering -----------------------------------------------------------------


def test_openalex_wins_when_it_matches():
    oa = FakeSource(Enrichment(matched=True, doi="10.1/oa"))
    cr = FakeSource(Enrichment(matched=True, doi="10.1/cr"))
    result = EnrichmentChain(openalex=oa, crossref=cr).enrich(PAPER)
    assert result.doi == "10.1/oa"
    assert cr.calls == 0            # the free source is not called needlessly


def test_crossref_is_used_when_openalex_finds_nothing():
    oa = FakeSource(Enrichment(matched=False, note="no OpenAlex candidates"))
    cr = FakeSource(Enrichment(matched=True, doi="10.1/cr"))
    result = EnrichmentChain(openalex=oa, crossref=cr).enrich(PAPER)
    assert result.doi == "10.1/cr"


def test_rate_limit_falls_through_to_crossref():
    # The whole point: a spent budget must not stop the run.
    oa = FakeSource(error=RateLimited("Insufficient budget."))
    cr = FakeSource(Enrichment(matched=True, doi="10.1/cr"))
    result = EnrichmentChain(openalex=oa, crossref=cr).enrich(PAPER)
    assert result.matched
    assert result.doi == "10.1/cr"


def test_openalex_is_not_retried_once_exhausted():
    # Every later call would fail identically; asking again wastes a request.
    oa = FakeSource(error=RateLimited("Insufficient budget."))
    cr = FakeSource(Enrichment(matched=True, doi="10.1/cr"))
    chain = EnrichmentChain(openalex=oa, crossref=cr)
    for _ in range(5):
        chain.enrich(PAPER)
    assert oa.calls == 1
    assert chain.openalex_exhausted is True


def test_chain_works_with_no_openalex_at_all():
    cr = FakeSource(Enrichment(matched=True, doi="10.1/cr"))
    assert EnrichmentChain(crossref=cr).enrich(PAPER).doi == "10.1/cr"


def test_both_failing_returns_an_unmatched_result():
    oa = FakeSource(Enrichment(matched=False, note="nothing"))
    cr = FakeSource(Enrichment(matched=False, note="nothing either"))
    assert EnrichmentChain(openalex=oa, crossref=cr).enrich(PAPER).matched is False


# --- open access locations ----------------------------------------------------


def test_unpaywall_fills_missing_pdf_locations():
    oa = FakeSource(Enrichment(matched=True, doi="10.1/x", pdf_candidates=[]))
    up = FakeUnpaywall(["https://repo.example/a.pdf"])
    result = EnrichmentChain(openalex=oa, unpaywall=up).enrich(PAPER)
    assert result.pdf_candidates == ["https://repo.example/a.pdf"]
    assert up.asked == ["10.1/x"]


def test_unpaywall_is_skipped_when_locations_already_known():
    oa = FakeSource(Enrichment(matched=True, doi="10.1/x",
                               pdf_candidates=["https://a.example/a.pdf"]))
    up = FakeUnpaywall(["https://repo.example/b.pdf"])
    EnrichmentChain(openalex=oa, unpaywall=up).enrich(PAPER)
    assert up.asked == []


def test_unpaywall_is_not_asked_without_a_doi():
    oa = FakeSource(Enrichment(matched=False))
    up = FakeUnpaywall(["https://repo.example/a.pdf"])
    EnrichmentChain(openalex=oa, unpaywall=up).enrich(PAPER)
    assert up.asked == []


def test_scholar_link_is_always_a_last_resort():
    paper = AlertPaper(title="A paper", year=2026, url="https://arxiv.org/pdf/2607.1")
    oa = FakeSource(Enrichment(matched=False))
    result = EnrichmentChain(openalex=oa).enrich(paper)
    assert result.pdf_candidates == ["https://arxiv.org/pdf/2607.1"]


def test_scholar_link_is_appended_after_the_authoritative_ones():
    paper = AlertPaper(title="A paper", year=2026, url="https://arxiv.org/pdf/2607.1")
    oa = FakeSource(Enrichment(matched=True, doi="10.1/x",
                               pdf_candidates=["https://repo.example/a.pdf"]))
    result = EnrichmentChain(openalex=oa).enrich(paper)
    assert result.pdf_candidates[0] == "https://repo.example/a.pdf"
    assert result.pdf_candidates[-1] == "https://arxiv.org/pdf/2607.1"


# --- Crossref -----------------------------------------------------------------


def crossref_client(handler, **kw):
    return CrossrefClient(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def items_response(*items):
    def handler(request):
        return httpx.Response(200, json={"message": {"items": list(items)}})

    return handler


def cr_item(title="Quantum Error Correction", year=2026, doi="10.1/abc",
            abstract=None, authors=None):
    return {
        "DOI": doi,
        "title": [title],
        "issued": {"date-parts": [[year]]},
        "abstract": abstract,
        "author": authors or [{"given": "A", "family": "Author"}],
        "container-title": ["A Journal"],
    }


def test_crossref_returns_doi_and_authors():
    c = crossref_client(items_response(cr_item()))
    e = c.enrich(PAPER)
    assert e.matched and e.doi == "10.1/abc"
    assert e.authors == ["A Author"]
    assert e.note == "via Crossref"


def test_crossref_wrong_paper_is_refused():
    # Crossref's bibliographic search is fuzzy and will happily return a
    # different paper on a related topic.
    c = crossref_client(items_response(cr_item(title="Quantum Nonlocality and Bell Tests")))
    assert c.enrich(PAPER).matched is False


def test_crossref_year_conflict_is_refused():
    c = crossref_client(items_response(cr_item(year=2015)))
    e = c.enrich(PAPER)
    assert e.matched is False
    assert "year conflicts" in e.note


def test_crossref_no_results():
    assert crossref_client(items_response()).enrich(PAPER).note == "no Crossref candidates"


def test_crossref_http_failure_is_reported():
    def handler(request):
        return httpx.Response(500)

    assert "failed" in crossref_client(handler).enrich(PAPER).note


def test_crossref_missing_year_does_not_block():
    item = cr_item()
    item["issued"] = {}
    assert crossref_client(items_response(item)).enrich(PAPER).matched


@pytest.mark.parametrize("raw,expected", [
    ("<jats:p>Hello world</jats:p>", "Hello world"),
    ("<jats:title>Abstract</jats:title><jats:p>Body text</jats:p>", "Body text"),
    ("Abstract: The real text", "The real text"),
    ("<p>a &amp; b</p>", "a & b"),
    ("<p>spread   over\n  lines</p>", "spread over lines"),
    (None, None),
    ("<p></p>", None),
])
def test_jats_abstract_cleaning(raw, expected):
    assert clean_jats_abstract(raw) == expected


# --- Unpaywall ----------------------------------------------------------------


def unpaywall_client(handler):
    return UnpaywallClient(
        email="me@example.com", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_unpaywall_returns_pdf_urls():
    def handler(request):
        return httpx.Response(200, json={
            "is_oa": True,
            "best_oa_location": {"url_for_pdf": "https://a.example/a.pdf"},
            "oa_locations": [{"url_for_pdf": "https://b.example/b.pdf"}],
        })

    assert unpaywall_client(handler).pdf_locations("10.1/x") == [
        "https://a.example/a.pdf", "https://b.example/b.pdf"]


def test_unpaywall_ignores_landing_page_only_records():
    # is_oa true with no url_for_pdf is common; fetching the landing page
    # would file HTML as though it were the paper.
    def handler(request):
        return httpx.Response(200, json={
            "is_oa": True, "best_oa_location": {"url_for_pdf": None},
            "oa_locations": [{"url_for_pdf": None}]})

    assert unpaywall_client(handler).pdf_locations("10.1/x") == []


def test_unpaywall_survives_a_non_json_body():
    # Unknown DOIs can come back as HTML.
    def handler(request):
        return httpx.Response(200, content=b"<html>not found</html>")

    assert unpaywall_client(handler).pdf_locations("10.1/x") == []


def test_unpaywall_survives_a_404():
    assert unpaywall_client(lambda r: httpx.Response(404)).pdf_locations("10.1/x") == []


def test_unpaywall_survives_a_network_error():
    def handler(request):
        raise httpx.ConnectError("down")

    assert unpaywall_client(handler).pdf_locations("10.1/x") == []


def test_unpaywall_needs_no_request_without_a_doi():
    called = []

    def handler(request):
        called.append(1)
        return httpx.Response(200, json={})

    assert unpaywall_client(handler).pdf_locations("") == []
    assert called == []


def test_unpaywall_requires_an_email():
    with pytest.raises(ValueError):
        UnpaywallClient(email="")


# --- assembly -----------------------------------------------------------------


def test_build_chain_skips_unpaywall_without_an_email():
    chain = build_chain(mailto=None)
    assert chain.unpaywall is None
    assert chain.crossref is not None
    chain.close()


def test_build_chain_can_omit_openalex():
    chain = build_chain(mailto="me@example.com", use_openalex=False)
    assert chain.openalex is None
    assert chain.crossref is not None and chain.unpaywall is not None
    chain.close()
