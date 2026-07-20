"""Fetch Google Scholar alert emails from Gmail.

The Gmail API can return a message as raw RFC-822, which is exactly what the
alert parser already consumes -- so this module does no parsing of its own. Its
job is to find the right messages, decode them faithfully, and remember which
ones have been handled so a re-run does not reprocess the same alerts.
"""

from __future__ import annotations

import base64
from typing import Iterator

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .imap_source import ALERT_SENDERS, RawMessage

# Gmail caps page size at 500.
_PAGE_SIZE = 100


class GmailError(Exception):
    """Gmail refused a request."""


def decode_raw(payload: str) -> bytes:
    """Decode Gmail's base64url message body.

    Gmail omits the "=" padding, which ``urlsafe_b64decode`` rejects outright,
    so it is restored here. Getting this wrong fails on some messages and not
    others depending on length, which is a miserable bug to chase.
    """
    padding = "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(payload + padding)


def build_query(
    *,
    senders: tuple[str, ...] = ALERT_SENDERS,
    newer_than_days: int | None = 2,
    extra: str | None = None,
) -> str:
    """Compose a Gmail search query for alert mail.

    The default window is deliberately wider than a day: the job runs on a
    laptop that may have been asleep at its scheduled time, and re-seeing a
    message is free because the ledger filters duplicates.
    """
    clauses = []
    if senders:
        joined = " OR ".join(f"from:{s}" for s in senders)
        clauses.append(f"({joined})" if len(senders) > 1 else joined)
    if newer_than_days:
        clauses.append(f"newer_than:{newer_than_days}d")
    if extra:
        clauses.append(extra)
    return " ".join(clauses)


class GmailClient:
    """Minimal Gmail wrapper: find alert messages, return them raw."""

    def __init__(self, credentials, *, service=None, user_id: str = "me") -> None:
        # `service` is injectable so tests never touch the network.
        self._service = service or build("gmail", "v1", credentials=credentials)
        self.user_id = user_id

    def list_message_ids(self, query: str, *, limit: int | None = None) -> list[str]:
        """Return message IDs matching a query, newest first, following pages."""
        ids: list[str] = []
        page_token = None
        try:
            while True:
                resp = (
                    self._service.users()
                    .messages()
                    .list(
                        userId=self.user_id,
                        q=query,
                        maxResults=_PAGE_SIZE,
                        pageToken=page_token,
                    )
                    .execute()
                )
                ids.extend(m["id"] for m in resp.get("messages", []))
                if limit is not None and len(ids) >= limit:
                    return ids[:limit]
                page_token = resp.get("nextPageToken")
                if not page_token:
                    return ids
        except HttpError as exc:
            raise GmailError(f"could not list messages: {exc}") from exc

    def fetch_raw(self, message_id: str) -> RawMessage:
        """Fetch one message as RFC-822 bytes."""
        try:
            resp = (
                self._service.users()
                .messages()
                .get(userId=self.user_id, id=message_id, format="raw")
                .execute()
            )
        except HttpError as exc:
            raise GmailError(f"could not fetch {message_id}: {exc}") from exc

        payload = resp.get("raw")
        if not payload:
            raise GmailError(f"{message_id} returned no raw body")
        return RawMessage(message_id=message_id, raw=decode_raw(payload))

    def fetch_alerts(
        self,
        *,
        senders: tuple[str, ...] = ALERT_SENDERS,
        newer_than_days: int | None = 2,
        skip: set[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[RawMessage]:
        """Yield unhandled alert messages, oldest first.

        Oldest-first matters: alerts are deduplicated first-wins, and the
        earliest sighting of a paper carries the position Scholar assigned it.
        """
        skip = skip or set()
        query = build_query(senders=senders, newer_than_days=newer_than_days)
        ids = [i for i in self.list_message_ids(query, limit=limit) if i not in skip]
        for message_id in reversed(ids):
            yield self.fetch_raw(message_id)
