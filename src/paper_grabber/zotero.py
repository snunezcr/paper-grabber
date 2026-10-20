"""Write a filed paper into a Zotero library.

One item per paper, built from what enrichment already knows, plus two
children where there is anything to put in them: the filing note, and a link
to the PDF in Drive. The PDF itself is deliberately not uploaded -- this app
keeps files in Drive on purpose, and Zotero's own storage is small and paid
for -- so what Zotero gets is a reference that clicks through to the copy that
already exists.

Metadata is best-effort in the same way bibtex.py is: Scholar gives initials,
OpenAlex gives full names, and plenty of papers have no venue at all. The one
thing never done is passing Scholar's two-line snippet off as an abstract; a
sparse record is honest, a wrong one is not.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any

import httpx

from .bibtex import entry_type
from .clean import short_venue
from .models import split_name

API_BASE = "https://api.zotero.org"
API_VERSION = "3"

# Zotero's cap for one write request. Far above anything a single card sends,
# but the limit the batching below must respect if it ever grows.
MAX_ITEMS_PER_REQUEST = 50

_TIMEOUT = httpx.Timeout(30.0)

# BibTeX has no preprint entry type, so entry_type folds a preprint into
# @article (it has a venue) or @misc (it does not). Zotero's vocabulary is
# finer; these are the nearest equivalents of the same judgement.
_ITEM_TYPES = {
    "inproceedings": "conferencePaper",
    "article": "journalArticle",
    "misc": "preprint",
}

# Where the venue goes differs by item type, and Zotero rejects a field the
# type does not declare -- a journal name in `repository` is an error, not a
# near miss.
_VENUE_FIELD = {
    "conferencePaper": "proceedingsTitle",
    "journalArticle": "publicationTitle",
    "preprint": "repository",
}

# A venue naming one of these is a preprint server, not a journal. Worth
# detecting because most of a Scholar queue is arXiv, and "arXiv preprint
# arXiv:2607.13699" as a journal name would be wrong in every bibliography
# the library is ever exported into.
_PREPRINT_HINTS = (
    "arxiv", "preprint", "biorxiv", "medrxiv", "chemrxiv",
    "ssrn", "osf.io", "hal-", "research square", "authorea",
)


class ZoteroWriteError(Exception):
    """The item could not be written to the library."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item_type(venue: str | None, source_url: str | None = None) -> str:
    """Pick the Zotero item type from what little we know of where it appeared."""
    lowered = (venue or "").lower()
    if any(hint in lowered for hint in _PREPRINT_HINTS):
        return "preprint"
    return _ITEM_TYPES[entry_type(venue, source_url)]


def creators(authors: list[str]) -> list[dict[str, str]]:
    """Render a byline as Zotero's two-field creator records.

    Two fields rather than one, because that is what Zotero sorts and cites
    by. The split is models.split_name, so a name is divided the same way here
    as it is for BibTeX.
    """
    out = []
    for name in authors:
        given, surname = split_name(name)
        if not surname:
            continue
        out.append(
            {"creatorType": "author", "firstName": given, "lastName": surname}
        )
    return out


def to_item(view: dict[str, Any]) -> dict[str, Any]:
    """Build the Zotero item for a paper_view dict.

    itemType, tags, collections and relations are sent whatever their value:
    Zotero requires all four on a top-level item.
    """
    kind = item_type(view.get("venue"), view.get("source_url"))
    item: dict[str, Any] = {
        "itemType": kind,
        "title": (view.get("title") or "Untitled").strip(),
        "creators": creators([a for a in (view.get("authors") or []) if a]),
        "tags": [],
        "collections": [],
        "relations": {},
    }

    # Only a real abstract. abstract_is_snippet means the text is Scholar's
    # two truncated lines, which would be a worse record than none.
    if view.get("abstract") and not view.get("abstract_is_snippet"):
        item["abstractNote"] = view["abstract"]

    venue = view.get("venue")
    if venue:
        # For a preprint the folded form is what belongs here -- the repository
        # is "arXiv", not "arXiv preprint arXiv:2607.13699". Elsewhere the full
        # journal or proceedings name is the right value, untruncated.
        item[_VENUE_FIELD[kind]] = short_venue(venue) if kind == "preprint" else venue

    if view.get("year"):
        item["date"] = str(view["year"])
    if view.get("doi"):
        item["DOI"] = view["doi"]

    # Where the paper lives, preferring the publisher's page over a direct PDF
    # link: the landing page keeps working when a PDF URL goes stale.
    url = view.get("source_url") or view.get("pdf_url")
    if url:
        item["url"] = url
        item["accessDate"] = _now()

    if view.get("alert_query"):
        # The saved search that surfaced it. Provenance the user already
        # organises by, and the only thing here Zotero could not derive.
        item["tags"] = [{"tag": view["alert_query"]}]

    return item


def _note_html(text: str) -> str:
    """Zotero notes are HTML, so a note written as plain text is escaped."""
    paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    return "".join(
        "<p>" + html_escape(p).replace("\n", "<br/>") + "</p>" for p in paragraphs
    )


def child_items(view: dict[str, Any], *, parent_key: str) -> list[dict[str, Any]]:
    """The note and the Drive link, for whichever of them exists."""
    children: list[dict[str, Any]] = []

    note = (view.get("note") or "").strip()
    if note:
        children.append(
            {
                "itemType": "note",
                "parentItem": parent_key,
                "note": _note_html(note),
                "tags": [],
                "relations": {},
            }
        )

    if view.get("drive_url"):
        # A linked URL, not a stored file: the PDF lives in Drive by design,
        # and this is the click-through to it from the reference.
        children.append(
            {
                "itemType": "attachment",
                "linkMode": "linked_url",
                "parentItem": parent_key,
                "title": "PDF in Google Drive",
                "url": view["drive_url"],
                "accessDate": _now(),
                "contentType": "application/pdf",
                "tags": [],
                "relations": {},
            }
        )

    return children


class ZoteroLibrary:
    """Writes items into one user's personal Zotero library."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.user_id = user_id
        self._http = http_client

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if self._http is not None:
            return self._http.request(method, url, **kw)
        with httpx.Client(timeout=_TIMEOUT) as client:
            return client.request(method, url, **kw)

    def _post(self, items: list[dict[str, Any]]) -> list[str]:
        """POST a batch of items and return the keys Zotero assigned."""
        if len(items) > MAX_ITEMS_PER_REQUEST:
            raise ZoteroWriteError(
                f"{len(items)} items is more than Zotero accepts at once "
                f"({MAX_ITEMS_PER_REQUEST})"
            )
        try:
            response = self._request(
                "POST",
                f"{API_BASE}/users/{self.user_id}/items",
                headers={
                    "Zotero-API-Key": self.api_key,
                    "Zotero-API-Version": API_VERSION,
                    "Content-Type": "application/json",
                    # Makes a repeated identical write a no-op at Zotero's end
                    # for a minute, so a double tap cannot produce two items.
                    "Zotero-Write-Token": secrets.token_hex(16),
                },
                json=items,
                # The key rides in a custom header, which httpx would carry
                # across a redirect; see the same guard in zotero_auth.
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ZoteroWriteError(f"could not reach Zotero: {exc}") from exc

        if response.is_redirect:
            raise ZoteroWriteError(
                "Zotero redirected the write; the endpoint has moved and this "
                "app needs updating"
            )
        if response.status_code in (401, 403):
            raise ZoteroWriteError(
                "Zotero refused the write. The connected key may not have "
                "write access -- reconnect it from the Filing tab."
            )
        if response.status_code in (429, 503):
            wait = response.headers.get("Retry-After") or response.headers.get("Backoff")
            when = f" Try again in {wait} seconds." if wait else " Try again shortly."
            raise ZoteroWriteError(f"Zotero is busy and would not answer.{when}")
        if response.status_code >= 400:
            raise ZoteroWriteError(f"Zotero answered {response.status_code} to the write")

        try:
            body = response.json()
        except ValueError as exc:
            raise ZoteroWriteError(f"Zotero's reply was not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ZoteroWriteError("Zotero's reply was not a write result")

        failed = body.get("failed") or {}
        if failed:
            # Keyed by the index in the request. One message is enough: these
            # batches are one or two items, and the first reason is the reason.
            first = next(iter(failed.values()))
            detail = first.get("message") if isinstance(first, dict) else str(first)
            raise ZoteroWriteError(f"Zotero rejected the item: {detail}")

        # Zotero's docs use both names for the same thing; successful carries
        # the saved objects, success just their keys.
        successful = body.get("successful") or {}
        if successful:
            return [
                obj.get("key", "") if isinstance(obj, dict) else str(obj)
                for _, obj in sorted(successful.items(), key=lambda kv: int(kv[0]))
            ]
        success = body.get("success") or {}
        if success:
            return [v for _, v in sorted(success.items(), key=lambda kv: int(kv[0]))]
        raise ZoteroWriteError("Zotero reported neither a success nor a failure")

    def save(self, view: dict[str, Any]) -> dict[str, Any]:
        """Create the item and its children, returning the item's key.

        The children go in a second request because a child has to name its
        parent's key, and Zotero assigns that key. If they fail the item still
        stands, so the failure is reported as a warning rather than raised --
        losing the reference over a missing note would be the worse outcome.
        """
        keys = self._post([to_item(view)])
        if not keys or not keys[0]:
            raise ZoteroWriteError("Zotero did not return an item key")
        item_key = keys[0]

        warnings: list[str] = []
        children = child_items(view, parent_key=item_key)
        if children:
            try:
                self._post(children)
            except ZoteroWriteError as exc:
                warnings.append(str(exc))

        return {"item_key": item_key, "warnings": warnings}
