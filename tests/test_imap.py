"""IMAP alert source tests.

The connection is a fake serving the real .eml fixtures, so the suite never
opens a socket or needs a mailbox.
"""

import imaplib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from paper_grabber.imap_source import (
    GMAIL_ALL_MAIL,
    PASSWORD_ENV,
    USER_ENV,
    ImapAlertSource,
    ImapConfig,
    ImapError,
    build_criteria,
    extract_message_id,
    imap_date,
)
from paper_grabber.parse import parse_alert_email

DATA = Path(__file__).parent / "data"
EMLS = sorted(DATA.glob("*.eml"))


class FakeIMAP:
    """Enough of imaplib's surface to drive the source."""

    def __init__(self, bodies, *, select_ok=True, search_ok=True, fetch_ok=True):
        self.bodies = bodies  # {seq_num_bytes: raw_bytes}
        self.select_ok = select_ok
        self.search_ok = search_ok
        self.fetch_ok = fetch_ok
        self.selected = None
        self.readonly = None
        self.criteria = None
        self.fetched = []
        self.logged_out = False

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        self.readonly = readonly
        return ("OK" if self.select_ok else "NO", [b""])

    def search(self, charset, *criteria):
        self.criteria = list(criteria)
        if not self.search_ok:
            return ("NO", [b""])
        return ("OK", [b" ".join(self.bodies)])

    def fetch(self, num, spec):
        self.fetched.append((num, spec))
        if not self.fetch_ok:
            return ("NO", [])
        return ("OK", [(b"%s (BODY[] {n}" % num, self.bodies[num]), b")"])

    def logout(self):
        self.logged_out = True


def source_from_fixtures(**kw):
    bodies = {str(i + 1).encode(): p.read_bytes() for i, p in enumerate(EMLS)}
    conn = FakeIMAP(bodies, **kw)
    cfg = ImapConfig(user="u@gmail.com", password="secret")
    return ImapAlertSource(cfg, connection=conn), conn


# --- config -------------------------------------------------------------------


def test_config_from_env(monkeypatch):
    monkeypatch.setenv(USER_ENV, "me@gmail.com")
    monkeypatch.setenv(PASSWORD_ENV, "abcd efgh ijkl mnop")
    cfg = ImapConfig.from_env()
    assert cfg.user == "me@gmail.com"
    assert cfg.mailbox == GMAIL_ALL_MAIL


def test_missing_password_explains_app_passwords(monkeypatch):
    monkeypatch.setenv(USER_ENV, "me@gmail.com")
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    with pytest.raises(ImapError, match="apppasswords"):
        ImapConfig.from_env()


def test_missing_user_is_reported(monkeypatch):
    monkeypatch.delenv(USER_ENV, raising=False)
    monkeypatch.setenv(PASSWORD_ENV, "x")
    with pytest.raises(ImapError, match="no IMAP user"):
        ImapConfig.from_env()


def test_password_never_appears_in_repr():
    cfg = ImapConfig(user="u@gmail.com", password="hunter2-app-password")
    assert "hunter2" not in repr(cfg)
    assert "u@gmail.com" in repr(cfg)


# --- search criteria ----------------------------------------------------------


def test_date_format_is_imap_style():
    assert imap_date(datetime(2026, 1, 5, tzinfo=timezone.utc)) == "05-Jan-2026"


def test_single_sender_criteria():
    c = build_criteria(senders=("a@x.com",), since_days=None)
    assert c == ["FROM", "a@x.com"]


def test_two_senders_fold_into_one_or():
    # IMAP's OR is prefix and binary: OR FROM a FROM b
    c = build_criteria(senders=("a@x.com", "b@x.com"), since_days=None)
    assert c == ["OR", "FROM", "a@x.com", "FROM", "b@x.com"]


def test_three_senders_fold_left():
    c = build_criteria(senders=("a", "b", "c"), since_days=None)
    assert c == ["OR", "OR", "FROM", "a", "FROM", "b", "FROM", "c"]
    assert c.count("OR") == 2  # N-1 ORs for N senders


def test_since_clause_is_added():
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    c = build_criteria(senders=(), since_days=2, now=now)
    assert c == ["SINCE", "17-Jul-2026"]


def test_no_criteria_falls_back_to_all():
    assert build_criteria(senders=(), since_days=None) == ["ALL"]


# --- message identity ---------------------------------------------------------


@pytest.mark.parametrize("path", EMLS, ids=lambda p: p.stem)
def test_message_id_extracted_from_real_fixtures(path):
    mid = extract_message_id(path.read_bytes())
    assert mid and mid.startswith("<") and mid.endswith(">")


def test_message_id_absent_returns_none():
    assert extract_message_id(b"Subject: no id here\r\n\r\nbody") is None


def test_message_id_matches_what_the_parser_reports():
    # The ledger key must agree with what parse_alert_email records, or the
    # same message would look new on every run.
    raw = EMLS[0].read_bytes()
    assert extract_message_id(raw) == parse_alert_email(raw)[0].message_id


# --- fetching -----------------------------------------------------------------


def test_fetches_every_fixture():
    source, conn = source_from_fixtures()
    msgs = list(source.fetch_alerts())
    assert len(msgs) == len(EMLS)
    assert all(m.raw.startswith(b"Delivered-To:") for m in msgs)


def test_fetched_messages_parse_into_papers():
    source, _ = source_from_fixtures()
    papers = []
    for msg in source.fetch_alerts():
        papers.extend(parse_alert_email(msg.raw))
    assert len(papers) == 16


def test_mailbox_is_opened_read_only():
    # Nothing here may mark mail as read or alter the mailbox.
    source, conn = source_from_fixtures()
    list(source.fetch_alerts())
    assert conn.readonly is True


def test_bodies_are_peeked_not_read():
    source, conn = source_from_fixtures()
    list(source.fetch_alerts())
    assert all(spec == "(BODY.PEEK[])" for _, spec in conn.fetched)


def test_skip_set_suppresses_known_messages():
    source, _ = source_from_fixtures()
    first = next(iter(source.fetch_alerts())).message_id
    source2, _ = source_from_fixtures()
    remaining = list(source2.fetch_alerts(skip={first}))
    assert first not in {m.message_id for m in remaining}
    assert len(remaining) == len(EMLS) - 1


def test_limit_keeps_the_most_recent():
    source, conn = source_from_fixtures()
    msgs = list(source.fetch_alerts(limit=1))
    assert len(msgs) == 1


def test_owned_connection_is_logged_out(monkeypatch):
    # An injected connection belongs to the caller and is left alone; one the
    # source opened itself must be closed, even though the fetch loop is a
    # generator that could be abandoned early.
    bodies = {b"1": EMLS[0].read_bytes()}
    conn = FakeIMAP(bodies)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port: conn)
    monkeypatch.setattr(conn, "login", lambda u, p: ("OK", [b""]), raising=False)

    source = ImapAlertSource(ImapConfig(user="u", password="p"))
    list(source.fetch_alerts())
    assert conn.logged_out


def test_injected_connection_is_left_open():
    source, conn = source_from_fixtures()
    list(source.fetch_alerts())
    assert not conn.logged_out  # the caller owns it


# --- failures -----------------------------------------------------------------


def test_bad_mailbox_is_reported():
    source, _ = source_from_fixtures(select_ok=False)
    with pytest.raises(ImapError, match="could not open mailbox"):
        list(source.fetch_alerts())


def test_search_failure_is_reported():
    source, _ = source_from_fixtures(search_ok=False)
    with pytest.raises(ImapError, match="search failed"):
        list(source.fetch_alerts())


def test_fetch_failure_is_reported():
    source, _ = source_from_fixtures(fetch_ok=False)
    with pytest.raises(ImapError, match="could not fetch"):
        list(source.fetch_alerts())


def test_login_failure_explains_app_password(monkeypatch):
    def boom(*a, **kw):
        raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    class FakeSSL:
        def __init__(self, host, port):
            pass

        login = boom

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeSSL)
    source = ImapAlertSource(ImapConfig(user="u", password="wrong"))
    with pytest.raises(ImapError, match="app password"):
        list(source.fetch_alerts())
