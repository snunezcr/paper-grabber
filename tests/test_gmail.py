"""Gmail fetching tests.

The Gmail service is a fake built from the real .eml fixtures, so the whole
path -- Gmail's base64url encoding, decoding, and the existing alert parser --
is exercised without a network or a mailbox.
"""

import base64
from pathlib import Path

import pytest
from googleapiclient.errors import HttpError

from paper_grabber.imap_source import ALERT_SENDERS
from paper_grabber.gmail import (
    GmailClient,
    GmailError,
    build_query,
    decode_raw,
)
from paper_grabber.parse import parse_alert_email

DATA = Path(__file__).parent / "data"
EMLS = sorted(DATA.glob("*.eml"))


def gmail_encode(raw: bytes) -> str:
    """Encode as Gmail does: base64url with the padding stripped."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class FakeMessages:
    def __init__(self, pages, bodies, get_error=None, list_error=None):
        self.pages = list(pages)
        self.bodies = bodies
        self.get_error = get_error
        self.list_error = list_error
        self.queries = []
        self.fetched = []

    def list(self, userId=None, q=None, maxResults=None, pageToken=None):
        self.queries.append(q)
        return _Req(lambda: self._page(pageToken), self.list_error)

    def _page(self, token):
        idx = 0 if token is None else int(token)
        return self.pages[idx]

    def get(self, userId=None, id=None, format=None):
        self.fetched.append(id)
        body = self.bodies.get(id)
        return _Req(lambda: ({"raw": body} if body is not None else {}), self.get_error)


class _Req:
    def __init__(self, fn, error=None):
        self._fn = fn
        self._error = error

    def execute(self):
        if self._error:
            raise self._error
        return self._fn()


class FakeService:
    def __init__(self, messages):
        self._m = messages

    def users(self):
        return self

    def messages(self):
        return self._m


def http_error(status=403):
    class Resp:
        def __init__(self):
            self.status = status
            self.reason = "error"

    return HttpError(Resp(), b"{}")


def client_from_fixtures():
    bodies = {p.stem: gmail_encode(p.read_bytes()) for p in EMLS}
    ids = list(bodies)
    messages = FakeMessages(pages=[{"messages": [{"id": i} for i in ids]}], bodies=bodies)
    return GmailClient(credentials=None, service=FakeService(messages)), messages, ids


# --- encoding -----------------------------------------------------------------


def test_decode_handles_missing_padding():
    # Gmail strips "=", which urlsafe_b64decode rejects. Lengths that need
    # 0, 1, and 2 padding characters must all work.
    for raw in (b"abc", b"abcd", b"abcde"):
        assert decode_raw(gmail_encode(raw)) == raw


def test_decode_handles_url_unsafe_bytes():
    # Bytes that encode to "+" and "/" in standard base64 must survive.
    raw = bytes(range(256))
    assert decode_raw(gmail_encode(raw)) == raw


@pytest.mark.parametrize("path", EMLS, ids=lambda p: p.stem)
def test_real_fixture_survives_the_gmail_round_trip(path):
    raw = path.read_bytes()
    assert decode_raw(gmail_encode(raw)) == raw


@pytest.mark.parametrize("path", EMLS, ids=lambda p: p.stem)
def test_parsing_is_identical_through_gmail(path):
    # The whole point: what Gmail hands back must parse exactly as the .eml did.
    direct = parse_alert_email(path.read_bytes())
    viagmail = parse_alert_email(decode_raw(gmail_encode(path.read_bytes())))
    assert [p.to_dict() for p in direct] == [p.to_dict() for p in viagmail]


# --- query construction -------------------------------------------------------


def test_default_query_targets_scholar_alerts():
    q = build_query()
    assert "from:scholaralerts-noreply@google.com" in q
    assert "newer_than:2d" in q


def test_multiple_senders_are_ORed():
    q = build_query(senders=("a@x.com", "b@x.com"))
    assert q.startswith("(from:a@x.com OR from:b@x.com)")


def test_window_can_be_widened():
    assert "newer_than:30d" in build_query(newer_than_days=30)


def test_window_can_be_omitted():
    assert "newer_than" not in build_query(newer_than_days=None)


def test_extra_clause_is_appended():
    assert build_query(extra="label:unread").endswith("label:unread")


# --- listing and fetching -----------------------------------------------------


def test_lists_and_fetches_every_fixture():
    client, messages, ids = client_from_fixtures()
    fetched = list(client.fetch_alerts())
    assert len(fetched) == len(ids)
    assert all(m.raw.startswith(b"Delivered-To:") for m in fetched)


def test_fetched_messages_parse_into_papers():
    client, _, _ = client_from_fixtures()
    papers = []
    for msg in client.fetch_alerts():
        papers.extend(parse_alert_email(msg.raw))
    # The three fixtures carry 10 + 2 + 4 results.
    assert len(papers) == 16


def test_already_seen_messages_are_skipped():
    client, messages, ids = client_from_fixtures()
    got = list(client.fetch_alerts(skip={ids[0]}))
    assert len(got) == len(ids) - 1
    assert ids[0] not in messages.fetched


def test_oldest_message_is_yielded_first():
    # Gmail returns newest-first; dedupe is first-wins, so the earliest
    # sighting of a paper must be the one that lands.
    client, _, ids = client_from_fixtures()
    order = [m.message_id for m in client.fetch_alerts()]
    assert order == list(reversed(ids))


def test_pagination_is_followed():
    bodies = {"a": gmail_encode(b"A"), "b": gmail_encode(b"B")}
    messages = FakeMessages(
        pages=[
            {"messages": [{"id": "a"}], "nextPageToken": "1"},
            {"messages": [{"id": "b"}]},
        ],
        bodies=bodies,
    )
    c = GmailClient(credentials=None, service=FakeService(messages))
    assert c.list_message_ids("q") == ["a", "b"]


def test_limit_stops_early():
    messages = FakeMessages(
        pages=[{"messages": [{"id": x} for x in "abcde"]}], bodies={}
    )
    c = GmailClient(credentials=None, service=FakeService(messages))
    assert c.list_message_ids("q", limit=2) == ["a", "b"]


def test_empty_mailbox_is_not_an_error():
    messages = FakeMessages(pages=[{}], bodies={})
    c = GmailClient(credentials=None, service=FakeService(messages))
    assert c.list_message_ids("q") == []


# --- failures -----------------------------------------------------------------


def test_list_failure_becomes_gmail_error():
    messages = FakeMessages(pages=[{}], bodies={}, list_error=http_error())
    c = GmailClient(credentials=None, service=FakeService(messages))
    with pytest.raises(GmailError, match="could not list"):
        c.list_message_ids("q")


def test_fetch_failure_becomes_gmail_error():
    messages = FakeMessages(pages=[{}], bodies={}, get_error=http_error(500))
    c = GmailClient(credentials=None, service=FakeService(messages))
    with pytest.raises(GmailError, match="could not fetch"):
        c.fetch_raw("x")


def test_message_without_a_body_is_reported():
    messages = FakeMessages(pages=[{}], bodies={})
    c = GmailClient(credentials=None, service=FakeService(messages))
    with pytest.raises(GmailError, match="no raw body"):
        c.fetch_raw("missing")
