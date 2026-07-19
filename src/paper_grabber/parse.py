"""Parse Google Scholar alert emails into structured records.

Scholar's alert markup is undocumented and unversioned, so this module leans on
the few things that have been stable for years -- the ``gse_alrt_title`` and
``gse_alrt_sni`` class names -- and treats everything else (inline styles,
element order, the social-share tables) as incidental.
"""

from __future__ import annotations

import re
from email import message_from_bytes, message_from_string
from email.message import Message
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from .models import AlertPaper, split_author_venue

# Scholar wraps every outbound link; the real target is the `url` query param.
_SCHOLAR_REDIRECT = "scholar.google.com/scholar_url"
# The byline div is identified only by its green colour.
_BYLINE_COLOR = "#006621"
_ALERT_QUERY_RE = re.compile(r"/scholar\?")
_WS_RE = re.compile(r"\s+")


def _text(el: Tag) -> str:
    """Flatten an element's text, collapsing whitespace.

    Deliberately joins with no separator: Scholar puts the trailing space
    *inside* its <b> tags ("<b>Quantum computing </b>for"), so any separator
    would invent a space in "<b>quantum</b>-dot".
    """
    return _WS_RE.sub(" ", el.get_text()).strip()


class ParseError(Exception):
    """The message did not look like a Scholar alert."""


def unwrap_scholar_url(href: str) -> str:
    """Return the publisher URL behind a Scholar redirect.

    Non-redirect links pass through untouched, so this is safe to call on any
    href.
    """
    if _SCHOLAR_REDIRECT not in href:
        return href
    params = parse_qs(urlparse(href).query)
    target = params.get("url", [None])[0]
    # No second unquote: parse_qs has already percent-decoded, and decoding
    # twice turns a literal "%2520" into a raw space and breaks the fetch.
    return target or href


def _html_part(msg: Message) -> str:
    """Extract the text/html body, decoding transfer-encoding and charset."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    raise ParseError("no text/html part found")


def _byline_and_snippet(anchor: Tag) -> tuple[str | None, str | None]:
    """Find the byline and snippet belonging to a title anchor.

    Searches forward from the title rather than assuming sibling adjacency,
    but stops at the next title so a result with a missing byline cannot
    steal the following result's.
    """
    byline = snippet = None

    # Walk forward from the anchor, not its <h3>: find_all_next() on the
    # heading would start with the heading's own children and immediately
    # rediscover this very title.
    for el in anchor.find_all_next():
        if not isinstance(el, Tag):
            continue
        # Reached the next result -- stop rather than borrow its fields.
        if el.name == "a" and "gse_alrt_title" in (el.get("class") or []):
            break
        classes = el.get("class") or []
        if snippet is None and "gse_alrt_sni" in classes:
            snippet = _text(el)
        elif byline is None and el.name == "div" and _BYLINE_COLOR in (el.get("style") or ""):
            byline = _text(el)
        if byline and snippet:
            break

    return byline, snippet


def _alert_metadata(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Pull the alert's query string and stable id from the footer."""
    query = alert_id = None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if query is None and _ALERT_QUERY_RE.search(href):
            params = parse_qs(urlparse(href).query)
            if "q" in params:
                query = params["q"][0]
        if alert_id is None and "alert_id=" in href:
            params = parse_qs(urlparse(href).query)
            if "alert_id" in params:
                alert_id = params["alert_id"][0]
        if query and alert_id:
            break

    return query, alert_id


def parse_alert_html(
    html: str,
    *,
    message_id: str | None = None,
    subject: str | None = None,
) -> list[AlertPaper]:
    """Parse the HTML body of one Scholar alert email."""
    soup = BeautifulSoup(html, "lxml")
    query, alert_id = _alert_metadata(soup)

    # Subject is "<query> - new results"; use it if the footer link is absent.
    if query is None and subject:
        query = re.sub(r"\s*-\s*new results\s*$", "", subject).strip() or None

    papers: list[AlertPaper] = []
    for pos, anchor in enumerate(soup.select("a.gse_alrt_title")):
        title = _text(anchor)
        if not title:
            continue

        href = anchor.get("href", "")
        byline, snippet = _byline_and_snippet(anchor)
        authors, venue, year = split_author_venue(byline) if byline else ([], None, None)

        heading = anchor.find_parent("h3")
        badge = bool(heading and "[PDF]" in heading.get_text())

        papers.append(
            AlertPaper(
                title=title,
                authors=authors,
                venue=venue,
                year=year,
                url=unwrap_scholar_url(href) if href else None,
                snippet=snippet,
                has_pdf_badge=badge,
                alert_query=query,
                alert_id=alert_id,
                message_id=message_id,
                position=pos,
            )
        )

    return papers


def parse_alert_email(raw: bytes | str) -> list[AlertPaper]:
    """Parse a complete RFC-822 message (an .eml file or a Gmail raw payload)."""
    msg = message_from_bytes(raw) if isinstance(raw, bytes) else message_from_string(raw)
    return parse_alert_html(
        _html_part(msg),
        message_id=msg.get("Message-ID"),
        subject=msg.get("Subject"),
    )


def dedupe(papers: list[AlertPaper]) -> list[AlertPaper]:
    """Collapse papers sharing a title, keeping the first occurrence.

    First-wins matters: the earliest alert is the one whose position ranking
    reflects Scholar's own relevance ordering for that query.
    """
    seen: set[str] = set()
    out: list[AlertPaper] = []
    for p in papers:
        if p.dedupe_key in seen:
            continue
        seen.add(p.dedupe_key)
        out.append(p)
    return out
