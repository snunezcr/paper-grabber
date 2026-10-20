"""Writing a filed paper into a Zotero library. No network."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from paper_grabber.ledger import Decision, Ledger, paper_view
from paper_grabber.models import AlertPaper
from paper_grabber.server import create_app
from paper_grabber.zotero import (
    ZoteroLibrary,
    ZoteroWriteError,
    child_items,
    creators,
    item_type,
    to_item,
)
from paper_grabber.zotero_auth import ZoteroAuth

KEY = "P9NiFoyLeZu2bZNvvuQPDWsd"
USER = "475425"
ITEM = "ABCD2345"


def view(**over):
    """A paper_view dict of the shape the server hands the library."""
    base = {
        "key": "k1",
        "title": "A Quantum Abstract Machine",
        "authors": ["AM Hafiz", "Abdul Mueed Ghaffar", "Plato"],
        "venue": "Image and Vision Computing",
        "year": 2026,
        "abstract": "A real abstract, recovered from the publisher.",
        "abstract_is_snippet": False,
        "doi": "10.1/abc",
        "source_url": "https://ex.org/paper",
        "pdf_url": "https://ex.org/paper.pdf",
        "alert_query": "quantum computer architecture",
        "note": "Worth re-reading the error-correction section.",
        "drive_url": "https://drive.google.com/file/d/fid/view",
        "uploaded": True,
        "in_zotero": False,
    }
    base.update(over)
    return base


# --- item type ----------------------------------------------------------------


@pytest.mark.parametrize("venue,expected", [
    ("Image and Vision Computing", "journalArticle"),
    ("Proceedings of the 2026 Conference on X", "conferencePaper"),
    ("2026 IEEE International Symposium on Y", "conferencePaper"),
    # Most of a Scholar queue. A preprint server is not a journal, and naming
    # it as one would be wrong in every bibliography this is exported into.
    ("arXiv preprint arXiv:2607.13699", "preprint"),
    ("bioRxiv", "preprint"),
    ("SSRN Electronic Journal", "preprint"),
    (None, "preprint"),
    ("", "preprint"),
])
def test_item_type_from_the_venue(venue, expected):
    assert item_type(venue, "https://ex.org/a") == expected


# --- field mapping ------------------------------------------------------------


def test_creators_are_split_into_zotero_s_two_fields():
    assert creators(["AM Hafiz", "Abdul Mueed Ghaffar"]) == [
        {"creatorType": "author", "firstName": "AM", "lastName": "Hafiz"},
        {"creatorType": "author", "firstName": "Abdul Mueed", "lastName": "Ghaffar"},
    ]


def test_a_single_word_name_is_all_surname():
    """What a reference is sorted and cited by."""
    assert creators(["Plato"]) == [
        {"creatorType": "author", "firstName": "", "lastName": "Plato"}
    ]


def test_an_empty_name_is_dropped_rather_than_sent_blank():
    assert creators(["", "   "]) == []


def test_the_item_carries_what_zotero_requires_of_a_top_level_item():
    item = to_item(view())
    for required in ("itemType", "tags", "collections", "relations"):
        assert required in item


def test_a_journal_article_maps_the_venue_to_publicationTitle():
    item = to_item(view())
    assert item["itemType"] == "journalArticle"
    assert item["publicationTitle"] == "Image and Vision Computing"
    assert item["date"] == "2026"
    assert item["DOI"] == "10.1/abc"
    # The landing page, not the PDF: a PDF URL goes stale, the page does not.
    assert item["url"] == "https://ex.org/paper"
    assert item["accessDate"].endswith("Z")


def test_a_conference_paper_maps_the_venue_to_proceedingsTitle():
    item = to_item(view(venue="Proceedings of NeurIPS 2026"))
    assert item["itemType"] == "conferencePaper"
    assert item["proceedingsTitle"] == "Proceedings of NeurIPS 2026"
    assert "publicationTitle" not in item


def test_a_preprint_folds_the_venue_to_the_repository_name():
    """"arXiv", not "arXiv preprint arXiv:2607.13699"."""
    item = to_item(view(venue="arXiv preprint arXiv:2607.13699"))
    assert item["itemType"] == "preprint"
    assert item["repository"] == "arXiv"


def test_a_paper_with_no_venue_sets_no_venue_field():
    item = to_item(view(venue=None))
    assert "repository" not in item
    assert "publicationTitle" not in item


def test_scholar_s_snippet_is_never_passed_off_as_an_abstract():
    """A sparse record is honest; a wrong one is not."""
    item = to_item(view(abstract="Two truncated lines…", abstract_is_snippet=True))
    assert "abstractNote" not in item


def test_a_real_abstract_is_kept():
    assert to_item(view())["abstractNote"].startswith("A real abstract")


def test_the_saved_search_becomes_a_tag():
    assert to_item(view())["tags"] == [{"tag": "quantum computer architecture"}]


def test_no_tag_when_the_paper_had_no_alert():
    assert to_item(view(alert_query=None))["tags"] == []


def test_a_missing_title_does_not_produce_an_untitled_blank():
    assert to_item(view(title=None))["title"] == "Untitled"


# --- children -----------------------------------------------------------------


def test_the_note_and_the_drive_link_become_children():
    kids = child_items(view(), parent_key=ITEM)
    assert [k["itemType"] for k in kids] == ["note", "attachment"]
    assert all(k["parentItem"] == ITEM for k in kids)

    note, link = kids
    assert "error-correction" in note["note"]
    # Zotero notes are HTML.
    assert note["note"].startswith("<p>")
    # A linked URL, not a stored file: the PDF lives in Drive by design.
    assert link["linkMode"] == "linked_url"
    assert link["url"] == "https://drive.google.com/file/d/fid/view"


def test_a_note_is_escaped_into_its_html():
    kids = child_items(view(note="Compare <A> & <B>"), parent_key=ITEM)
    assert "&lt;A&gt; &amp; &lt;B&gt;" in kids[0]["note"]
    assert "<A>" not in kids[0]["note"]


def test_paragraphs_in_a_note_survive_as_paragraphs():
    kids = child_items(view(note="One.\n\nTwo."), parent_key=ITEM)
    assert kids[0]["note"] == "<p>One.</p><p>Two.</p>"


def test_no_children_when_there_is_nothing_to_put_in_them():
    assert child_items(view(note=None, drive_url=None), parent_key=ITEM) == []


def test_a_child_item_is_not_placed_in_a_collection():
    """Zotero does not allow a child item to belong to one."""
    for kid in child_items(view(), parent_key=ITEM):
        assert "collections" not in kid


# --- the library client -------------------------------------------------------


def library(handler):
    return ZoteroLibrary(
        api_key=KEY,
        user_id=USER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def write_handler(*, fail_children=False, body=None):
    posts = []

    def handler(request):
        assert request.url.path == f"/users/{USER}/items"
        assert request.headers["Zotero-API-Key"] == KEY
        assert request.headers["Zotero-API-Version"] == "3"
        # 32 hex characters, so a double tap cannot produce two items.
        assert len(request.headers["Zotero-Write-Token"]) == 32
        sent = json.loads(request.content)
        posts.append(sent)
        if len(posts) > 1:
            if fail_children:
                return httpx.Response(
                    200,
                    json={"successful": {}, "failed": {"0": {"message": "no notes"}}},
                )
            return httpx.Response(200, json={"successful": {"0": {"key": "CHILD123"}}})
        return httpx.Response(200, json=body or {"successful": {"0": {"key": ITEM}}})

    handler.posts = posts
    return handler


def test_save_creates_the_item_then_its_children():
    handler = write_handler()
    result = library(handler).save(view())

    assert result == {"item_key": ITEM, "warnings": []}
    # Two requests, because a child has to name a key Zotero only assigns on
    # the first.
    assert len(handler.posts) == 2
    assert handler.posts[0][0]["itemType"] == "journalArticle"
    assert [k["itemType"] for k in handler.posts[1]] == ["note", "attachment"]


def test_only_one_request_when_there_are_no_children():
    handler = write_handler()
    library(handler).save(view(note=None, drive_url=None))
    assert len(handler.posts) == 1


def test_a_failed_child_is_a_warning_not_a_lost_reference():
    """The item stands; losing it over a missing note would be worse."""
    handler = write_handler(fail_children=True)
    result = library(handler).save(view())
    assert result["item_key"] == ITEM
    assert "no notes" in result["warnings"][0]


def test_the_older_success_shape_is_also_read():
    """Zotero's docs use both `successful` and `success`."""
    handler = write_handler(body={"success": {"0": ITEM}, "failed": {}})
    assert library(handler).save(view(note=None, drive_url=None))["item_key"] == ITEM


def test_a_rejected_item_reports_zotero_s_own_reason():
    handler = lambda r: httpx.Response(
        200, json={"failed": {"0": {"code": 400, "message": "Invalid field 'repo'"}}}
    )
    with pytest.raises(ZoteroWriteError, match="Invalid field"):
        library(handler).save(view())


def test_a_refused_write_points_at_the_key_s_permissions():
    handler = lambda r: httpx.Response(403, text="Forbidden")
    with pytest.raises(ZoteroWriteError, match="write access"):
        library(handler).save(view())


def test_a_busy_zotero_is_not_reported_as_a_bad_item():
    handler = lambda r: httpx.Response(429, headers={"Retry-After": "12"})
    with pytest.raises(ZoteroWriteError) as exc:
        library(handler).save(view())
    assert "busy" in str(exc.value) and "12 seconds" in str(exc.value)


def test_a_redirected_write_is_not_followed():
    """The key rides in a custom header, which httpx would carry across one."""
    handler = lambda r: httpx.Response(302, headers={"location": "https://evil.test/"})
    with pytest.raises(ZoteroWriteError, match="redirected"):
        library(handler).save(view())


def test_no_write_error_ever_carries_the_key():
    for handler in [
        lambda r: httpx.Response(403, text="Forbidden"),
        lambda r: httpx.Response(500, text="boom"),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"successful": {}, "failed": {}}),
    ]:
        with pytest.raises(ZoteroWriteError) as exc:
            library(handler).save(view())
        assert KEY not in str(exc.value)


# --- the endpoint -------------------------------------------------------------


def seed(path, *, uploaded=True, zotero_key=None):
    """A ledger holding one accepted paper, filed and optionally in Drive."""
    with Ledger(path) as led:
        led.record(AlertPaper(
            title="A Quantum Abstract Machine",
            authors=["AM Hafiz"],
            venue="arXiv preprint arXiv:2607.13699",
            year=2026,
            url="https://arxiv.org/abs/2607.13699",
            alert_query="quantum computer architecture",
        ))
        key = led.pending()[0].key
        led.decide_by_key(key, Decision.ACCEPTED)
        led.set_destination(key, "fid", "Quantum")
        led.set_note(key, "Read the appendix.")
        if uploaded:
            led.set_uploaded(key, "drive-file-id")
        if zotero_key:
            led.set_zotero_item(key, zotero_key)
        return key


@pytest.fixture
def app(tmp_path):
    def make(handler=None, *, connected=True, write=True, notes=True, **seed_kw):
        auth = ZoteroAuth(
            client_path=tmp_path / "no-client.json",
            credential_path=tmp_path / "zotero.json",
        )
        if connected:
            access = {"library": True, "files": True}
            if write:
                access["write"] = True
            if notes:
                access["notes"] = True
            auth.credential_path.write_text(json.dumps({
                "api_key": KEY, "user_id": USER, "username": "snunez",
                "library_access": True, "write_access": write,
                "notes_access": notes, "linked_at": 0,
            }))
        ledger_path = tmp_path / "ledger.db"
        key = seed(ledger_path, **seed_kw)
        factory = None
        if handler is not None:
            factory = lambda cred: ZoteroLibrary(
                api_key=cred.api_key,
                user_id=cred.identity.user_id,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
        app = create_app(ledger_path, zotero=auth, zotero_factory=factory)
        return TestClient(app), key, ledger_path

    return make


def test_saving_a_filed_paper_records_the_item_on_the_ledger(app):
    handler = write_handler()
    client, key, ledger_path = app(handler)

    r = client.post(f"/api/papers/{key}/zotero")
    assert r.status_code == 200
    assert r.json() == {"already": False, "item_key": ITEM, "warnings": []}

    with Ledger(ledger_path) as led:
        paper = led.get(key)
    assert paper.zotero_item_key == ITEM
    assert paper_view(paper)["in_zotero"] is True

    # The arXiv venue became a preprint, with the note and the Drive link.
    assert handler.posts[0][0]["itemType"] == "preprint"
    assert handler.posts[0][0]["repository"] == "arXiv"
    assert [k["itemType"] for k in handler.posts[1]] == ["note", "attachment"]


def test_saving_twice_does_not_create_a_second_item(app):
    """Zotero's write token only dedupes for a minute, so this guard is ours."""
    handler = write_handler()
    client, key, _ = app(handler)

    client.post(f"/api/papers/{key}/zotero")
    r = client.post(f"/api/papers/{key}/zotero")
    assert r.json() == {"item_key": ITEM, "already": True, "warnings": []}
    # Still just the one item and its children.
    assert len(handler.posts) == 2


def test_a_paper_already_in_zotero_is_reported_without_a_write(app):
    client, key, _ = app(None, zotero_key="EXISTING1")
    r = client.post(f"/api/papers/{key}/zotero")
    assert r.json()["already"] is True
    assert r.json()["item_key"] == "EXISTING1"


def test_a_paper_not_yet_in_drive_is_refused_with_the_next_step(app):
    client, key, _ = app(None, uploaded=False)
    r = client.post(f"/api/papers/{key}/zotero")
    assert r.status_code == 409
    assert "Upload this paper to Drive first" in r.json()["detail"]


def test_saving_without_a_connected_account_says_where_to_connect(app):
    client, key, _ = app(None, connected=False)
    r = client.post(f"/api/papers/{key}/zotero")
    assert r.status_code == 503
    assert "Connect an account in Filing" in r.json()["detail"]


def test_a_key_that_cannot_write_is_caught_before_the_request(app):
    client, key, _ = app(None, write=False)
    r = client.post(f"/api/papers/{key}/zotero")
    assert r.status_code == 503
    assert "lacks write" in r.json()["detail"]


def test_a_key_that_cannot_write_notes_is_caught_too(app):
    client, key, _ = app(None, notes=False)
    r = client.post(f"/api/papers/{key}/zotero")
    assert r.status_code == 503
    assert "lacks notes" in r.json()["detail"]


def test_an_unknown_paper_is_a_404(app):
    client, _, _ = app(None)
    assert client.post("/api/papers/nope/zotero").status_code == 404


def test_a_zotero_failure_leaves_the_ledger_unmarked(app):
    """Otherwise the card would claim an item that was never created."""
    client, key, ledger_path = app(lambda r: httpx.Response(403, text="Forbidden"))
    r = client.post(f"/api/papers/{key}/zotero")
    assert r.status_code == 502
    assert "write access" in r.json()["detail"]
    with Ledger(ledger_path) as led:
        assert led.get(key).zotero_item_key is None


def test_the_capability_is_declared(app):
    client, _, _ = app(None)
    assert "zotero-save" in client.get("/api/version").json()["capabilities"]
