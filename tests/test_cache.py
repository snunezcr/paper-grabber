import time

import httpx
import pytest

from paper_grabber.cache import HIT_TTL_SECONDS, MISS_TTL_SECONDS, LookupCache
from paper_grabber.enrich import OpenAlexClient, RateLimited
from paper_grabber.models import AlertPaper
from conftest import make_client, work, works_response


@pytest.fixture
def cache(tmp_path):
    with LookupCache(tmp_path / "cache.db") as c:
        yield c


def test_roundtrip(cache):
    cache.put("A Title", {"doi": "10.1/x"}, matched=True)
    assert cache.get("A Title") == {"doi": "10.1/x"}


def test_miss_returns_none(cache):
    assert cache.get("Never seen") is None


def test_key_is_normalized_like_the_deduper(cache):
    # "Title: Sub" and "title  sub!" are the same paper.
    cache.put("Quantum: Computing", {"doi": "10.1/x"}, matched=True)
    assert cache.get("quantum  computing!") == {"doi": "10.1/x"}


def test_hit_survives_a_long_time(cache, monkeypatch):
    cache.put("A", {"v": 1}, matched=True)
    # Capture the real clock before patching, or the lambda calls itself.
    soon = time.time() + HIT_TTL_SECONDS - 60
    monkeypatch.setattr(time, "time", lambda: soon)
    assert cache.get("A") is not None


def test_stale_hit_is_ignored(cache, monkeypatch):
    cache.put("A", {"v": 1}, matched=True)
    later = time.time() + HIT_TTL_SECONDS + 60
    monkeypatch.setattr(time, "time", lambda: later)
    assert cache.get("A") is None


def test_negative_result_expires_sooner_than_a_hit(cache, monkeypatch):
    # A paper missing today may be indexed next week.
    cache.put("A", {"matched": False}, matched=False)
    later = time.time() + MISS_TTL_SECONDS + 60
    monkeypatch.setattr(time, "time", lambda: later)
    assert cache.get("A") is None


def test_negative_result_is_reused_within_its_window(cache, monkeypatch):
    cache.put("A", {"matched": False}, matched=False)
    soon = time.time() + 3600
    monkeypatch.setattr(time, "time", lambda: soon)
    assert cache.get("A") is not None


def test_put_overwrites(cache):
    cache.put("A", {"v": 1}, matched=True)
    cache.put("A", {"v": 2}, matched=True)
    assert cache.get("A") == {"v": 2}
    assert len(cache) == 1


def test_cache_persists_across_instances(tmp_path):
    path = tmp_path / "c.db"
    with LookupCache(path) as c:
        c.put("A", {"v": 1}, matched=True)
    with LookupCache(path) as c2:
        assert c2.get("A") == {"v": 1}


# --- integration with the client ---------------------------------------------


def test_second_lookup_costs_no_request(cache):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"results": [work(title="Quantum Computing")]})

    p = AlertPaper(title="Quantum Computing", year=2026)
    c = make_client(handler, cache=cache)
    first = c.enrich(p)
    second = c.enrich(p)
    assert len(calls) == 1  # the budget is spent exactly once
    assert first.doi == second.doi
    assert second.matched


def test_negative_results_are_cached_too(cache):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"results": []})

    p = AlertPaper(title="Not indexed anywhere", year=2026)
    c = make_client(handler, cache=cache)
    c.enrich(p)
    c.enrich(p)
    assert len(calls) == 1


def test_cached_enrichment_keeps_pdf_candidates(cache):
    p = AlertPaper(title="Quantum Computing", year=2026)
    c = make_client(works_response(work(title="Quantum Computing")), cache=cache)
    c.enrich(p)
    again = c.enrich(p)
    assert again.pdf_candidates == ["https://ex.org/a.pdf"]
    assert again.pdf_url == "https://ex.org/a.pdf"


# --- rate limiting ------------------------------------------------------------


def test_rate_limit_raises_rather_than_returning():
    # Every later call would fail identically; the run must stop, not churn.
    body = {
        "error": "Rate limit exceeded",
        "message": "Insufficient budget.",
        "retryAfter": 571,
    }

    def handler(request):
        return httpx.Response(429, json=body)

    c = make_client(handler)
    with pytest.raises(RateLimited) as exc:
        c.enrich(AlertPaper(title="Anything", year=2026))
    assert exc.value.retry_after == 571
    assert "Insufficient budget" in str(exc.value)


def test_rate_limit_without_a_body_still_raises():
    def handler(request):
        return httpx.Response(429, content=b"nope")

    with pytest.raises(RateLimited):
        make_client(handler).enrich(AlertPaper(title="Anything", year=2026))


def test_other_http_errors_do_not_raise():
    # A 500 on one paper should not abort the whole run.
    def handler(request):
        return httpx.Response(500)

    e = make_client(handler).enrich(AlertPaper(title="Anything", year=2026))
    assert not e.matched
    assert "HTTP 500" in e.note


def test_rate_limited_lookup_is_not_cached(cache):
    def handler(request):
        return httpx.Response(429, json={"message": "Insufficient budget."})

    c = make_client(handler, cache=cache)
    with pytest.raises(RateLimited):
        c.enrich(AlertPaper(title="Anything", year=2026))
    # Nothing was learned, so nothing should be remembered.
    assert len(cache) == 0
