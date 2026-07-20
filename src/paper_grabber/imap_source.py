"""Fetch Scholar alert emails over IMAP.

An app password and IMAP avoid OAuth entirely for the mail side: no consent
screen, no verification, and no refresh token quietly expiring after seven days
and stopping the daily run.

The mailbox is opened read-only and bodies are fetched with BODY.PEEK, so
nothing here can mark mail as read, move it, or delete it. That is belt and
braces -- read-only SELECT already forbids flag changes -- but the guarantee is
worth making structurally rather than relying on one keyword argument.
"""

from __future__ import annotations

import imaplib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

# Scholar sends every kind of alert -- saved search, citations to your own
# work, followed authors -- from this one address.
ALERT_SENDERS = ("scholaralerts-noreply@google.com",)


@dataclass
class RawMessage:
    """One fetched message, still in RFC-822 form."""

    message_id: str
    raw: bytes


GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

# Gmail exposes archived mail here; INBOX alone misses anything auto-archived
# by a filter, which is exactly how most people handle alert mail.
GMAIL_ALL_MAIL = "[Gmail]/All Mail"

# Environment is the default home for the app password so it never has to be
# typed on a command line, where it would land in shell history.
PASSWORD_ENV = "PAPER_GRABBER_IMAP_PASSWORD"
USER_ENV = "PAPER_GRABBER_IMAP_USER"

_MESSAGE_ID_RE = re.compile(rb"^Message-ID:\s*(<[^>]+>)", re.IGNORECASE | re.MULTILINE)


class ImapError(Exception):
    """The mail server refused a request."""


@dataclass
class ImapConfig:
    user: str
    password: str
    host: str = GMAIL_IMAP_HOST
    port: int = GMAIL_IMAP_PORT
    mailbox: str = GMAIL_ALL_MAIL

    @classmethod
    def from_env(cls, **overrides) -> "ImapConfig":
        user = overrides.pop("user", None) or os.environ.get(USER_ENV)
        password = overrides.pop("password", None) or os.environ.get(PASSWORD_ENV)
        if not user:
            raise ImapError(f"no IMAP user; set {USER_ENV} or pass --user")
        if not password:
            raise ImapError(
                f"no IMAP app password; set {PASSWORD_ENV}. Create one at "
                "https://myaccount.google.com/apppasswords (requires 2-Step "
                "Verification)."
            )
        return cls(user=user, password=password, **overrides)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Never let the password reach a log or a traceback.
        return f"ImapConfig(user={self.user!r}, host={self.host!r}, mailbox={self.mailbox!r})"


def imap_date(when: datetime) -> str:
    """Format a date the way IMAP SEARCH expects: 01-Jan-2026."""
    return when.strftime("%d-%b-%Y")


def build_criteria(
    *,
    senders: tuple[str, ...] = ALERT_SENDERS,
    since_days: int | None = 2,
    now: datetime | None = None,
) -> list[str]:
    """Build IMAP SEARCH criteria as a token list.

    IMAP's OR is prefix and strictly binary, so N senders need N-1 ORs folded
    left; getting this wrong silently matches the wrong set rather than
    erroring.
    """
    criteria: list[str] = []

    if senders:
        terms = [["FROM", s] for s in senders]
        folded = terms[0]
        for term in terms[1:]:
            folded = ["OR"] + folded + term
        criteria += folded

    if since_days is not None:
        now = now or datetime.now(timezone.utc)
        # SEARCH SINCE is date-granular and inclusive, so this is a floor, not
        # an exact window -- fine, because the ledger filters what repeats.
        criteria += ["SINCE", imap_date(now - timedelta(days=since_days))]

    return criteria or ["ALL"]


def extract_message_id(raw: bytes) -> str | None:
    """Pull the RFC-822 Message-ID from a raw message.

    Used as the ledger key in preference to the IMAP UID: UIDs are per-mailbox
    and reset whenever UIDVALIDITY changes, which would make every message look
    new again. The Message-ID is stable for the life of the message.
    """
    match = _MESSAGE_ID_RE.search(raw)
    return match.group(1).decode("ascii", "replace") if match else None


class ImapAlertSource:
    """Reads Scholar alerts from an IMAP mailbox."""

    def __init__(self, config: ImapConfig, *, connection=None) -> None:
        self.config = config
        # `connection` is injectable so tests never open a socket.
        self._injected = connection

    @contextmanager
    def _connect(self):
        if self._injected is not None:
            yield self._injected
            return

        try:
            conn = imaplib.IMAP4_SSL(self.config.host, self.config.port)
        except OSError as exc:
            raise ImapError(f"could not reach {self.config.host}: {exc}") from exc

        try:
            conn.login(self.config.user, self.config.password)
        except imaplib.IMAP4.error as exc:
            raise ImapError(
                "IMAP login failed. With 2-Step Verification enabled this must "
                "be a 16-character app password, not the account password."
            ) from exc

        try:
            yield conn
        finally:
            try:
                conn.logout()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    def fetch_alerts(
        self,
        *,
        senders: tuple[str, ...] = ALERT_SENDERS,
        since_days: int | None = 2,
        skip: set[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[RawMessage]:
        """Yield unhandled alert messages, oldest first.

        IMAP returns sequence numbers in ascending order, which is already
        oldest-first -- the order dedupe wants, since it keeps the first
        sighting of each paper.
        """
        skip = skip or set()

        with self._connect() as conn:
            # readonly: this must never mark mail as read or alter the mailbox.
            status, _ = conn.select(self.config.mailbox, readonly=True)
            if status != "OK":
                raise ImapError(f"could not open mailbox {self.config.mailbox!r}")

            criteria = build_criteria(senders=senders, since_days=since_days)
            status, data = conn.search(None, *criteria)
            if status != "OK":
                raise ImapError(f"search failed: {criteria}")

            ids = (data[0] or b"").split()
            if limit is not None:
                ids = ids[-limit:]

            for num in ids:
                # PEEK so the \Seen flag is never set, even if the mailbox were
                # somehow opened writable.
                status, payload = conn.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not payload:
                    raise ImapError(f"could not fetch message {num!r}")

                raw = _extract_body(payload)
                if raw is None:
                    raise ImapError(f"message {num!r} returned no body")

                message_id = extract_message_id(raw) or num.decode("ascii", "replace")
                if message_id in skip:
                    continue
                yield RawMessage(message_id=message_id, raw=raw)


def _extract_body(payload) -> bytes | None:
    """Pull the message bytes out of imaplib's nested fetch response."""
    for part in payload:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return None


def check_login(config: ImapConfig) -> str:
    """Verify credentials and return a short description of the mailbox."""
    source = ImapAlertSource(config)
    with source._connect() as conn:
        status, _ = conn.select(config.mailbox, readonly=True)
        if status != "OK":
            raise ImapError(f"could not open mailbox {config.mailbox!r}")
        status, data = conn.search(None, *build_criteria(since_days=30))
        count = len((data[0] or b"").split()) if status == "OK" else 0
        return f"{config.user}: {count} Scholar alerts in the last 30 days"
