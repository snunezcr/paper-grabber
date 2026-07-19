"""Downloader tests. All transport is mocked -- the suite never touches the network."""

import httpx
import pytest

from paper_grabber.fetch import (
    FetchResult,
    download_first_available,
    download_pdf,
    looks_like_html,
    looks_like_pdf,
)

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"x" * 500
HTML = b"<!DOCTYPE html><html><head><title>Sign in</title></head><body>...</body></html>"


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def responder(status=200, body=PDF, headers=None):
    def handler(request):
        return httpx.Response(status, content=body, headers=headers or {})

    return handler


# --- content sniffing ---------------------------------------------------------


def test_pdf_magic_detected():
    assert looks_like_pdf(PDF)


def test_pdf_magic_tolerates_leading_junk():
    # Some servers prepend a BOM or stray whitespace before the marker.
    assert looks_like_pdf(b"\xef\xbb\xbf\n  " + PDF)


def test_html_is_not_a_pdf():
    assert not looks_like_pdf(HTML)
    assert looks_like_html(HTML)


def test_html_detection_is_case_insensitive():
    assert looks_like_html(b"<HTML><BODY>hi</BODY></HTML>")


# --- the case the whole module exists for -------------------------------------


def test_html_interstitial_claiming_to_be_pdf_is_rejected():
    # 200 OK, Content-Type: application/pdf, body is a login wall. Trusting
    # the header would file a sign-in page as if it were the paper.
    c = client_for(responder(body=HTML, headers={"content-type": "application/pdf"}))
    r = download_pdf("https://publisher.example/paper.pdf", client=c)
    assert not r.ok
    assert r.is_html
    assert r.content is None


def test_real_pdf_with_wrong_content_type_is_accepted():
    # The mirror image: servers that label a genuine PDF as octet-stream or
    # text/html. The bytes are what matter.
    c = client_for(responder(body=PDF, headers={"content-type": "text/html"}))
    r = download_pdf("https://repo.example/paper", client=c)
    assert r.ok
    assert r.content == PDF


# --- ordinary outcomes --------------------------------------------------------


def test_successful_download_populates_result():
    c = client_for(responder(headers={"content-type": "application/pdf"}))
    r = download_pdf("https://arxiv.org/pdf/1234", client=c)
    assert r.ok
    assert r.size == len(PDF)
    assert r.status == 200
    assert r.reason is None


def test_http_error_is_reported():
    c = client_for(responder(status=403))
    r = download_pdf("https://researchgate.net/x.pdf", client=c)
    assert not r.ok
    assert r.reason == "HTTP 403"
    assert r.status == 403


def test_404_is_reported():
    c = client_for(responder(status=404))
    assert download_pdf("https://x.example/gone.pdf", client=c).reason == "HTTP 404"


def test_connection_error_is_reported_not_raised():
    def handler(request):
        raise httpx.ConnectError("connection reset by peer")

    r = download_pdf("https://dead.example/x.pdf", client=client_for(handler))
    assert not r.ok
    assert "ConnectError" in r.reason


def test_timeout_is_reported_not_raised():
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    r = download_pdf("https://slow.example/x.pdf", client=client_for(handler))
    assert not r.ok
    assert "ReadTimeout" in r.reason


def test_empty_body_is_rejected():
    c = client_for(responder(body=b""))
    r = download_pdf("https://x.example/empty.pdf", client=c)
    assert not r.ok
    assert r.reason == "empty response"


# --- size limits --------------------------------------------------------------


def test_declared_oversize_is_refused_before_download():
    c = client_for(responder(headers={"content-length": str(10**9)}))
    r = download_pdf("https://x.example/huge.pdf", client=c, max_bytes=1000)
    assert not r.ok
    assert "too large" in r.reason


def test_undeclared_oversize_is_refused_mid_stream():
    # A chunked response carries no content-length, so the declared-size check
    # cannot fire and the running total is the only thing holding the line.
    def handler(request):
        def chunks():
            yield b"%PDF-1.7\n"
            for _ in range(50):
                yield b"y" * 100

        return httpx.Response(200, content=chunks())

    r = download_pdf("https://x.example/huge.pdf", client=client_for(handler), max_bytes=1000)
    assert not r.ok
    assert "exceeded" in r.reason
    assert r.content is None


def test_size_just_under_the_cap_is_accepted():
    c = client_for(responder(body=PDF))
    assert download_pdf("https://x.example/a.pdf", client=c, max_bytes=len(PDF) + 1).ok


# --- candidate fallthrough ----------------------------------------------------


def test_first_candidate_wins_when_it_works():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=PDF)

    r = download_first_available(
        ["https://a.example/1.pdf", "https://b.example/2.pdf"], client=client_for(handler)
    )
    assert r.ok
    assert len(seen) == 1  # the second mirror is never touched


def test_falls_through_to_a_working_mirror():
    def handler(request):
        if "blocked" in str(request.url):
            return httpx.Response(403)
        return httpx.Response(200, content=PDF)

    r = download_first_available(
        ["https://blocked.example/a.pdf", "https://mirror.example/a.pdf"],
        client=client_for(handler),
    )
    assert r.ok
    assert "mirror" in r.final_url


def test_skips_an_html_interstitial_for_a_real_mirror():
    def handler(request):
        if "wall" in str(request.url):
            return httpx.Response(200, content=HTML, headers={"content-type": "application/pdf"})
        return httpx.Response(200, content=PDF)

    r = download_first_available(
        ["https://wall.example/a.pdf", "https://ok.example/a.pdf"], client=client_for(handler)
    )
    assert r.ok
    assert r.content == PDF


def test_all_candidates_failing_reports_the_last_real_reason():
    c = client_for(responder(status=403))
    r = download_first_available(
        ["https://a.example/1.pdf", "https://b.example/2.pdf"], client=c
    )
    assert not r.ok
    assert r.reason == "HTTP 403"


def test_no_candidates_is_reported_cleanly():
    r = download_first_available([], client=client_for(responder()))
    assert not r.ok
    assert r.reason == "no PDF candidates"
