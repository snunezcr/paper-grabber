"""Free metadata sources used when OpenAlex is unavailable.

OpenAlex meters its API at $0.001 per request against a $0.10 daily allowance,
so a busy alert day exhausts it and every paper silently degrades to a Scholar
snippet. Crossref and Unpaywall are both free and unmetered, and between them
cover the two things that actually matter: a DOI with bibliographic metadata,
and a location for an open-access PDF.

Crossref's bibliographic search is markedly fuzzier than OpenAlex's title
search -- it will confidently return a different paper on a related topic --
so matches go through the same similarity and year checks, not a bare "first
result wins".
"""

from __future__ import annotations

import html
import re
from typing import Any, Callable

import httpx

from .enrich import Enrichment, _year_conflicts, title_similarity
from .models import AlertPaper

CROSSREF_WORKS = "https://api.crossref.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"

# Crossref abstracts arrive as JATS XML fragments.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Many begin with a redundant heading that adds nothing once tags are stripped.
_ABSTRACT_LEAD_RE = re.compile(r"^\s*abstract\b[:\s]*", re.IGNORECASE)


def clean_jats_abstract(raw: str | None) -> str | None:
    """Turn a JATS abstract fragment into plain text."""
    if not raw:
        return None
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _ABSTRACT_LEAD_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def best_match(
    paper: AlertPaper,
    candidates: list[dict[str, Any]],
    *,
    get_title: Callable[[dict[str, Any]], str],
    get_year: Callable[[dict[str, Any]], int | None],
    threshold: float,
) -> tuple[dict[str, Any] | None, float, bool]:
    """Pick the best candidate, or none. Returns (candidate, score, year_clash).

    Shared by every provider so a fuzzy source cannot slip a wrong paper
    through by being the fallback.
    """
    scored = sorted(
        ((title_similarity(paper.title, get_title(c) or ""), c) for c in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    year_clash = False
    for score, cand in scored:
        if score < threshold:
            break
        if _year_conflicts(paper.year, get_year(cand)):
            year_clash = True
            continue
        return cand, score, year_clash
    return None, (scored[0][0] if scored else 0.0), year_clash


class CrossrefClient:
    """Bibliographic metadata and DOIs. Free, unmetered, no key."""

    def __init__(
        self,
        *,
        mailto: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        threshold: float = 0.82,
    ) -> None:
        self.mailto = mailto
        self.threshold = threshold
        agent = "paper-grabber/0.1"
        if mailto:
            agent += f" (mailto:{mailto})"
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": agent}, follow_redirects=True
        )

    def search_by_title(self, title: str, *, rows: int = 5) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "query.bibliographic": title,
            "rows": rows,
            "select": "DOI,title,author,issued,container-title,abstract",
        }
        if self.mailto:
            params["mailto"] = self.mailto
        resp = self._client.get(CROSSREF_WORKS, params=params)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("items", []) or []

    def enrich(self, paper: AlertPaper) -> Enrichment:
        try:
            items = self.search_by_title(paper.title)
        except httpx.HTTPError as exc:
            return Enrichment(note=f"Crossref lookup failed: {type(exc).__name__}")

        if not items:
            return Enrichment(note="no Crossref candidates")

        cand, score, year_clash = best_match(
            paper,
            items,
            get_title=lambda c: (c.get("title") or [""])[0],
            get_year=_crossref_year,
            threshold=self.threshold,
        )
        if cand is None:
            note = (
                "Crossref: title matched but publication year conflicts"
                if year_clash
                else f"Crossref: best candidate scored {score:.2f} < {self.threshold}"
            )
            return Enrichment(match_score=round(score, 3), note=note)

        authors = [
            " ".join(part for part in (a.get("given"), a.get("family")) if part).strip()
            for a in (cand.get("author") or [])
        ]
        return Enrichment(
            matched=True,
            match_score=round(score, 3),
            doi=cand.get("DOI"),
            title=(cand.get("title") or [None])[0],
            abstract=clean_jats_abstract(cand.get("abstract")),
            year=_crossref_year(cand),
            authors=[a for a in authors if a],
            note="via Crossref",
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CrossrefClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _crossref_year(item: dict[str, Any]) -> int | None:
    parts = (item.get("issued") or {}).get("date-parts") or [[]]
    first = parts[0] if parts else []
    return first[0] if first and isinstance(first[0], int) else None


class UnpaywallClient:
    """Open-access PDF locations by DOI. Free, 100k requests/day, needs an email."""

    def __init__(
        self,
        *,
        email: str,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not email:
            raise ValueError("Unpaywall requires a contact email")
        self.email = email
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def pdf_locations(self, doi: str) -> list[str]:
        """PDF URLs for a DOI, best first.

        Only entries with an explicit ``url_for_pdf`` count. Unpaywall reports
        plenty of works as open access while offering nothing but a landing
        page, and fetching one of those returns HTML that would be filed as if
        it were the paper.
        """
        if not doi:
            return []
        try:
            resp = self._client.get(
                f"{UNPAYWALL}/{doi.lstrip('/')}", params={"email": self.email}
            )
            if resp.status_code != 200:
                return []
            # Unknown DOIs can come back as HTML rather than JSON.
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []

        urls: list[str] = []
        best = (data.get("best_oa_location") or {}).get("url_for_pdf")
        if best:
            urls.append(best)
        for loc in data.get("oa_locations") or []:
            pdf = (loc or {}).get("url_for_pdf")
            if pdf:
                urls.append(pdf)

        seen: set[str] = set()
        return [u for u in urls if not (u in seen or seen.add(u))]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "UnpaywallClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
