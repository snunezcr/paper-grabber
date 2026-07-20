"""Abstract backfill tests. All HTTP mocked."""

import httpx
import pytest

from paper_grabber.abstracts import (
    MIN_ABSTRACT_CHARS,
    ArxivAbstracts,
    PageMetaAbstracts,
    SemanticScholarAbstracts,
    arxiv_id_of,
    clean_text,
    looks_like_an_abstract,
)

LONG = "Quantum error correction is essential for fault tolerance. " * 6


# --- arXiv ids ----------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://arxiv.org/pdf/2607.13699", "2607.13699"),
    ("https://arxiv.org/abs/2607.12249", "2607.12249"),
    ("https://arxiv.org/pdf/2607.13699v2", "2607.13699"),
    ("https://arxiv.org/abs/cond-mat/0703470", "cond-mat/0703470"),
    ("https://dl.acm.org/doi/10.1145/1", None),
    (None, None),
])
def test_arxiv_id_extraction(url, expected):
    assert arxiv_id_of(url) == expected


# --- quality gate -------------------------------------------------------------


def test_short_text_is_not_an_abstract():
    # A title or a teaser is worse than the snippet it would replace.
    assert looks_like_an_abstract("Quantum computing is hard.") is False


def test_long_text_is_an_abstract():
    assert looks_like_an_abstract(LONG) is True


@pytest.mark.parametrize("junk", [
    "JavaScript is disabled in your browser. " + "x" * 300,
    "Please enable cookies to continue. " + "y" * 300,
    "Sign in to access this article. " + "z" * 300,
    "Just a moment while we verify your browser. " + "w" * 300,
])
def test_boilerplate_is_rejected(junk):
    assert looks_like_an_abstract(junk) is False


def test_none_is_not_an_abstract():
    assert looks_like_an_abstract(None) is False


def test_clean_text_strips_markup_and_entities():
    assert clean_text("<p>a &amp; b</p>") == "a & b"
    assert clean_text("  spread\n  over lines ") == "spread over lines"
    assert clean_text("") is None


# --- arXiv --------------------------------------------------------------------


def arxiv_feed(*entries):
    body = "".join(
        f"<entry><id>http://arxiv.org/abs/{i}</id><summary>{s}</summary></entry>"
        for i, s in entries
    )
    return f"<feed>{body}</feed>"


def arxiv_client(handler):
    return ArxivAbstracts(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_arxiv_returns_abstracts_by_id():
    def handler(request):
        return httpx.Response(200, text=arxiv_feed(("2607.11490v1", LONG)))

    got = arxiv_client(handler).fetch(["2607.11490"])
    assert got["2607.11490"].source == "arXiv"
    assert "fault tolerance" in got["2607.11490"].text


def test_arxiv_version_suffix_is_stripped():
    # The feed answers 2607.11490v1 for a query of 2607.11490.
    def handler(request):
        return httpx.Response(200, text=arxiv_feed(("2607.11490v3", LONG)))

    assert "2607.11490" in arxiv_client(handler).fetch(["2607.11490"])


def test_arxiv_batches_ids_into_one_request():
    calls = []

    def handler(request):
        calls.append(request.url.params.get("id_list"))
        return httpx.Response(200, text=arxiv_feed(("1", LONG), ("2", LONG)))

    arxiv_client(handler).fetch(["1", "2"])
    assert len(calls) == 1
    assert calls[0] == "1,2"


def test_arxiv_ignores_a_short_summary():
    def handler(request):
        return httpx.Response(200, text=arxiv_feed(("1", "too short")))

    assert arxiv_client(handler).fetch(["1"]) == {}


def test_arxiv_survives_an_http_error():
    def handler(request):
        return httpx.Response(503)

    assert arxiv_client(handler).fetch(["1"]) == {}


def test_arxiv_survives_a_network_error():
    def handler(request):
        raise httpx.ConnectError("down")

    assert arxiv_client(handler).fetch(["1"]) == {}


# --- Semantic Scholar ---------------------------------------------------------


def test_semantic_scholar_is_unusable_without_a_key(monkeypatch):
    # The anonymous pool answers 429 in practice, so a keyless request is a
    # wasted round trip.
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    assert SemanticScholarAbstracts().usable is False


def test_semantic_scholar_returns_an_abstract_with_a_key():
    def handler(request):
        assert request.headers.get("x-api-key") == "k"
        return httpx.Response(200, json={"abstract": LONG})

    s = SemanticScholarAbstracts(
        api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert s.fetch_one("10.1/x").source == "Semantic Scholar"


def test_semantic_scholar_handles_a_miss():
    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    s = SemanticScholarAbstracts(
        api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert s.fetch_one("10.1/x") is None


# --- publisher metadata -------------------------------------------------------


def meta_client(handler):
    return PageMetaAbstracts(client=httpx.Client(transport=httpx.MockTransport(handler)))


def page(**metas):
    tags = "".join(
        f'<meta name="{k}" content="{v}">' for k, v in metas.items()
    )
    return f"<html><head>{tags}</head><body></body></html>"


def test_citation_abstract_is_preferred():
    def handler(request):
        return httpx.Response(200, text=page(**{
            "description": "Site blurb. " + "b" * 300,
            "citation_abstract": LONG,
        }))

    assert "fault tolerance" in meta_client(handler).fetch_one("https://x.example/a").text


def test_falls_back_to_og_description():
    def handler(request):
        return httpx.Response(200, text=page(**{"og:description": LONG}))

    assert meta_client(handler).fetch_one("https://x.example/a").source == "og:description"


def test_content_before_name_is_also_read():
    def handler(request):
        return httpx.Response(
            200, text=f'<meta content="{LONG}" name="citation_abstract">'
        )

    assert meta_client(handler).fetch_one("https://x.example/a") is not None


def test_blocked_page_yields_nothing():
    # ResearchGate answers 403 to anything automated.
    def handler(request):
        return httpx.Response(403)

    assert meta_client(handler).fetch_one("https://researchgate.net/x") is None


def test_page_without_useful_metadata_yields_nothing():
    def handler(request):
        return httpx.Response(200, text=page(**{"description": "Short blurb."}))

    assert meta_client(handler).fetch_one("https://x.example/a") is None
