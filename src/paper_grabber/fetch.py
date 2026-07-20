"""Download open-access PDFs.

The hard part is not the HTTP; it is deciding whether what came back is
actually a PDF. Publishers routinely answer a PDF request with ``200 OK``,
``Content-Type: application/pdf``, and an HTML interstitial -- a cookie wall, a
CAPTCHA, a "choose your institution" page. Trusting the headers means silently
filing a 4KB login page as a paper, so every response is validated by its
leading bytes instead.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterator

import httpx

# Some publishers 403 anything that looks automated. This is a real browser
# string: we are fetching one openly-licensed document at human pace, not
# crawling, but a default python-httpx agent gets blocked on sight.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# A PDF must start with %PDF-, but some servers prepend stray whitespace or a
# BOM, so the marker is searched for within a short prefix.
_PDF_MAGIC = b"%PDF-"
_MAGIC_WINDOW = 1024

_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body")

# The largest paper seen in practice is ~30 MB; 100 MB covers theses and
# image-heavy PDFs with room to spare. The cap exists so a misdirected download
# cannot exhaust memory -- the whole body is buffered, and briefly doubled when
# assembled -- which matters on a 1 GB box. Override with PG_MAX_PDF_MB for a
# larger machine.
def _default_max_bytes() -> int:
    raw = os.environ.get("PG_MAX_PDF_MB", "100")
    try:
        mb = int(raw)
    except ValueError:
        mb = 100
    return max(1, mb) * 1024 * 1024


DEFAULT_MAX_BYTES = _default_max_bytes()

# Enough of the body to identify the content without buffering a whole file.
_SNIFF_BYTES = 4096


@dataclass
class FetchResult:
    """Outcome of one download attempt."""

    url: str
    ok: bool = False
    content: bytes | None = None
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    size: int = 0
    reason: str | None = None

    @property
    def is_html(self) -> bool:
        return self.reason == "server returned HTML, not a PDF"


def looks_like_pdf(head: bytes) -> bool:
    """True when a response prefix really is a PDF."""
    return _PDF_MAGIC in head[:_MAGIC_WINDOW]


def looks_like_html(head: bytes) -> bool:
    lowered = head[:_MAGIC_WINDOW].lower()
    return any(marker in lowered for marker in _HTML_MARKERS)


def _describe_body(head: bytes) -> str:
    """Explain, for the log, what arrived instead of a PDF."""
    if looks_like_html(head):
        return "server returned HTML, not a PDF"
    if not head:
        return "empty response"
    return "response was not a PDF"


def download_pdf(
    url: str,
    *,
    client: httpx.Client,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchResult:
    """Fetch one URL and return it only if it is genuinely a PDF.

    Streams the body so an oversized or wrong-typed response is abandoned
    after a few kilobytes rather than downloaded in full.
    """
    try:
        with client.stream("GET", url) as resp:
            result = FetchResult(
                url=url,
                status=resp.status_code,
                final_url=str(resp.url),
                content_type=resp.headers.get("content-type"),
            )

            if resp.status_code != 200:
                result.reason = f"HTTP {resp.status_code}"
                return result

            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                result.reason = f"too large: {declared} bytes"
                return result

            chunks: list[bytes] = []
            total = 0
            head = b""
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if len(head) < _SNIFF_BYTES:
                    head = b"".join(chunks)[:_SNIFF_BYTES]
                    # Bail out early on an interstitial rather than paying for
                    # the whole body.
                    if len(head) >= len(_PDF_MAGIC) and not looks_like_pdf(head):
                        if looks_like_html(head):
                            result.reason = _describe_body(head)
                            return result
                if total > max_bytes:
                    result.reason = f"exceeded {max_bytes} bytes"
                    return result

            body = b"".join(chunks)
            result.size = len(body)

            if not looks_like_pdf(body):
                result.reason = _describe_body(body)
                return result

            result.ok = True
            result.content = body
            return result

    except httpx.HTTPError as exc:
        return FetchResult(url=url, reason=f"{type(exc).__name__}: {exc}")


def download_first_available(
    urls: Iterator[str] | list[str],
    *,
    client: httpx.Client,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchResult:
    """Try each candidate in order, returning the first genuine PDF.

    Publishers block, mirrors rot, and repositories move files, so a work with
    several known locations is far more likely to be retrievable than any one
    URL suggests. On total failure the *last* attempt's result is returned, so
    the reason reflects a real response rather than a synthetic error.
    """
    urls = list(urls)
    if not urls:
        return FetchResult(url="", reason="no PDF candidates")

    last: FetchResult | None = None
    for url in urls:
        last = download_pdf(url, client=client, max_bytes=max_bytes)
        if last.ok:
            return last
    assert last is not None
    return last


def make_client(*, timeout: float = 60.0) -> httpx.Client:
    """An httpx client configured the way publishers expect."""
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            # Some servers content-negotiate away from PDF without this.
            "Accept": "application/pdf,*/*",
        },
    )
