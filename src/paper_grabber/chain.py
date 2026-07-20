"""Enrichment across several sources, so one running dry does not stop the run.

Order matters and is not arbitrary:

1. **OpenAlex** first. It is the only source that returns a DOI, an abstract,
   and open-access locations in one request, so when it works it is a single
   call rather than three. It is also the only metered one.
2. **Crossref** when OpenAlex is exhausted or found nothing. Free and
   unmetered, authoritative for DOIs, but its abstracts are patchy and it
   knows nothing about open access.
3. **Unpaywall** last, and only to fill in PDF locations for a DOI the earlier
   steps produced. It is the authority on where an open-access copy lives.

Scholar's own link is always kept as a final PDF candidate: it is independent
of all three and often points straight at an arXiv PDF.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .enrich import Enrichment, RateLimited, direct_pdf_url
from .models import AlertPaper


class EnrichmentChain:
    """Tries each source in turn and merges what they return."""

    def __init__(
        self,
        *,
        openalex: Any | None = None,
        crossref: Any | None = None,
        unpaywall: Any | None = None,
    ) -> None:
        self.openalex = openalex
        self.crossref = crossref
        self.unpaywall = unpaywall
        # Once the budget is gone every later call fails identically, so stop
        # asking rather than spending a request per paper to be told again.
        self.openalex_exhausted = False
        self.rate_limit_note: str | None = None

    def enrich(self, paper: AlertPaper) -> Enrichment:
        result = self._primary(paper)

        if not result.matched and self.crossref is not None:
            fallback = self.crossref.enrich(paper)
            if fallback.matched:
                # Keep the primary's note when it explains why we fell through.
                result = fallback

        result = self._add_oa_locations(paper, result)
        return result

    def _primary(self, paper: AlertPaper) -> Enrichment:
        if self.openalex is None or self.openalex_exhausted:
            note = self.rate_limit_note or "OpenAlex not consulted"
            return Enrichment(note=note)

        try:
            return self.openalex.enrich(paper)
        except RateLimited as exc:
            self.openalex_exhausted = True
            self.rate_limit_note = f"OpenAlex budget exhausted: {exc}"
            return Enrichment(note=self.rate_limit_note)

    def _add_oa_locations(self, paper: AlertPaper, result: Enrichment) -> Enrichment:
        candidates = list(result.pdf_candidates)

        if not candidates and result.doi and self.unpaywall is not None:
            candidates.extend(self.unpaywall.pdf_locations(result.doi))

        # Independent of every API, and frequently an arXiv PDF.
        scholar = direct_pdf_url(paper)
        if scholar and scholar not in candidates:
            candidates.append(scholar)

        if candidates == result.pdf_candidates:
            return result
        return replace(result, pdf_candidates=candidates)

    def close(self) -> None:
        for client in (self.openalex, self.crossref, self.unpaywall):
            if client is not None and hasattr(client, "close"):
                client.close()

    def __enter__(self) -> "EnrichmentChain":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def build_chain(
    *,
    mailto: str | None,
    cache: Any | None = None,
    api_key: str | None = None,
    use_openalex: bool = True,
    use_crossref: bool = True,
    use_unpaywall: bool = True,
) -> EnrichmentChain:
    """Assemble the default chain.

    Unpaywall is skipped without a contact address: it requires one, and a
    request without it is rejected rather than merely impolite.
    """
    from .enrich import OpenAlexClient
    from .providers import CrossrefClient, UnpaywallClient

    return EnrichmentChain(
        openalex=(
            OpenAlexClient(mailto=mailto, cache=cache, api_key=api_key)
            if use_openalex
            else None
        ),
        crossref=CrossrefClient(mailto=mailto) if use_crossref else None,
        unpaywall=(
            UnpaywallClient(email=mailto) if (use_unpaywall and mailto) else None
        ),
    )
