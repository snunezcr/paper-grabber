"""Enrich alert records against OpenAlex.

Scholar gives us a title, a byline, and a two-line snippet. OpenAlex turns that
into a DOI, a real abstract, and -- crucially -- a location for an open-access
PDF. Matching is by title, which is fuzzy, so every match is scored and a weak
match is reported as *no* match rather than silently attaching another paper's
metadata to yours.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, asdict, field
from typing import Any, Iterable

import httpx

from .cache import LookupCache
from .models import AlertPaper, normalize_title

OPENALEX_WORKS = "https://api.openalex.org/works"

# OpenAlex asks for a contact address in exchange for the faster "polite pool".
_MAILTO_ENV = "OPENALEX_MAILTO"

# Below this title similarity we refuse the match. Title search is forgiving --
# it will happily return a different survey on the same topic -- and a wrong
# DOI is far worse than a missing one, since it silently misfiles the paper.
DEFAULT_MATCH_THRESHOLD = 0.82

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# OpenAlex filter syntax uses "," to AND clauses and "|" to OR them, so those
# characters inside a search *value* are parsed as syntax and the request 400s.
# Titles with commas are extremely common, so this is not an edge case.
_FILTER_SYNTAX_RE = re.compile(r"[,|]+")


def sanitize_search_value(value: str) -> str:
    """Strip characters OpenAlex would read as filter syntax."""
    # Collapse afterwards: "a, b" would otherwise become "a  b".
    return re.sub(r"\s+", " ", _FILTER_SYNTAX_RE.sub(" ", value)).strip()


class RateLimited(Exception):
    """OpenAlex refused the request for lack of budget.

    Raised rather than returned: once the daily allowance is gone every
    subsequent lookup fails identically, so a run should stop and keep what it
    already has instead of grinding through the rest.
    """

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    @classmethod
    def from_response(cls, response: httpx.Response) -> "RateLimited":
        try:
            body = response.json()
        except ValueError:
            body = {}
        retry = body.get("retryAfter")
        message = body.get("message") or "OpenAlex rate limit exceeded"
        return cls(message, retry_after=int(retry) if retry else None)


@dataclass
class Enrichment:
    """What OpenAlex could tell us about one alert record."""

    matched: bool = False
    match_score: float = 0.0
    openalex_id: str | None = None
    doi: str | None = None
    title: str | None = None
    abstract: str | None = None
    year: int | None = None
    is_oa: bool = False
    oa_status: str | None = None
    # Genuine PDF URLs, best first. A landing page is NOT a candidate: fetching
    # one yields an HTML interstitial, which would be filed as if it were the
    # paper. Landing pages live in landing_url for the "open in browser" path.
    pdf_candidates: list[str] = field(default_factory=list)
    landing_url: str | None = None
    cited_by_count: int | None = None
    authors: list[str] = field(default_factory=list)
    # Why enrichment produced nothing useful, for triage in the UI and logs.
    note: str | None = None

    @property
    def pdf_url(self) -> str | None:
        """The best PDF URL, or None if only landing pages are known."""
        return self.pdf_candidates[0] if self.pdf_candidates else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pdf_url"] = self.pdf_url
        return d


def reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """Rebuild plain text from OpenAlex's inverted index.

    OpenAlex stores abstracts as {word: [positions]} for copyright reasons.
    Publishers that suppress abstracts leave the field present but empty, so an
    empty index is a legitimate "no abstract", not an error.
    """
    if not inverted:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        positions.extend((i, word) for i in idxs)
    if not positions:
        return None
    positions.sort()
    return " ".join(word for _, word in positions)


def title_similarity(a: str, b: str) -> float:
    """Token-set similarity between two titles, in [0, 1].

    Deliberately token-based rather than character-based: Scholar reorders
    subtitles and drops punctuation, neither of which should count against a
    match, while a genuinely different paper shares few tokens.

    This is the Dice coefficient -- symmetric, so a candidate that contains
    every one of our tokens *plus several more* is penalised for the extras.
    An overlap coefficient (dividing by the shorter title) scored
    "Quantum Machine Learning: Bridging Quantum Computing & Machine Learning"
    against "...Bridging Quantum Computing and Artificial Intelligence" at a
    perfect 1.0 -- a different paper, from a different year.
    """
    ta = set(_TOKEN_RE.findall(normalize_title(a)))
    tb = set(_TOKEN_RE.findall(normalize_title(b)))
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def _year_conflicts(alert_year: int | None, work_year: int | None) -> bool:
    """True when two publication years are too far apart to be one paper.

    A year of slack absorbs the usual preprint-to-publication drift; beyond
    that, a title match is almost certainly a different paper with a similar
    name.
    """
    if alert_year is None or work_year is None:
        return False
    return abs(alert_year - work_year) > 1


def direct_pdf_url(paper: AlertPaper) -> str | None:
    """Treat Scholar's own link as a PDF when it plainly is one.

    OpenAlex sometimes reports a work as closed while Scholar has already
    pointed at an arXiv or repository PDF, so this recovers those cases.
    """
    url = paper.url or ""
    if not url:
        return None
    lowered = url.lower()
    if lowered.endswith(".pdf") or "/pdf/" in lowered:
        return url
    if re.search(r"arxiv\.org/abs/", lowered):
        return re.sub(r"/abs/", "/pdf/", url, count=1)
    return None


def _pdf_candidates(work: dict[str, Any], paper: AlertPaper) -> list[str]:
    """Ordered, de-duplicated PDF URLs for a work.

    Only URLs that OpenAlex explicitly labels ``pdf_url`` count, plus Scholar's
    own link when it is plainly a PDF. ``open_access.oa_url`` is deliberately
    excluded: for repository-hosted works it is usually a DOI landing page, and
    fetching it returns HTML that would be filed as though it were the paper.
    """
    urls: list[str] = []

    best = (work.get("best_oa_location") or {}).get("pdf_url")
    if best:
        urls.append(best)

    # Later locations are worth keeping as fallbacks: the preferred host is
    # sometimes down or bot-blocked while a mirror serves the same file.
    for loc in work.get("locations") or []:
        pdf = (loc or {}).get("pdf_url")
        if pdf:
            urls.append(pdf)

    scholar = direct_pdf_url(paper)
    if scholar:
        urls.append(scholar)

    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


class OpenAlexClient:
    """Thin OpenAlex wrapper.

    Kept small on purpose: the interesting logic is matching and OA selection,
    not HTTP.
    """

    def __init__(
        self,
        *,
        mailto: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        threshold: float = DEFAULT_MATCH_THRESHOLD,
        cache: "LookupCache | None" = None,
    ) -> None:
        self.mailto = mailto or os.environ.get(_MAILTO_ENV)
        self.threshold = threshold
        self.cache = cache
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": self._user_agent()},
            follow_redirects=True,
        )

    def _user_agent(self) -> str:
        ua = "paper-grabber/0.1"
        return f"{ua} (mailto:{self.mailto})" if self.mailto else ua

    def _params(self, **extra: Any) -> dict[str, Any]:
        params = dict(extra)
        if self.mailto:
            params["mailto"] = self.mailto
        return params

    def search_by_title(self, title: str, *, per_page: int = 5) -> list[dict[str, Any]]:
        """Return candidate works for a title, best-first per OpenAlex."""
        resp = self._client.get(
            OPENALEX_WORKS,
            params=self._params(
                filter=f"title.search:{sanitize_search_value(title)}",
                per_page=per_page,
            ),
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def enrich(self, paper: AlertPaper) -> Enrichment:
        """Look up one alert record and score the best candidate.

        A cache hit costs nothing; a miss costs OpenAlex budget, so the cache
        is consulted first and written on every outcome.
        """
        if self.cache is not None:
            cached = self.cache.get(paper.title)
            if cached is not None:
                return Enrichment(**cached)

        try:
            candidates = self.search_by_title(paper.title)
        except httpx.HTTPStatusError as exc:
            # OpenAlex meters requests against a daily budget. A 429 is not a
            # transient network blip: every later call in the run will fail the
            # same way, and the caller needs to know to stop rather than churn
            # through a hundred doomed lookups.
            if exc.response.status_code == 429:
                raise RateLimited.from_response(exc.response) from exc
            return Enrichment(note=f"lookup failed: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return Enrichment(note=f"lookup failed: {type(exc).__name__}")

        if not candidates:
            return self._remember(paper, Enrichment(note="no OpenAlex candidates"))

        scored = [
            (title_similarity(paper.title, html.unescape(c.get("display_name") or "")), c)
            for c in candidates
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        # Walk candidates best-first, skipping any whose year rules it out, so
        # a near-duplicate title from the wrong year cannot shadow the right
        # paper sitting one position below it.
        rejected_on_year = False
        for score, cand in scored:
            if score < self.threshold:
                break
            if _year_conflicts(paper.year, cand.get("publication_year")):
                rejected_on_year = True
                continue
            return self._remember(paper, self._from_work(cand, score, paper))

        best_score = scored[0][0] if scored else 0.0
        note = (
            "title matched but publication year conflicts"
            if rejected_on_year
            else f"best candidate scored {best_score:.2f} < {self.threshold}"
        )
        return self._remember(paper, Enrichment(match_score=round(best_score, 3), note=note))

    def _remember(self, paper: AlertPaper, result: Enrichment) -> Enrichment:
        """Persist an outcome so the next run does not pay for it again."""
        if self.cache is not None:
            payload = asdict(result)  # not to_dict(): pdf_url is derived
            self.cache.put(paper.title, payload, matched=result.matched)
        return result

    def _from_work(
        self, work: dict[str, Any], score: float, paper: AlertPaper
    ) -> Enrichment:
        oa = work.get("open_access") or {}
        best_loc = work.get("best_oa_location") or {}

        candidates = _pdf_candidates(work, paper)
        note = None
        if not candidates:
            note = (
                "open access, but only a landing page is known"
                if oa.get("is_oa")
                else None
            )

        doi = work.get("doi")
        return Enrichment(
            matched=True,
            match_score=round(score, 3),
            openalex_id=work.get("id"),
            doi=doi.removeprefix("https://doi.org/") if doi else None,
            # OpenAlex stores some titles with HTML entities still escaped
            # ("Computing &amp; Learning"); unescape before it reaches a filename.
            title=html.unescape(work["display_name"]) if work.get("display_name") else None,
            abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
            year=work.get("publication_year"),
            is_oa=bool(oa.get("is_oa")),
            oa_status=oa.get("oa_status"),
            pdf_candidates=candidates,
            landing_url=best_loc.get("landing_page_url") or oa.get("oa_url"),
            cited_by_count=work.get("cited_by_count"),
            authors=[
                a["author"]["display_name"]
                for a in work.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ],
            note=note,
        )

    def enrich_all(self, papers: Iterable[AlertPaper]) -> list[Enrichment]:
        return [self.enrich(p) for p in papers]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenAlexClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
