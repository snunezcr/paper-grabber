"""Shared test helpers.

Lives in conftest so both test_enrich and test_cache can use it without the
tests directory needing to be an importable package.
"""

import httpx

from paper_grabber.enrich import OpenAlexClient


def make_client(handler, **kw):
    """An OpenAlexClient whose transport is a callable, not a socket."""
    transport = httpx.MockTransport(handler)
    return OpenAlexClient(client=httpx.Client(transport=transport), **kw)


def works_response(*results):
    """A handler returning a fixed /works result list."""

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
    """A trimmed copy of a real OpenAlex work record."""
    return {
        "id": "https://openalex.org/W1",
        "display_name": title,
        "publication_year": year,
        "doi": doi,
        "open_access": {
            "is_oa": is_oa,
            "oa_status": "green" if is_oa else "closed",
            "oa_url": None,
        },
        "best_oa_location": (
            {"pdf_url": pdf_url, "landing_page_url": "https://ex.org/a"} if pdf_url else None
        ),
        "locations": [{"pdf_url": pdf_url}] if pdf_url else [],
        "abstract_inverted_index": abstract,
        "cited_by_count": 3,
        "authorships": [{"author": {"display_name": "A Author"}}],
    }
