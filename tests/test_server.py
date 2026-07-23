import json

import pytest
from fastapi.testclient import TestClient

from paper_grabber.ledger import Decision, Ledger
from paper_grabber.models import AlertPaper, normalize_title
from paper_grabber.server import create_app
from paper_grabber.staging import StagingArea


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "state.db"


@pytest.fixture
def seeded(ledger_path):
    with Ledger(ledger_path) as led:
        led.record(
            AlertPaper(
                title="Quantum Error Correction on FPGAs",
                authors=["C AlSaneh", "C Mattar"],
                venue="Some Conference",
                year=2026,
                snippet="A short Scholar snippet.",
                url="https://arxiv.org/pdf/1234",
                alert_query="quantum computer architecture",
                has_pdf_badge=True,
            )
        )
        led.record(AlertPaper(title="Schizoanalysis: Politics and Subjectivity", year=2026))
    return ledger_path


@pytest.fixture
def client(seeded):
    return TestClient(create_app(seeded))


# --- page and PWA assets ------------------------------------------------------


def test_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "paper-grabber" in r.text


def test_index_has_no_external_resources(client):
    # A CDN reference would break on a flaky tablet connection and violate the
    # self-contained requirement.
    body = client.get("/").text
    for marker in ("http://", "https://cdn", "//unpkg", "//cdnjs"):
        assert marker not in body


def test_manifest_is_valid_json(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    manifest = r.json()
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["icons"]


def test_service_worker_is_javascript(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_service_worker_never_caches_api(client):
    # A cached triage list would re-offer papers already decided.
    assert "/api/" in client.get("/sw.js").text


# --- listing ------------------------------------------------------------------


def test_pending_lists_both_papers(client):
    data = client.get("/api/pending").json()
    assert len(data["papers"]) == 2
    assert data["counts"]["pending"] == 2


def test_paper_payload_has_what_the_card_needs(client):
    papers = client.get("/api/pending").json()["papers"]
    p = next(x for x in papers if "FPGA" in x["title"])
    assert p["authors"] == ["C AlSaneh", "C Mattar"]
    assert p["year"] == 2026
    assert p["venue"] == "Some Conference"
    assert p["url"] == "https://arxiv.org/pdf/1234"
    assert p["alert_query"] == "quantum computer architecture"


def test_snippet_is_used_as_abstract_but_flagged(client):
    # The user asked that a paper never be hidden for want of an abstract, but
    # a snippet must not masquerade as one.
    papers = client.get("/api/pending").json()["papers"]
    p = next(x for x in papers if "FPGA" in x["title"])
    assert p["abstract"] == "A short Scholar snippet."
    assert p["abstract_is_snippet"] is True


def test_paper_without_snippet_still_appears(client):
    papers = client.get("/api/pending").json()["papers"]
    p = next(x for x in papers if "Schizoanalysis" in x["title"])
    assert p["abstract"] is None  # the page renders a placeholder
    assert p["key"]


def test_get_single_paper(client, seeded):
    key = normalize_title("Quantum Error Correction on FPGAs")
    r = client.get(f"/api/papers/{key}")
    assert r.status_code == 200
    assert "FPGA" in r.json()["title"]


def test_unknown_paper_is_404(client):
    assert client.get("/api/papers/nope").status_code == 404


# --- deciding -----------------------------------------------------------------


def test_accepting_removes_from_pending(client, seeded):
    key = normalize_title("Quantum Error Correction on FPGAs")
    r = client.post(f"/api/papers/{key}/decision", json={"decision": "accepted"})
    assert r.status_code == 200
    assert r.json()["counts"]["accepted"] == 1

    remaining = client.get("/api/pending").json()["papers"]
    assert all("FPGA" not in p["title"] for p in remaining)


def test_rejecting_is_recorded(client, seeded):
    key = normalize_title("Schizoanalysis: Politics and Subjectivity")
    client.post(f"/api/papers/{key}/decision", json={"decision": "rejected"})
    with Ledger(seeded) as led:
        assert led.decision_for("Schizoanalysis: Politics and Subjectivity") is Decision.REJECTED


def test_decision_survives_a_new_request(client, seeded):
    # Each request opens its own SQLite connection; the write must be durable.
    key = normalize_title("Quantum Error Correction on FPGAs")
    client.post(f"/api/papers/{key}/decision", json={"decision": "accepted"})
    assert client.get("/api/pending").json()["counts"]["pending"] == 1


def test_undo_returns_a_paper_to_pending(client, seeded):
    # A mis-tap on a tablet is common; an accidental reject must be reversible.
    key = normalize_title("Quantum Error Correction on FPGAs")
    client.post(f"/api/papers/{key}/decision", json={"decision": "rejected"})
    client.post(f"/api/papers/{key}/decision", json={"decision": "pending"})
    titles = [p["title"] for p in client.get("/api/pending").json()["papers"]]
    assert any("FPGA" in t for t in titles)


def test_deciding_an_unknown_paper_is_404(client):
    r = client.post("/api/papers/nosuchkey/decision", json={"decision": "accepted"})
    assert r.status_code == 404


def test_invalid_decision_is_rejected(client, seeded):
    key = normalize_title("Quantum Error Correction on FPGAs")
    r = client.post(f"/api/papers/{key}/decision", json={"decision": "maybe"})
    assert r.status_code == 422


def test_empty_ledger_serves_an_empty_list(ledger_path):
    c = TestClient(create_app(ledger_path))
    data = c.get("/api/pending").json()
    assert data["papers"] == []


# --- enriched papers ----------------------------------------------------------


def test_real_abstract_supersedes_the_snippet(seeded):
    with Ledger(seeded) as led:
        key = normalize_title("Quantum Error Correction on FPGAs")
        led.attach_enrichment(key, {
            "abstract": "A genuine OpenAlex abstract.",
            "year": 2025,
            "doi": "10.1/abc",
            "pdf_url": "https://ex.org/a.pdf",
        })
    c = TestClient(create_app(seeded))
    p = next(x for x in c.get("/api/pending").json()["papers"] if "FPGA" in x["title"])
    assert p["abstract"] == "A genuine OpenAlex abstract."
    assert p["abstract_is_snippet"] is False
    assert p["doi"] == "10.1/abc"
    assert p["has_pdf"] is True


def test_enriched_year_supersedes_the_alert_year(seeded):
    with Ledger(seeded) as led:
        key = normalize_title("Schizoanalysis: Politics and Subjectivity")
        led.attach_enrichment(key, {"year": 2024})
    c = TestClient(create_app(seeded))
    p = next(x for x in c.get("/api/pending").json()["papers"] if "Schizo" in x["title"])
    assert p["year"] == 2024


# --- filing and folder browsing -----------------------------------------------


class FakeDrive:
    """Just enough Drive for the picker."""

    def __init__(self, tree=None, fail=False, missing=(), trashed=(), file_error=None):
        self.missing = set(missing)
        self.trashed = set(trashed)
        self.file_error = file_error
        self.descriptions = {}
        # {folder_id: (name, parent, [child_ids])}
        self.tree = tree or {
            "BASE": ("Papers", "ROOT", ["QUANTUM", "NETWORKS"]),
            "QUANTUM": ("Quantum", "BASE", ["ERRCORR"]),
            "NETWORKS": ("Networks", "BASE", []),
            "ERRCORR": ("Error Correction", "QUANTUM", []),
        }
        self.fail = fail
        self.created = []

    def list_child_folders(self, parent_id="root"):
        if self.fail:
            from paper_grabber.drive import DriveError
            raise DriveError("drive is down")
        _, _, children = self.tree.get(parent_id, ("", "", []))
        return [{"id": c, "name": self.tree[c][0]} for c in children]

    def breadcrumb(self, folder_id, stop_at=None):
        trail = []
        cur = folder_id
        while cur in self.tree:
            name, parent, _ = self.tree[cur]
            trail.append({"id": cur, "name": name})
            if stop_at and cur == stop_at:
                break
            cur = parent
        trail.reverse()
        return trail or [{"id": "root", "name": "My Drive"}]

    def file_status(self, file_id):
        if self.file_error:
            raise self.file_error
        if file_id in self.missing:
            return {"present": False, "trashed": False, "name": None}
        if file_id in self.trashed:
            return {"present": False, "trashed": True, "name": "gone.pdf"}
        return {"present": True, "trashed": False, "name": "a.pdf"}

    def set_description(self, file_id, description):
        self.descriptions[file_id] = description

    def create_folder(self, name, *, parent_id):
        new_id = f"NEW-{name}"
        self.tree[new_id] = (name, parent_id, [])
        self.tree[parent_id][2].append(new_id)
        self.created.append((name, parent_id))
        return {"id": new_id, "name": name}


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
def app_client(seeded, drive):
    return TestClient(create_app(seeded, drive_factory=lambda: drive))


def accept_all(client):
    for p in client.get("/api/pending").json()["papers"]:
        client.post(f"/api/papers/{p['key']}/decision", json={"decision": "accepted"})


def test_settings_start_empty(app_client):
    s = app_client.get("/api/settings").json()
    assert s["base_folder_id"] is None


def test_base_folder_can_be_set_and_persists(app_client):
    r = app_client.put("/api/settings/base-folder",
                       json={"folder_id": "BASE", "folder_name": "Papers"})
    assert r.status_code == 200
    assert app_client.get("/api/settings").json()["base_folder_id"] == "BASE"


def test_browsing_defaults_to_the_base_folder(app_client):
    app_client.put("/api/settings/base-folder",
                   json={"folder_id": "BASE", "folder_name": "Papers"})
    data = app_client.get("/api/drive/folders").json()
    assert data["parent"] == "BASE"
    assert {f["name"] for f in data["folders"]} == {"Quantum", "Networks"}


def test_browsing_descends(app_client):
    app_client.put("/api/settings/base-folder",
                   json={"folder_id": "BASE", "folder_name": "Papers"})
    data = app_client.get("/api/drive/folders", params={"parent": "QUANTUM"}).json()
    assert [f["name"] for f in data["folders"]] == ["Error Correction"]


def test_breadcrumb_stops_at_the_base(app_client):
    # Navigating above the configured base would let papers be filed anywhere.
    app_client.put("/api/settings/base-folder",
                   json={"folder_id": "BASE", "folder_name": "Papers"})
    data = app_client.get("/api/drive/folders", params={"parent": "ERRCORR"}).json()
    assert [c["name"] for c in data["breadcrumb"]] == ["Papers", "Quantum", "Error Correction"]


def test_new_folder_is_created_in_the_right_parent(app_client, drive):
    r = app_client.post("/api/drive/folders", json={"name": "Photonics", "parent_id": "BASE"})
    assert r.status_code == 200
    assert drive.created == [("Photonics", "BASE")]


def test_blank_folder_name_is_refused(app_client):
    assert app_client.post("/api/drive/folders",
                           json={"name": "   ", "parent_id": "BASE"}).status_code == 400


def test_drive_failure_surfaces_as_502(seeded):
    c = TestClient(create_app(seeded, drive_factory=lambda: FakeDrive(fail=True)))
    assert c.get("/api/drive/folders").status_code == 502


def test_accepted_papers_start_unfiled(app_client):
    accept_all(app_client)
    data = app_client.get("/api/accepted").json()
    assert len(data["unfiled"]) == 2
    assert data["filed"] == []


def test_assigning_a_destination_in_bulk(app_client):
    accept_all(app_client)
    keys = [p["key"] for p in app_client.get("/api/accepted").json()["unfiled"]]
    r = app_client.post("/api/destination",
                        json={"keys": keys, "folder_id": "QUANTUM", "folder_name": "Quantum"})
    assert r.status_code == 200
    assert len(r.json()["updated"]) == 2

    data = app_client.get("/api/accepted").json()
    assert data["unfiled"] == []
    assert {p["dest_folder_name"] for p in data["filed"]} == {"Quantum"}


def test_destination_can_be_reassigned(app_client):
    accept_all(app_client)
    keys = [p["key"] for p in app_client.get("/api/accepted").json()["unfiled"]]
    app_client.post("/api/destination",
                    json={"keys": keys[:1], "folder_id": "QUANTUM", "folder_name": "Quantum"})
    app_client.post("/api/destination",
                    json={"keys": keys[:1], "folder_id": "NETWORKS", "folder_name": "Networks"})
    filed = app_client.get("/api/accepted").json()["filed"]
    assert filed[0]["dest_folder_name"] == "Networks"


def test_empty_key_list_is_refused(app_client):
    r = app_client.post("/api/destination",
                        json={"keys": [], "folder_id": "Q", "folder_name": "Q"})
    assert r.status_code == 400


def test_unknown_keys_are_404(app_client):
    r = app_client.post("/api/destination",
                        json={"keys": ["nope"], "folder_id": "Q", "folder_name": "Q"})
    assert r.status_code == 404


def test_triage_works_without_drive_credentials(seeded):
    # The triage half must not require Google auth at all.
    c = TestClient(create_app(seeded))
    assert c.get("/api/pending").status_code == 200
    assert c.get("/api/accepted").status_code == 200


# --- naming and counts --------------------------------------------------------


def test_page_is_titled_research_stream(client):
    body = client.get("/").text
    assert "<title>Research Stream</title>" in body
    assert "<h1>Research Stream</h1>" in body


def test_manifest_names_the_app_research_stream(client):
    m = client.get("/manifest.webmanifest").json()
    assert m["name"] == "Research Stream"
    assert m["short_name"] == "Research Stream"


def test_counts_include_filed(client):
    c = client.get("/api/pending").json()["counts"]
    assert c["filed"] == 0


def test_filed_count_rises_when_a_destination_is_chosen(app_client):
    accept_all(app_client)
    keys = [p["key"] for p in app_client.get("/api/accepted").json()["unfiled"]]
    app_client.post("/api/destination",
                    json={"keys": keys[:1], "folder_id": "QUANTUM", "folder_name": "Quantum"})
    assert app_client.get("/api/pending").json()["counts"]["filed"] == 1


def test_counts_line_renders_all_four(client):
    # The header shows pending, accepted, rejected, then filed.
    body = client.get("/").text
    for word in ("pending", "accepted", "rejected", "filed"):
        assert word in body


# --- version and capability negotiation ---------------------------------------


def test_version_lists_capabilities(client):
    v = client.get("/api/version").json()
    assert "upload" in v["capabilities"]
    assert "unfile" in v["capabilities"]
    assert "refresh" in v["capabilities"]


def test_every_advertised_capability_has_a_route(client):
    # A capability the server claims but cannot serve would be worse than not
    # advertising it at all.
    v = client.get("/api/version").json()
    paths = {"upload": "/api/upload", "refresh": "/api/refresh"}
    for name, path in paths.items():
        assert name in v["capabilities"]
        assert client.get(path).status_code == 200


def test_page_checks_the_server_version(client):
    body = client.get("/").text
    assert "checkServerVersion" in body
    assert "older code" in body


# --- processed tab ------------------------------------------------------------


def upload_one(seeded, key, folder="Quantum"):
    with Ledger(seeded) as led:
        led.set_destination(key, "F1", folder)
        led.set_uploaded(key, "DRIVE-ABC")


def test_processed_starts_empty(app_client):
    assert app_client.get("/api/processed").json()["papers"] == []


def test_uploaded_paper_appears_in_processed(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    upload_one(seeded, key)
    papers = app_client.get("/api/processed").json()["papers"]
    assert len(papers) == 1
    assert papers[0]["uploaded"] is True


def test_uploaded_paper_leaves_the_filing_lists(app_client, seeded):
    # The whole point of the separation: "ready to upload" must not include
    # what is already uploaded.
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    upload_one(seeded, key)
    acc = app_client.get("/api/accepted").json()
    assert all(p["key"] != key for p in acc["filed"])
    assert all(p["key"] != key for p in acc["unfiled"])


def test_processed_paper_has_a_drive_link(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    upload_one(seeded, key)
    p = app_client.get("/api/processed").json()["papers"][0]
    assert p["drive_url"] == "https://drive.google.com/file/d/DRIVE-ABC/view"
    assert p["dest_folder_name"] == "Quantum"


def test_counts_separate_filed_from_processed(app_client, seeded):
    accept_all(app_client)
    keys = [p["key"] for p in app_client.get("/api/accepted").json()["unfiled"]]
    app_client.post("/api/destination",
                    json={"keys": [keys[0]], "folder_id": "F1", "folder_name": "Quantum"})
    upload_one(seeded, keys[1])
    counts = app_client.get("/api/processed").json()["counts"]
    assert counts["filed"] == 1
    assert counts["processed"] == 1


def test_page_has_the_processed_tab(app_client):
    body = app_client.get("/").text
    assert 'id="tab-processed"' in body
    assert 'id="view-processed"' in body


def test_processed_is_an_advertised_capability(app_client):
    assert "processed" in app_client.get("/api/version").json()["capabilities"]


# --- verifying a processed paper ----------------------------------------------


def processed_key(app_client, seeded, drive_id="DRIVE-ABC"):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    with Ledger(seeded) as led:
        led.set_destination(key, "F1", "Quantum")
        led.set_uploaded(key, drive_id)
    return key


def test_verify_reports_a_file_still_present(app_client, seeded):
    key = processed_key(app_client, seeded)
    r = app_client.post(f"/api/papers/{key}/verify")
    assert r.status_code == 200
    assert r.json()["present"] is True
    # Still processed, not moved.
    assert len(app_client.get("/api/processed").json()["papers"]) == 1


def test_missing_file_returns_the_paper_to_filing(seeded):
    drive = FakeDrive(missing=["DRIVE-GONE"])
    c = TestClient(create_app(seeded, drive_factory=lambda: drive))
    key = processed_key(c, seeded, drive_id="DRIVE-GONE")

    r = c.post(f"/api/papers/{key}/verify")
    assert r.json()["present"] is False
    assert "returned to Filing" in r.json()["detail"]
    assert c.get("/api/processed").json()["papers"] == []
    # Back in the queue with its destination intact, ready to re-upload.
    filed = c.get("/api/accepted").json()["filed"]
    assert [p["key"] for p in filed] == [key]
    assert filed[0]["dest_folder_name"] == "Quantum"


def test_trashed_file_counts_as_gone(seeded):
    drive = FakeDrive(trashed=["DRIVE-BIN"])
    c = TestClient(create_app(seeded, drive_factory=lambda: drive))
    key = processed_key(c, seeded, drive_id="DRIVE-BIN")
    r = c.post(f"/api/papers/{key}/verify")
    assert r.json()["present"] is False
    assert r.json()["trashed"] is True
    assert "bin" in r.json()["detail"]


def test_a_drive_failure_changes_nothing(seeded):
    # The safety property: an unreachable Drive must not be read as deletion.
    from paper_grabber.drive import DriveError

    drive = FakeDrive(file_error=DriveError("network down"))
    c = TestClient(create_app(seeded, drive_factory=lambda: drive))
    key = processed_key(c, seeded)

    assert c.post(f"/api/papers/{key}/verify").status_code == 502
    assert len(c.get("/api/processed").json()["papers"]) == 1
    assert c.get("/api/accepted").json()["filed"] == []


def test_verify_refuses_a_paper_never_uploaded(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    assert app_client.post(f"/api/papers/{key}/verify").status_code == 409


def test_verify_unknown_paper_is_404(app_client):
    assert app_client.post("/api/papers/nope/verify").status_code == 404


def test_counts_move_from_processed_to_filed(seeded):
    drive = FakeDrive(missing=["DRIVE-GONE"])
    c = TestClient(create_app(seeded, drive_factory=lambda: drive))
    key = processed_key(c, seeded, drive_id="DRIVE-GONE")
    counts = c.post(f"/api/papers/{key}/verify").json()["counts"]
    assert counts["processed"] == 0
    assert counts["filed"] == 1


def test_processed_cards_have_a_check_button(app_client):
    assert "iconBtn('verify'" in app_client.get("/").text


def test_drive_only_token_does_not_use_gmail(tmp_path, monkeypatch):
    # A Drive-only token handed to the Gmail API fails with "insufficient
    # authentication scopes" only when a request is made -- long after the UI
    # has claimed everything is fine.
    import json

    from paper_grabber.oauth_web import WebOAuth

    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "token": "at", "refresh_token": "rt",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "cs",
        "scopes": ["https://www.googleapis.com/auth/drive.file"],
    }))
    oauth = WebOAuth(credentials_path=tmp_path / "none.json", token_path=token)

    monkeypatch.setenv("PAPER_GRABBER_IMAP_USER", "me@example.com")
    monkeypatch.setenv("PAPER_GRABBER_IMAP_PASSWORD", "app-password")

    app = create_app(tmp_path / "s.db", oauth=oauth)
    c = TestClient(app)
    status = c.get("/api/auth/status").json()
    assert status["needs_reauth"] is True
    assert status["has_gmail"] is False


# --- alert filter -------------------------------------------------------------


def test_pending_payload_carries_the_alert_query(client):
    # The filter is client-side, so it depends entirely on this field being
    # present on every paper.
    papers = client.get("/api/pending").json()["papers"]
    assert all("alert_query" in p for p in papers)


def test_page_has_the_filter_sidebar(client):
    body = client.get("/").text
    assert 'id="sidebar"' in body
    assert 'id="alertoptions"' in body
    assert "renderAlertFilter" in body


def test_sidebar_has_a_toggle_for_narrow_screens(client):
    # The Tab S10 in portrait has no width for a permanent second column.
    body = client.get("/").text
    assert 'id="filtertoggle"' in body
    assert 'aria-controls="sidebar"' in body


def test_alert_group_is_hidden_for_a_single_alert(client):
    # One checkbox is not a choice.
    assert "counts.size < 2" in client.get("/").text


def test_alert_selection_is_multi_select(client):
    body = client.get("/").text
    assert "box.type = 'checkbox'" in body
    assert "state.alerts" in body


def test_all_button_selects_every_alert(client):
    # It used to clear the selection and rely on "empty means everything",
    # which left every box unticked and looked like it had done nothing. Now it
    # ticks the alerts visible in the current view (other tabs' stay put).
    body = client.get("/").text
    assert "for (const name of alertCounts().keys()) state.alerts.add(name);" in body


def test_none_button_clears_the_selection(client):
    assert "for (const name of alertCounts().keys()) state.alerts.delete(name);" \
        in client.get("/").text


def test_a_ticked_box_means_the_alert_is_shown(client):
    # Literal semantics: no branch where an empty selection shows everything.
    body = client.get("/").text
    assert "if (!state.alerts.size) return papers;" not in body


def test_new_alerts_arrive_selected(client):
    # Otherwise a newly configured Scholar alert would show up invisible.
    body = client.get("/").text
    assert "state.knownAlerts" in body


def test_empty_selection_explains_itself(client):
    assert "No alerts selected" in client.get("/").text


def test_papers_from_several_alerts_are_distinguishable(seeded):
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="From Alert Two", year=2026,
                              alert_query="quantum programming language"))
    c = TestClient(create_app(seeded))
    queries = {p["alert_query"] for p in c.get("/api/pending").json()["papers"]}
    assert "quantum programming language" in queries
    assert len(queries) >= 2


# --- per-card links -----------------------------------------------------------


def test_card_exposes_three_distinct_links(seeded):
    with Ledger(seeded) as led:
        key = normalize_title("Quantum Error Correction on FPGAs")
        led.attach_enrichment(key, {
            "doi": "10.1145/12345",
            "pdf_url": "https://arxiv.org/pdf/2607.1",
            "pdf_candidates": ["https://arxiv.org/pdf/2607.1"],
            "landing_url": "https://dl.acm.org/doi/10.1145/12345",
        })
    c = TestClient(create_app(seeded))
    p = next(x for x in c.get("/api/pending").json()["papers"] if "FPGA" in x["title"])
    assert p["pdf_url"] == "https://arxiv.org/pdf/2607.1"
    assert p["doi_url"] == "https://doi.org/10.1145/12345"
    assert p["source_url"] == "https://dl.acm.org/doi/10.1145/12345"


def test_doi_becomes_a_resolvable_link(seeded):
    with Ledger(seeded) as led:
        led.attach_enrichment(normalize_title("Quantum Error Correction on FPGAs"),
                              {"doi": "10.1016/j.imavis.2026.106120"})
    c = TestClient(create_app(seeded))
    p = next(x for x in c.get("/api/pending").json()["papers"] if "FPGA" in x["title"])
    assert p["doi_url"] == "https://doi.org/10.1016/j.imavis.2026.106120"


def test_paper_without_a_doi_has_no_doi_link(client):
    papers = client.get("/api/pending").json()["papers"]
    assert all(p["doi_url"] is None for p in papers)


def test_publisher_link_falls_back_to_the_scholar_url(client):
    # Only 3 of 67 real papers had a landing_url; the Scholar link is what
    # makes this useful for the rest.
    p = next(x for x in client.get("/api/pending").json()["papers"] if "FPGA" in x["title"])
    assert p["source_url"] == "https://arxiv.org/pdf/1234"


def test_page_renders_the_link_row(client):
    body = client.get("/").text
    assert "cardLinks" in body
    for label in ("'PDF'", "'DOI'", "'Publisher'"):
        assert label in body


def test_links_open_in_a_new_tab_safely(client):
    # target=_blank without noopener hands the opener to the destination.
    body = client.get("/").text
    assert 'target="_blank" rel="noopener"' in body


def test_card_carries_a_venue_label(client):
    p = next(x for x in client.get("/api/pending").json()["papers"] if "FPGA" in x["title"])
    assert p["source_label"] == "Some Conference"


def test_venue_label_falls_back_to_host(seeded):
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="No Venue Paper", year=2026,
                              url="https://dl.acm.org/doi/abs/10.1/x"))
    c = TestClient(create_app(seeded))
    p = next(x for x in c.get("/api/pending").json()["papers"] if "No Venue" in x["title"])
    assert p["source_label"] == "dl.acm.org"


def test_page_labels_the_link_with_the_venue(client):
    assert "p.source_label" in client.get("/").text


def test_sidebar_sticks_at_the_measured_header_height(client):
    # A hardcoded offset makes the sidebar drift upward on the first scroll,
    # because the header's height varies with the sign-in bar and wrapping.
    body = client.get("/").text
    assert "top: var(--header-h" in body
    assert "syncHeaderHeight" in body
    assert "ResizeObserver" in body


def test_sidebar_can_be_collapsed(client):
    body = client.get("/").text
    assert 'id="sidebarcollapse"' in body
    assert 'id="sidebarexpand"' in body
    assert "sidebar-collapsed" in body


def test_collapse_arrow_has_its_own_row(client):
    # Absolutely positioning it over the first group put it on top of that
    # group's disclosure arrow.
    body = client.get("/").text
    assert '<div id="sidebarhead">' in body
    assert "position: absolute; top: .55rem; right: .35rem;" not in body


def test_collapse_arrow_stays_put_while_the_sidebar_scrolls(client):
    body = client.get("/").text
    assert "#sidebarhead {\n    position: sticky; top: 0;" in body


def test_only_one_collapse_control_on_wide_screens(client):
    # The header button is the drawer handle; wide layouts use the arrows.
    assert "#filtertoggle { display: none; }" in client.get("/").text


def test_collapsed_sidebar_leaves_the_flow(client):
    # Shrinking it to a stub would still cost width; display:none gives it back.
    assert "body.sidebar-collapsed #sidebar { display: none; }" in client.get("/").text


def test_collapse_choice_is_remembered(client):
    body = client.get("/").text
    assert "rs.sidebar.collapsed" in body
    assert "localStorage" in body


def test_collapse_state_is_not_restored_into_the_drawer(client):
    # A drawer that opened itself on load would cover the papers.
    assert "body.sidebar-collapsed #sidebar { display: block; }" in client.get("/").text


# --- citations ----------------------------------------------------------------


def test_bibtex_endpoint_returns_an_entry(client, seeded):
    key = normalize_title("Quantum Error Correction on FPGAs")
    r = client.get(f"/api/papers/{key}/bibtex")
    assert r.status_code == 200
    entry = r.json()["bibtex"]
    assert entry.startswith("@")
    assert "Quantum Error Correction on FPGAs" in entry


def test_bibtex_for_an_unknown_paper_is_404(client):
    assert client.get("/api/papers/nope/bibtex").status_code == 404


def test_cite_button_is_on_every_filing_and_processed_card(client):
    body = client.get("/").text
    filing = body.split("function filingCard")[1].split("\nasync function")[0]
    selectable = filing.split("if (selectable) {")[1].split("} else {")[0]
    ready = filing.split("} else {")[1]
    processed = body.split("function processedCard")[1].split("\nasync function")[0]

    # A card awaiting a destination still deserves a citation: wanting the
    # reference has nothing to do with having decided where the PDF goes.
    assert "iconBtn('cite'" in selectable
    assert "iconBtn('cite'" in ready
    assert "iconBtn('cite'" in processed


def test_triage_cards_have_no_cite_button(client):
    body = client.get("/").text
    triage = body.split("function triageCard")[1].split("\nasync function")[0]
    assert "cite" not in triage


def test_copy_falls_back_outside_a_secure_context(client):
    # The tablet reaches this app over plain HTTP, where navigator.clipboard
    # does not exist.
    body = client.get("/").text
    assert "isSecureContext" in body
    assert "execCommand('copy')" in body


def test_citation_opens_in_the_side_panel(client):
    # Same drawer as notes, in a citation mode.
    body = client.get("/").text
    assert 'id="np-cite"' in body
    assert "openPanel('cite'" in body


def test_citation_is_shown_as_well_as_copied(client):
    # Where the clipboard is unavailable -- a plain-HTTP LAN address is not a
    # secure context -- the entry must still be on screen to take by hand.
    body = client.get("/").text
    assert "Copying is blocked here" in body
    assert 'id="citecopy"' in body


def test_the_panel_serves_both_notes_and_citations(client):
    body = client.get("/").text
    assert "$('#np-note').hidden = mode !== 'note';" in body
    assert "$('#np-cite').hidden = mode !== 'cite';" in body


def test_cite_is_pinned_to_the_right_of_the_action_row(client):
    body = client.get("/").text
    rule = body.split(".filed-actions .cite")[1].split("}")[0]
    assert "margin-left: auto" in rule


def test_cite_comes_last_in_every_action_row(client):
    # margin-left:auto only pushes it right if nothing follows it.
    body = client.get("/").text
    for name in ("filingCard", "processedCard"):
        block = body.split(f"function {name}")[1].split("\nasync function")[0]
        for seg in block.split("innerHTML"):
            if "iconBtn('cite'" in seg and "cardstatus" in seg:
                assert seg.index("iconBtn('cite'") > seg.index("cardstatus")


def test_note_sits_immediately_before_cite(client):
    body = client.get("/").text
    filing = body.split("function filingCard")[1].split("\nasync function")[0]
    rows = [seg for seg in filing.split("innerHTML") if "iconBtn('note" in seg]
    assert rows, "no filing row offers a note"
    for seg in rows:
        # Adjacent and both to the right of the status, so they read as a pair.
        assert seg.index("iconBtn('note") < seg.index("iconBtn('cite'")
        assert seg.index("cardstatus") < seg.index("iconBtn('note")


def test_note_and_cite_are_pushed_right_as_a_pair(client):
    body = client.get("/").text
    assert ".filed-actions .note + .cite { margin-left: 0; }" in body


# --- search -------------------------------------------------------------------


def test_page_has_a_search_box(client):
    body = client.get("/").text
    assert 'id="q"' in body
    assert 'id="qclear"' in body


def test_search_covers_the_fields_worth_searching(client):
    body = client.get("/").text
    hay = body.split("function searchHaystack")[1].split("}")[0]
    for field in ("p.title", "p.authors", "p.venue", "p.abstract", "p.doi"):
        assert field in hay


def test_search_is_accent_insensitive(client):
    # "Schrodinger" must find "Schrödinger".
    body = client.get("/").text
    assert "normalize('NFKD')" in body
    assert "\\u0300-\\u036f" in body


def test_search_requires_every_term(client):
    # More words should narrow, not widen.
    body = client.get("/").text
    assert "terms.every(t => hay.includes(t))" in body


def test_search_and_alert_filter_compose(client):
    # Search composes on top of the alert selection, in one shared path that
    # every list view runs through.
    body = client.get("/").text
    assert "return byAlert(papers).filter(p => matchesQuery(p, terms));" in body


def test_empty_search_result_names_the_query(client):
    assert 'Nothing matches "${state.query}".' in client.get("/").text


# --- bulk rejection from Filing -----------------------------------------------


def test_bulk_reject_removes_papers_from_filing(app_client):
    accept_all(app_client)
    keys = [p["key"] for p in app_client.get("/api/accepted").json()["unfiled"]]
    r = app_client.post("/api/decisions", json={"keys": keys, "decision": "rejected"})
    assert r.status_code == 200
    assert len(r.json()["updated"]) == len(keys)

    acc = app_client.get("/api/accepted").json()
    assert acc["unfiled"] == [] and acc["filed"] == []


def test_bulk_reject_is_permanent_for_future_syncs(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    app_client.post("/api/decisions", json={"keys": [key], "decision": "rejected"})
    with Ledger(seeded) as led:
        paper = led.get(key)
        assert paper.decision is Decision.REJECTED
        # Re-recording must not resurrect it on the next check.
        assert led.record(AlertPaper(title=paper.title)) is False


def test_bulk_reject_can_be_undone(app_client):
    accept_all(app_client)
    keys = [p["key"] for p in app_client.get("/api/accepted").json()["unfiled"]]
    app_client.post("/api/decisions", json={"keys": keys, "decision": "rejected"})
    app_client.post("/api/decisions", json={"keys": keys, "decision": "accepted"})
    assert len(app_client.get("/api/accepted").json()["unfiled"]) == len(keys)


def test_bulk_decision_with_no_keys_is_refused(app_client):
    r = app_client.post("/api/decisions", json={"keys": [], "decision": "rejected"})
    assert r.status_code == 400


def test_bulk_decision_with_unknown_keys_is_404(app_client):
    r = app_client.post("/api/decisions", json={"keys": ["nope"], "decision": "rejected"})
    assert r.status_code == 404


def test_bulk_decision_rejects_an_invalid_value(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    r = app_client.post("/api/decisions", json={"keys": [key], "decision": "maybe"})
    assert r.status_code == 422


def test_reject_button_sits_between_select_all_and_file(client):
    import re

    bar = client.get("/").text.split('<div id="filebar"')[1].split("</div>")[0]
    assert re.findall(r'id="(selall|selreject|filesel)"', bar) == [
        "selall", "selreject", "filesel"]


def test_reject_button_uses_the_palette_reject_colour(client):
    body = client.get("/").text
    rule = body.split("#filebar button.danger")[1].split("}")[0]
    assert "var(--reject)" in rule


def test_bulk_rejection_offers_an_undo(client):
    # Dropped papers are suppressed on every future check; a mis-tap here
    # would bury them silently.
    body = client.get("/").text
    assert "pushUndo({scope: 'filing', keys: data.updated});" in body


def test_undo_is_a_multi_level_stack(client):
    # Swiping is fast, so a rushed run of decisions must walk back one at a
    # time, not just the last -- a bounded LIFO stack, scoped to the tab.
    body = client.get("/").text
    assert "function pushUndo" in body
    assert "function popUndo" in body
    assert "const UNDO_MAX = 25;" in body
    # A single triage decision pushes onto the stack, not a lone slot.
    assert "pushUndo({scope: 'triage', paper});" in body
    # The pill shows how many steps are undoable on this tab.
    assert "n > 1 ? `Undo (${n})` : 'Undo'" in body


# --- rejected view and recovery -----------------------------------------------


def test_rejected_endpoint_lists_rejected_papers(app_client, seeded):
    accept_all(app_client)
    keys = [p["key"] for p in app_client.get("/api/accepted").json()["unfiled"]]
    app_client.post("/api/decisions", json={"keys": keys, "decision": "rejected"})
    papers = app_client.get("/api/rejected").json()["papers"]
    assert len(papers) == len(keys)


def test_rejected_starts_empty(app_client):
    assert app_client.get("/api/rejected").json()["papers"] == []


def test_recovering_returns_a_paper_to_filing(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    app_client.post("/api/decisions", json={"keys": [key], "decision": "rejected"})
    assert any(p["key"] == key for p in app_client.get("/api/rejected").json()["papers"])

    # Recovery goes to accepted, not pending: it was judged interesting once.
    app_client.post("/api/decisions", json={"keys": [key], "decision": "accepted"})
    assert all(p["key"] != key for p in app_client.get("/api/rejected").json()["papers"])
    assert any(p["key"] == key
               for p in app_client.get("/api/accepted").json()["unfiled"])


def test_recovered_paper_does_not_return_to_triage(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    app_client.post("/api/decisions", json={"keys": [key], "decision": "rejected"})
    app_client.post("/api/decisions", json={"keys": [key], "decision": "accepted"})
    assert all(p["key"] != key for p in app_client.get("/api/pending").json()["papers"])


def test_rejected_button_sits_left_of_check_now(client):
    import re

    bar = client.get("/").text.split('<div class="titlebar">')[1].split("</div>")[0]
    assert re.findall(r'id="(rejectedbtn|check)"', bar) == ["rejectedbtn", "check"]


def test_rejected_button_uses_the_palette_reject_colour(client):
    rule = client.get("/").text.split("#rejectedbtn {")[1].split("}")[0]
    assert "var(--reject)" in rule


def test_rejected_view_and_recover_button_exist(client):
    body = client.get("/").text
    assert 'id="view-rejected"' in body
    assert "iconBtn('recover'" in body
    assert "recoverOne" in body


def test_rejected_is_not_a_tab(client):
    # It is reached from the header, so no tab should be selected while it
    # is showing.
    body = client.get("/").text
    assert "$('#view-rejected').hidden = tab !== 'rejected';" in body


def test_rejected_button_has_no_count(client):
    body = client.get("/").text
    bar = body.split('<div class="titlebar">')[1].split("</div>")[0]
    assert "badge-rejected" not in bar
    # The label lives in aria-label and the tooltip now that it is an icon.
    assert 'aria-label="Dropped"' in bar


# --- notes --------------------------------------------------------------------


def test_note_is_saved_and_returned(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    r = app_client.put(f"/api/papers/{key}/note", json={"note": "Read section 4."})
    assert r.status_code == 200
    assert r.json()["note"] == "Read section 4."

    p = next(x for x in app_client.get("/api/accepted").json()["unfiled"]
             if x["key"] == key)
    assert p["note"] == "Read section 4."


def test_note_is_trimmed_and_emptied(app_client, seeded):
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    app_client.put(f"/api/papers/{key}/note", json={"note": "  spaced  "})
    assert app_client.get("/api/accepted").json()["unfiled"][0]["note"] == "spaced"
    app_client.put(f"/api/papers/{key}/note", json={"note": "   "})
    assert app_client.get("/api/accepted").json()["unfiled"][0]["note"] is None


def test_note_on_an_uploaded_paper_syncs_to_drive(app_client, seeded, drive):
    # Reading happens after filing, which is exactly when there is something
    # worth writing down -- so the edit is allowed and follows the file.
    accept_all(app_client)
    key = app_client.get("/api/accepted").json()["unfiled"][0]["key"]
    with Ledger(seeded) as led:
        led.set_destination(key, "F1", "Quantum")
        led.set_uploaded(key, "DRIVE1")

    r = app_client.put(f"/api/papers/{key}/note", json={"note": "worth rereading"})
    assert r.status_code == 200
    assert r.json()["synced_to_drive"] is True
    assert drive.descriptions["DRIVE1"] == "worth rereading"


def test_a_drive_failure_does_not_lose_the_note(seeded):
    # The note is already in the ledger; failing the request would imply
    # nothing was saved.
    class BrokenDrive(FakeDrive):
        def set_description(self, file_id, description):
            raise RuntimeError("drive down")

    c = TestClient(create_app(seeded, drive_factory=lambda: BrokenDrive()))
    accept_all(c)
    key = c.get("/api/accepted").json()["unfiled"][0]["key"]
    with Ledger(seeded) as led:
        led.set_uploaded(key, "DRIVE1")

    r = c.put(f"/api/papers/{key}/note", json={"note": "kept anyway"})
    assert r.status_code == 200
    assert r.json()["synced_to_drive"] is False
    with Ledger(seeded) as led:
        assert led.get(key).note == "kept anyway"


def test_note_for_an_unknown_paper_is_404(app_client):
    assert app_client.put("/api/papers/nope/note", json={"note": "x"}).status_code == 404


def test_note_button_is_on_filing_cards(client):
    body = client.get("/").text
    filing = body.split("function filingCard")[1].split("\nasync function")[0]
    assert "iconBtn('note" in filing
    assert "editNote" in body


def test_note_is_shown_on_the_card(client):
    assert 'class="note-text"' in client.get("/").text


def test_note_editor_says_when_it_reaches_drive(client):
    body = client.get("/").text
    assert "description in Drive when it is uploaded" in body


def test_note_editor_is_a_right_hand_drawer(client):
    # The card being annotated stays visible while the note is written.
    body = client.get("/").text
    assert 'id="notepanel"' in body
    rule = body.split("#notepanel {")[1].split("}")[0]
    assert "right: 0" in rule


def test_note_drawer_collapses_after_saving(client):
    body = client.get("/").text
    save = body.split("$('#notesave').addEventListener")[1].split("document.addEventListener")[0]
    assert "closeNote();" in save
    assert "Collapses on save" in save


def test_tooltip_delay_is_one_second(client):
    assert "TIP_DELAY_MS = 1000" in client.get("/").text


def test_icons_are_inline_and_monochrome(client):
    body = client.get("/").text
    assert 'stroke="currentColor"' in body
    assert "ICONS = {" in body


def test_icon_buttons_keep_an_accessible_name(client):
    # Icon-only buttons have no visible text, so the name must come from
    # aria-label, and the same words drive the tooltip.
    body = client.get("/").text
    assert 'aria-label="${esc(label)}"' in body
    assert 'data-tip="${esc(label)}"' in body


def test_touch_gets_the_labels_too(client):
    # A tablet has no hover; without a long press the labels are unreachable
    # on the device this app is built for.
    body = client.get("/").text
    assert "TIP_HOLD_MS" in body
    assert "touchstart" in body


def test_check_now_is_green_like_upload(client):
    rule = client.get("/").text.split("#check {")[1].split("}")[0]
    assert "var(--accept)" in rule


def test_interesting_uses_a_tick(client):
    assert "iconBtn('yes', 'check'" in client.get("/").text


def test_card_icon_buttons_share_one_size(client):
    # Per-button padding overrides had made Note narrower than Cite, Check and
    # Recover, so a row of icons read as mismatched.
    import re

    body = client.get("/").text
    assert ".filed-actions .iconbtn { flex: 0 0 auto; min-width: 44px; padding: 0; }" in body
    assert not re.findall(r"\.filed-actions \.(cite|verify|recover|note) \{[^}]*padding", body)


def test_only_the_trailing_pair_is_right_aligned(client):
    body = client.get("/").text
    # Note takes the auto margin when present, Cite when it is alone.
    assert ".filed-actions .note,\n  .filed-actions .cite { margin-left: auto; }" in body
    assert ".filed-actions .note ~ .cite { margin-left: 0; }" in body
    # The primary actions stay at the left.
    for name in (".up", ".unfile", ".suggest", ".verify"):
        rule_start = body.find(f".filed-actions {name} {{")
        if rule_start != -1:
            assert "margin-left: auto" not in body[rule_start:body.index("}", rule_start)]


def test_public_bind_is_refused_without_the_flag():
    from paper_grabber.cli import _is_public_bind
    # Loopback and Tailscale's CGNAT range are safe; 0.0.0.0 and a public IP
    # would expose an unauthenticated API.
    assert _is_public_bind("127.0.0.1") is False
    assert _is_public_bind("100.101.102.103") is False   # tailnet
    assert _is_public_bind("192.168.1.5") is False        # home LAN
    assert _is_public_bind("0.0.0.0") is True
    assert _is_public_bind("49.12.200.10") is True        # a Hetzner IP


# --- in-app reader ------------------------------------------------------------


def test_reader_virtualizes_pages_with_eviction(client):
    # Canvas rendering only runs in a browser, so guard the wiring: pages are
    # rendered near the viewport and released once far, keeping a long PDF's
    # memory bounded to the window on screen rather than its page count.
    body = client.get("/").text
    assert "renderObs = new IntersectionObserver" in body
    assert "keepObs = new IntersectionObserver" in body
    assert "function evictPage" in body
    # A released canvas hands its bytes back immediately.
    assert "canvas.width = canvas.height = 0;" in body


PDF_BYTES = b"%PDF-1.7\nreader test\n"


def test_pdf_is_served_from_staging_when_local(seeded, tmp_path):
    staging = tmp_path / "staging"
    area = StagingArea(staging)
    area.stage("2026 A Paper.pdf", PDF_BYTES)
    with Ledger(seeded) as led:
        key = led.pending()[0].key
        led.decide_by_key(key, Decision.ACCEPTED)
        led.set_staged(key, "2026 A Paper.pdf")

    c = TestClient(create_app(seeded, staging_path=staging))
    r = c.get(f"/api/papers/{key}/pdf")
    assert r.status_code == 200
    assert r.content == PDF_BYTES
    assert r.headers["content-type"] == "application/pdf"


def test_pdf_is_pulled_from_drive_once_uploaded(seeded, tmp_path):
    import io

    class DriveWithFile(FakeDrive):
        def download(self, file_id, **kw):
            return io.BytesIO(PDF_BYTES)

    with Ledger(seeded) as led:
        key = led.pending()[0].key
        led.decide_by_key(key, Decision.ACCEPTED)
        led.set_uploaded(key, "DRIVE1")

    c = TestClient(create_app(seeded, staging_path=tmp_path / "s",
                              drive_factory=lambda: DriveWithFile()))
    r = c.get(f"/api/papers/{key}/pdf")
    assert r.status_code == 200
    assert r.content == PDF_BYTES


def test_paper_with_no_known_pdf_is_404(seeded, tmp_path):
    # No local copy, nothing in Drive, and no open-access location to try.
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="Closed Access Only", year=2026,
                              url="https://dl.acm.org/doi/abs/10.1/x"))
        key = [p.key for p in led.pending() if "Closed" in p.title][0]

    c = TestClient(create_app(seeded, staging_path=tmp_path / "s"))
    r = c.get(f"/api/papers/{key}/pdf")
    assert r.status_code == 404
    assert "no open-access PDF" in r.json()["detail"]


def test_unfetched_paper_is_downloaded_on_demand(seeded, tmp_path):
    # The reader must work on an accepted-but-not-yet-fetched paper: that is
    # exactly the state papers sit in when you want to read them.
    asked = []

    class Fetched:
        ok, content, reason, size = True, PDF_BYTES, None, len(PDF_BYTES)

    def fake_fetch(candidates):
        asked.append(list(candidates))
        return Fetched()

    with Ledger(seeded) as led:
        key = led.pending()[0].key          # has an arXiv url, never staged

    c = TestClient(create_app(seeded, staging_path=tmp_path / "s",
                              pdf_fetcher=fake_fetch))
    r = c.get(f"/api/papers/{key}/pdf")
    assert r.status_code == 200
    assert r.content == PDF_BYTES
    assert asked and asked[0]              # it had somewhere to fetch from


def test_a_failed_on_demand_fetch_is_502(seeded, tmp_path):
    class Failed:
        ok, content, reason, size = False, None, "HTTP 403", 0

    with Ledger(seeded) as led:
        key = led.pending()[0].key

    c = TestClient(create_app(seeded, staging_path=tmp_path / "s",
                              pdf_fetcher=lambda c_: Failed()))
    r = c.get(f"/api/papers/{key}/pdf")
    assert r.status_code == 502
    assert "403" in r.json()["detail"]


def test_reading_does_not_stage_the_paper(seeded, tmp_path):
    # Staging would put it in the upload queue without anyone asking.
    class Fetched:
        ok, content, reason, size = True, PDF_BYTES, None, len(PDF_BYTES)

    with Ledger(seeded) as led:
        key = led.pending()[0].key

    c = TestClient(create_app(seeded, staging_path=tmp_path / "s",
                              pdf_fetcher=lambda c_: Fetched()))
    c.get(f"/api/papers/{key}/pdf")
    with Ledger(seeded) as led:
        assert led.get(key).staged_name is None


def test_pdf_for_an_unknown_paper_is_404(client):
    assert client.get("/api/papers/nope/pdf").status_code == 404


def test_a_drive_download_failure_is_502(seeded, tmp_path):
    from paper_grabber.drive import DriveError

    class BrokenDrive(FakeDrive):
        def download(self, file_id, **kw):
            raise DriveError("drive down")

    with Ledger(seeded) as led:
        key = led.pending()[0].key
        led.set_uploaded(key, "DRIVE1")

    c = TestClient(create_app(seeded, staging_path=tmp_path / "s",
                              drive_factory=lambda: BrokenDrive()))
    assert c.get(f"/api/papers/{key}/pdf").status_code == 502


def test_pdfjs_is_vendored_not_loaded_from_a_cdn(client):
    # The tailnet has no guarantee of internet access from the browser.
    r = client.get("/vendor/pdf.mjs")
    assert r.status_code == 200
    assert len(r.content) > 100_000

    body = client.get("/").text
    assert "'/vendor/pdf.mjs'" in body
    assert "cdnjs" not in body and "unpkg" not in body


def test_pdfjs_worker_is_vendored(client):
    assert client.get("/vendor/pdf.worker.mjs").status_code == 200


def test_pdfjs_is_loaded_lazily(client):
    # 2.7 MB should not be fetched for a session that only triages.
    body = client.get("/").text
    assert "await import('/vendor/pdf.mjs')" in body


def test_reader_renders_pages_lazily(client):
    body = client.get("/").text
    assert "IntersectionObserver" in body
    assert "rdpage" in body


def test_reader_is_an_advertised_capability(client):
    assert "reader" in client.get("/api/version").json()["capabilities"]


def test_reader_sits_below_the_header_not_over_it(client):
    # A full-screen takeover hid the tabs and Check now; the reader should
    # leave the app's navigation reachable while reading.
    body = client.get("/").text
    rule = body.split("#reader {")[1].split("}")[0]
    assert "top: var(--header-h" in rule
    assert "inset: 0" not in rule


def test_leaving_a_tab_closes_the_reader(client):
    assert "if (state.readerPaper) closeReader();" in client.get("/").text


def test_pdf_link_opens_the_reader_once_the_file_is_held(client):
    # Clicking a link labelled PDF should show the PDF, not open a new tab at
    # the publisher -- that was the same word meaning two things.
    body = client.get("/").text
    links = body.split("function cardLinks")[1].split("function wirePdfLink")[0]
    assert "hasPdfFile(p)" in links
    assert 'class="pdflink"' in links
    assert "wirePdfLink" in body


def test_pdf_link_stays_external_before_the_file_exists(client):
    # During triage there is no local copy, and the source link is the point.
    links = client.get("/").text.split("function cardLinks")[1].split("function wirePdfLink")[0]
    assert "link(p.pdf_url, 'PDF')" in links


def test_every_card_wires_the_pdf_link(client):
    # cardBody is shared by five renderers; a missed one would be a dead link.
    assert client.get("/").text.count("wirePdfLink(el, p);") == 5


def test_read_is_offered_whenever_a_paper_can_be_opened(client):
    # Requiring a downloaded copy hid the reader on accepted-but-unfetched
    # papers -- precisely the ones a user wants to read.
    body = client.get("/").text
    assert "return Boolean(p.can_read);" in body


def test_can_read_covers_an_unfetched_open_access_paper(client):
    p = next(x for x in client.get("/api/pending").json()["papers"]
             if "FPGA" in x["title"])
    assert p["staged"] is False and p["uploaded"] is False
    assert p["can_read"] is True          # an arXiv link is enough


def test_can_read_is_false_without_any_pdf_location(seeded):
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="Paywalled", year=2026,
                              url="https://dl.acm.org/doi/abs/10.1/x"))
    c = TestClient(create_app(seeded))
    p = next(x for x in c.get("/api/pending").json()["papers"]
             if x["title"] == "Paywalled")
    assert p["can_read"] is False


def test_portrait_notes_pane_is_capped_at_a_third(client):
    # It is absolutely positioned, so flex-basis does not apply -- it needs an
    # explicit width or it shrink-to-fits to about two fifths of the screen.
    portrait = client.get("/").text.split("@media (max-width: 900px)")[1]
    pane = portrait.split("#rdnotepane {")[1].split("#")[0]   # to the next rule
    assert "width: min(28rem, 33vw);" in pane


def test_swipe_commit_gives_a_haptic_tick(client):
    body = client.get("/").text
    assert "function vibrateTick" in body
    assert "navigator.vibrate(10)" in body
    # Guarded by reduced-motion, and fired only on commit.
    assert "matchMedia('(prefers-reduced-motion: reduce)').matches) return;" in body
    assert "vibrateTick();   // only on commit, never on the snap-back" in body


def test_auth_is_a_single_header_button(client):
    body = client.get("/").text
    tb = body.split('<div class="titlebar">')[1].split("</div>")[0]
    # In the header row, beside Check and Rejected.
    assert 'id="authbtn"' in tb
    # The old separate row and its status line are gone.
    assert 'id="authbar"' not in body
    assert "authmsg" not in body


def test_no_access_renewal_text(client):
    # The renewal note described something automatic the user cannot act on.
    assert "renew automatically" not in client.get("/").text


def test_auth_button_toggles_sign_in_and_out(client):
    body = client.get("/").text
    assert "state.authSignedIn" in body
    assert "'/api/auth/signout'" in body
    assert "'/auth/google/start'" in body


# --- attaching a local PDF ----------------------------------------------------


REAL_PDF = b"%PDF-1.7\n" + b"local upload " * 40 + b"\n%%EOF\n"


def closed_access_key(seeded):
    """An accepted paper with no open-access location."""
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="Closed Paper", year=2026,
                              url="https://dl.acm.org/doi/abs/10.1/x"))
        key = [p.key for p in led.pending() if "Closed" in p.title][0]
        led.decide_by_key(key, Decision.ACCEPTED)
    return key


def test_has_oa_pdf_flags_the_manual_case(seeded):
    key = closed_access_key(seeded)
    c = TestClient(create_app(seeded))
    p = next(x for x in c.get("/api/accepted").json()["unfiled"] if x["key"] == key)
    assert p["has_oa_pdf"] is False


def test_a_valid_pdf_is_attached_and_staged(seeded, tmp_path):
    key = closed_access_key(seeded)
    c = TestClient(create_app(seeded, staging_path=tmp_path / "staging"))
    r = c.post(f"/api/papers/{key}/local-pdf",
               files={"file": ("paper.pdf", REAL_PDF, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["staged"] is True
    with Ledger(seeded) as led:
        name = led.get(key).staged_name
    assert name
    assert (tmp_path / "staging" / name).read_bytes() == REAL_PDF


def test_a_non_pdf_is_refused_by_its_bytes(seeded, tmp_path):
    # A browser's content-type is only a hint; the magic bytes are the truth.
    key = closed_access_key(seeded)
    c = TestClient(create_app(seeded, staging_path=tmp_path / "s"))
    r = c.post(f"/api/papers/{key}/local-pdf",
               files={"file": ("fake.pdf", b"<html>not a pdf</html>", "application/pdf")})
    assert r.status_code == 400
    assert "not a PDF" in r.json()["detail"]
    with Ledger(seeded) as led:
        assert led.get(key).staged_name is None


def test_reattaching_replaces_without_orphaning(seeded, tmp_path):
    key = closed_access_key(seeded)
    c = TestClient(create_app(seeded, staging_path=tmp_path / "staging"))
    c.post(f"/api/papers/{key}/local-pdf",
           files={"file": ("a.pdf", REAL_PDF, "application/pdf")})
    second = b"%PDF-1.7\nreplacement\n%%EOF\n"
    c.post(f"/api/papers/{key}/local-pdf",
           files={"file": ("b.pdf", second, "application/pdf")})

    staged = list((tmp_path / "staging").glob("*.pdf"))
    assert len(staged) == 1                       # replaced, not duplicated
    assert staged[0].read_bytes() == second


def test_attaching_to_an_uploaded_paper_is_refused(seeded, tmp_path):
    key = closed_access_key(seeded)
    with Ledger(seeded) as led:
        led.set_uploaded(key, "DRIVE1")
    c = TestClient(create_app(seeded, staging_path=tmp_path / "s"))
    r = c.post(f"/api/papers/{key}/local-pdf",
               files={"file": ("a.pdf", REAL_PDF, "application/pdf")})
    assert r.status_code == 409


def test_the_attached_file_is_what_uploads_to_drive(seeded, tmp_path, monkeypatch):
    # The point of the feature: a closed-access paper reaches Drive from the
    # user's file, never from a download attempt.
    key = closed_access_key(seeded)
    staging = tmp_path / "staging"
    c = TestClient(create_app(seeded, staging_path=staging))
    c.post(f"/api/papers/{key}/local-pdf",
           files={"file": ("mine.pdf", REAL_PDF, "application/pdf")})

    with Ledger(seeded) as led:
        led.set_destination(key, "F1", "Quantum")
        staged_name = led.get(key).staged_name

    # A download must never be attempted for this paper.
    import paper_grabber.fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "download_first_available",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched!")))

    import hashlib
    from paper_grabber.staging import RemoteFile
    from paper_grabber.uploader import make_upload_job

    class Drive:
        def __init__(self): self.uploaded = None
        def upload(self, path, *, folder_id, name=None, description=None):
            self.uploaded = path.read_bytes()
            return RemoteFile(file_id="D1", size=len(self.uploaded),
                              md5=hashlib.md5(self.uploaded).hexdigest())
        def close(self): pass

    drive = Drive()
    make_upload_job(ledger_path=seeded, staging_path=staging, keys=[key],
                    drive_factory=lambda: drive)()
    assert drive.uploaded == REAL_PDF             # the user's file, verbatim


def test_page_has_the_attach_control(client):
    body = client.get("/").text
    assert 'id="localfile"' in body
    assert "function attachBtn" in body
    assert "/local-pdf" in body
    # The button lives in `actions`, which is appended to the card only after
    # wiring; wiring against the card would find nothing and the click would
    # do nothing. Guard that regression.
    assert "wireAttach(actions, p, el)" in body


# --- detaching a local PDF ----------------------------------------------------


def test_detach_removes_the_reference_and_the_file(seeded, tmp_path):
    key = closed_access_key(seeded)
    staging = tmp_path / "staging"
    c = TestClient(create_app(seeded, staging_path=staging))
    c.post(f"/api/papers/{key}/local-pdf",
           files={"file": ("a.pdf", REAL_PDF, "application/pdf")})
    with Ledger(seeded) as led:
        name = led.get(key).staged_name
    assert (staging / name).exists()

    r = c.delete(f"/api/papers/{key}/local-pdf")
    assert r.status_code == 200
    assert r.json()["staged"] is False
    with Ledger(seeded) as led:
        assert led.get(key).staged_name is None          # card back to usual
    assert not (staging / name).exists()                 # file gone


def test_detach_when_nothing_attached_is_404(seeded, tmp_path):
    key = closed_access_key(seeded)
    c = TestClient(create_app(seeded, staging_path=tmp_path / "s"))
    r = c.delete(f"/api/papers/{key}/local-pdf")
    assert r.status_code == 404


def test_detach_after_drive_upload_is_refused(seeded, tmp_path):
    key = closed_access_key(seeded)
    with Ledger(seeded) as led:
        led.set_uploaded(key, "DRIVE1")
    c = TestClient(create_app(seeded, staging_path=tmp_path / "s"))
    r = c.delete(f"/api/papers/{key}/local-pdf")
    assert r.status_code == 409


def test_detach_tolerates_a_missing_staged_file(seeded, tmp_path):
    # The reference is the source of truth; a hand-deleted file must not wedge
    # the card in an attached state.
    key = closed_access_key(seeded)
    staging = tmp_path / "staging"
    c = TestClient(create_app(seeded, staging_path=staging))
    c.post(f"/api/papers/{key}/local-pdf",
           files={"file": ("a.pdf", REAL_PDF, "application/pdf")})
    with Ledger(seeded) as led:
        (staging / led.get(key).staged_name).unlink()

    r = c.delete(f"/api/papers/{key}/local-pdf")
    assert r.status_code == 200
    with Ledger(seeded) as led:
        assert led.get(key).staged_name is None


def test_page_has_the_detach_control(client):
    body = client.get("/").text
    assert "function detachLocal" in body
    assert "'trash-2'" in body
    assert "method: 'DELETE'" in body


def test_counts_bar_summarises_the_pipeline(client):
    # Four disjoint facts -- pending, triaged, kept-rate, archived -- with the
    # rate labelled so it can't be misread as "share in Drive", and no
    # double-counting of accepted/filed/in-Drive.
    body = client.get("/").text
    assert "const triaged = (c.accepted || 0) + (c.rejected || 0);" in body
    assert "`${triaged} triaged`" in body
    assert "% kept`" in body
    assert "`${archived} archived`" in body


def test_status_banners_are_live_regions(client):
    # Screen readers announce a text change only inside a live region; without
    # these, "PDF attached" and errors are silent.
    body = client.get("/").text
    assert '<div id="error" role="alert" aria-live="assertive"' in body
    assert '<div id="notice" role="status" aria-live="polite"' in body
    # And the text must be written while displayed, or the change isn't spoken.
    assert "el.hidden = false;\n  el.textContent = msg;" in body


def test_overlays_are_modal_dialogs(client):
    body = client.get("/").text
    assert 'id="notepanel" role="dialog" aria-modal="true"' in body
    assert 'id="sheet" role="dialog" aria-modal="true"' in body


def test_overlays_trap_and_restore_focus(client):
    body = client.get("/").text
    assert "function trapFocus" in body
    assert "function releaseFocus" in body
    # Both overlays install a trap and release it on close.
    assert "state.noteTrap = trapFocus(notePanel" in body
    assert "state.sheetTrap = trapFocus($('#sheet')" in body
    assert "releaseFocus(state.noteTrap)" in body
    assert "releaseFocus(state.sheetTrap)" in body
    # Escape closes the sheet as well as the note drawer.
    assert "else if (!$('#sheet').hidden) closeSheet();" in body


# --- per-alert skip rate ------------------------------------------------------


def test_pending_payload_carries_alert_stats(seeded):
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="Noisy One", alert_query="cs.AI"))
        led.record(AlertPaper(title="Noisy Two", alert_query="cs.AI"))
        led.decide("Noisy One", Decision.REJECTED)
        led.decide("Noisy Two", Decision.REJECTED)
    c = TestClient(create_app(seeded))
    stats = c.get("/api/pending").json()["alert_stats"]
    assert stats["cs.AI"] == {"accepted": 0, "rejected": 2, "pending": 0}


def test_sidebar_renders_skip_rate_and_sort_toggle(client):
    body = client.get("/").text
    # The rate, its noise threshold, and the stash off the pending payload.
    assert "function alertRate" in body
    assert "const MIN_TRIAGED_FOR_RATE = 5;" in body
    assert "% dropped · ${triaged} triaged" in body
    assert "state.alertStats = data.alert_stats || {};" in body
    # The sort toggle between pending volume and drop rate.
    assert 'id="alertsort"' in body
    assert "state.alertSort === 'skip' ? 'Sort: most dropped'" in body


def test_triage_supports_swipe_and_keyboard(client):
    body = client.get("/").text
    # Swipe: the drag machinery, the commit threshold, the directional stamps,
    # and the touch-action that leaves vertical scrolling to the browser.
    assert "function wireSwipe" in body
    assert "const SWIPE_COMMIT" in body
    assert 'class="swipefb keep"' in body
    assert 'class="swipefb skip"' in body
    assert "touch-action: pan-y" in body
    # Keyboard: Keep/Drop mnemonic keys, plus the direction-matching arrows.
    assert "function keyDecide" in body
    assert "case 'k': case 'ArrowRight':" in body
    assert "case 'd': case 'ArrowLeft':" in body
    # Button, swipe, and key all record through one path.
    assert "async function commitDecision" in body


def test_decision_vocabulary_is_keep_and_drop(client):
    body = client.get("/").text
    # The accept/reject actions read as Keep/Drop everywhere they show text.
    assert ">Keep</div>" in body                       # swipe stamp
    assert ">Drop</div>" in body
    assert "'Keep (press K)'" in body                   # triage buttons
    assert "'Drop (press D)'" in body
    assert 'data-tip="Drop the selected papers"' in body   # bulk action
    assert 'data-tip="Show dropped papers"' in body        # header toggle
    assert "Nothing dropped yet." in body                  # empty state
    # The old vocabulary is gone from what the user reads.
    assert "Interesting (press A)" not in body
    assert "Nothing rejected yet." not in body


def test_swipe_hint_shows_once(client):
    body = client.get("/").text
    assert 'id="swipehint"' in body
    assert "swipe a card right to keep, left to drop" in body
    assert "const SWIPE_HINT_KEY = 'rs.swipehint.seen';" in body
    # Retired on the first decision and remembered across visits.
    assert "dismissSwipeHint();" in body
    assert "swipeHintSeen() || shown.length === 0" in body


def test_risky_filing_actions_carry_text_labels(client):
    # Attach/remove, upload, and unfile show their word beside the glyph so a
    # bare icon can't be mis-tapped -- the attach/remove pair especially.
    body = client.get("/").text
    assert "function textBtn" in body
    assert "textBtn('up', 'cloud-upload', 'Upload'" in body
    assert "textBtn('unfile', 'corner-up-left', 'Unfile'" in body
    assert "textBtn('attach', 'file-up', 'Attach'" in body
    assert "textBtn('detach', 'trash-2', 'Remove'" in body
    # The fuller description is still the accessible name, not the short word.
    assert "'Attach a PDF from this device'" in body


def test_filters_apply_across_all_list_views(client):
    body = client.get("/").text
    # The sidebar acts on whichever list the current tab shows...
    assert "function activePapers" in body
    assert "function passesFilters" in body
    assert "function applyFilters" in body
    # ...each list view filters its data through the same path.
    assert "function renderFiling" in body
    assert "function renderProcessed" in body
    assert "function renderRejected" in body
    assert "const shown = passesFilters(raw);" in body
    # The sidebar is shown on every list view, not just triage.
    assert "['triage', 'reading', 'filing', 'processed', 'rejected'].includes(state.tab)" in body
    # A filtered-empty list reads differently from a genuinely empty one.
    assert "Nothing matches this filter." in body


def test_no_folder_suggestion_without_a_pdf(client):
    # A suggestion is a one-click "file here"; offering it for a paper with no
    # PDF lands it in Ready to upload with nothing to upload.
    body = client.get("/").text
    assert "const hint = hasPdfFile(p) ? state.suggestions?.[p.key] : null;" in body


# --- reading queue ------------------------------------------------------------


def test_reading_endpoint_lists_kept_papers_with_state(seeded):
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="Keeper", year=2026))
        led.decide("Keeper", Decision.ACCEPTED)
    c = TestClient(create_app(seeded))
    data = c.get("/api/reading").json()
    keeper = next(p for p in data["papers"] if p["title"] == "Keeper")
    assert keeper["read_state"] == "unread"
    assert keeper["pinned"] is False
    assert data["counts"]["unread"] >= 1


def test_read_state_endpoint_updates(seeded):
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="Keeper", year=2026))
        led.decide("Keeper", Decision.ACCEPTED)
        key = [p.key for p in led.reading() if p.title == "Keeper"][0]
    c = TestClient(create_app(seeded))
    r = c.put(f"/api/papers/{key}/read-state", json={"state": "reading"})
    assert r.status_code == 200
    with Ledger(seeded) as led:
        assert led.get(key).read_state == "reading"


def test_read_state_endpoint_rejects_bad_state(seeded):
    with Ledger(seeded) as led:
        key = led.pending()[0].key
    c = TestClient(create_app(seeded))
    assert c.put(f"/api/papers/{key}/read-state", json={"state": "nope"}).status_code == 400


def test_read_state_endpoint_404_for_unknown_paper(seeded):
    c = TestClient(create_app(seeded))
    assert c.put("/api/papers/nope/read-state", json={"state": "read"}).status_code == 404


def test_pin_endpoint_toggles(seeded):
    with Ledger(seeded) as led:
        led.record(AlertPaper(title="Keeper", year=2026))
        led.decide("Keeper", Decision.ACCEPTED)
        key = [p.key for p in led.reading() if p.title == "Keeper"][0]
    c = TestClient(create_app(seeded))
    assert c.put(f"/api/papers/{key}/pin", json={"pinned": True}).status_code == 200
    with Ledger(seeded) as led:
        assert led.get(key).pinned is True


def test_reading_view_and_controls_present(client):
    body = client.get("/").text
    assert 'id="tab-reading"' in body
    assert 'id="view-reading"' in body
    assert "function readingCard" in body
    assert "function setReadState" in body
    # Opening the reader marks a paper as reading; the reader can mark it read.
    assert "async function markReading" in body
    assert "id=\"rdread\"" in body
    # The badge follows the unread count.
    assert "$('#badge-reading').textContent = c.unread || 0;" in body


def test_reading_tab_has_a_read_state_filter(client):
    body = client.get("/").text
    # A sidebar group, hidden except on the Reading tab, with the three states.
    assert 'id="fg-readstate"' in body
    assert 'data-state="unread"' in body
    assert 'data-state="reading"' in body
    assert 'data-state="read"' in body
    assert "$('#fg-readstate').hidden = state.tab !== 'reading';" in body
    # The list is filtered by the chosen states, on top of search + alerts.
    assert "passesFilters(raw).filter(p => state.readStates.has(p.read_state))" in body
    assert "function renderReadStateFilter" in body
