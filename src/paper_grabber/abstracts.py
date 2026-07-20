"""Recover abstracts for papers whose enrichment found none.

OpenAlex and Crossref between them leave most alert results with nothing but
Scholar's two-line snippet -- 54 of 67 in a real queue -- and a snippet is not
enough to judge whether a paper is worth reading. These sources fill the gap,
tried cheapest and most reliable first:

1. **arXiv** for anything with an arXiv id in its URL. Exact lookup, no
   searching, batched, and the abstract is the real one.
2. **Semantic Scholar**, but only with an API key: the shared anonymous pool
   returns 429 in practice, so without a key it is a waste of a request.
3. **Publisher page metadata**, last because it costs a full HTML fetch and
   some hosts refuse it outright.

Everything here is best-effort. A paper with no recoverable abstract keeps its
snippet, which is why the UI labels which of the two it is showing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

import httpx

ARXIV_API = "https://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"

S2_KEY_ENV = "SEMANTIC_SCHOLAR_API_KEY"

# arXiv ids as they appear in Scholar's links.
_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:pdf|abs)/((?:\d{4}\.\d{4,5})|[a-z\-]+/\d{7})", re.I)

_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
_ARXIV_ENTRY_ID_RE = re.compile(r"<id>.*?/abs/([^<]+)</id>", re.S)

# Ordered by trustworthiness: a citation_abstract really is the abstract,
# whereas a bare "description" is often site boilerplate.
_META_KEYS = (
    "citation_abstract",
    "dc.description",
    "dcterms.abstract",
    "og:description",
    "twitter:description",
    "description",
)
_META_RE = re.compile(
    r"""<meta[^>]+(?:name|property)\s*=\s*["']([^"']+)["'][^>]*content\s*=\s*["'](.*?)["']""",
    re.I | re.S,
)
# Some pages put content before name.
_META_REV_RE = re.compile(
    r"""<meta[^>]+content\s*=\s*["'](.*?)["'][^>]*(?:name|property)\s*=\s*["']([^"']+)["']""",
    re.I | re.S,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Shorter than this and it is a title, a teaser, or a cookie notice -- not an
# abstract worth showing in place of Scholar's snippet.
MIN_ABSTRACT_CHARS = 200

# Phrases that mark a page's generic blurb rather than the paper's abstract.
_BOILERPLATE = (
    "javascript is disabled",
    "enable javascript",
    "cookies",
    "sign in to",
    "subscribe to",
    "your browser",
    "access through your",
    "just a moment",
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class Abstract:
    text: str
    source: str


def arxiv_id_of(url: str | None) -> str | None:
    """The arXiv identifier in a URL, if there is one."""
    if not url:
        return None
    m = _ARXIV_ID_RE.search(url)
    return m.group(1) if m else None


def clean_text(raw: str | None) -> str | None:
    """Strip markup and collapse whitespace."""
    if not raw:
        return None
    import html as _html

    text = _html.unescape(_TAG_RE.sub(" ", raw))
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def looks_like_an_abstract(text: str | None) -> bool:
    """Whether a candidate is worth preferring over Scholar's snippet."""
    if not text or len(text) < MIN_ABSTRACT_CHARS:
        return False
    lowered = text.lower()
    return not any(phrase in lowered for phrase in _BOILERPLATE)


class ArxivAbstracts:
    """Exact lookup by identifier, batched."""

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    def fetch(self, arxiv_ids: Iterable[str], *, batch: int = 25) -> dict[str, Abstract]:
        """Map arXiv id -> abstract. Ids that return nothing are simply absent."""
        ids = [i for i in dict.fromkeys(arxiv_ids) if i]
        found: dict[str, Abstract] = {}

        for start in range(0, len(ids), batch):
            chunk = ids[start : start + batch]
            try:
                resp = self._client.get(
                    ARXIV_API, params={"id_list": ",".join(chunk), "max_results": len(chunk)}
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                continue

            for entry in _ENTRY_RE.findall(resp.text):
                entry_id = _ARXIV_ENTRY_ID_RE.search(entry)
                summary = _SUMMARY_RE.search(entry)
                if not (entry_id and summary):
                    continue
                # Strip the version suffix so 2607.11490v1 matches 2607.11490.
                bare = re.sub(r"v\d+$", "", entry_id.group(1))
                text = clean_text(summary.group(1))
                if looks_like_an_abstract(text):
                    found[bare] = Abstract(text, "arXiv")
        return found

    def close(self) -> None:
        self._client.close()


class SemanticScholarAbstracts:
    """Lookup by DOI. Requires a key -- the anonymous pool answers 429."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key or os.environ.get(S2_KEY_ENV)
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    @property
    def usable(self) -> bool:
        return bool(self.api_key)

    def fetch_one(self, doi: str) -> Abstract | None:
        if not doi or not self.usable:
            return None
        try:
            resp = self._client.get(
                f"{SEMANTIC_SCHOLAR}/DOI:{doi}",
                params={"fields": "abstract"},
                # Sent per request rather than set on the client: an injected
                # client would otherwise drop the key and fall back to the
                # anonymous pool without saying so.
                headers={"x-api-key": self.api_key} if self.api_key else None,
            )
            if resp.status_code != 200:
                return None
            text = clean_text((resp.json() or {}).get("abstract"))
        except (httpx.HTTPError, ValueError):
            return None
        return Abstract(text, "Semantic Scholar") if looks_like_an_abstract(text) else None

    def close(self) -> None:
        self._client.close()


class PageMetaAbstracts:
    """Read the abstract out of a publisher page's metadata."""

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 20.0) -> None:
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            follow_redirects=True,
        )

    def fetch_one(self, url: str) -> Abstract | None:
        if not url:
            return None
        try:
            resp = self._client.get(url)
            if resp.status_code != 200:
                return None
            # Only the head matters, and some publisher pages are enormous.
            html = resp.text[:400_000]
        except httpx.HTTPError:
            return None

        found: dict[str, str] = {}
        for name, content in _META_RE.findall(html):
            found.setdefault(name.strip().lower(), content)
        for content, name in _META_REV_RE.findall(html):
            found.setdefault(name.strip().lower(), content)

        for key in _META_KEYS:
            text = clean_text(found.get(key))
            if looks_like_an_abstract(text):
                return Abstract(text, key)
        return None

    def close(self) -> None:
        self._client.close()
