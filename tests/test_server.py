import json

import pytest
from fastapi.testclient import TestClient

from paper_grabber.ledger import Decision, Ledger
from paper_grabber.models import AlertPaper, normalize_title
from paper_grabber.server import create_app


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
    assert 'class="verify"' in app_client.get("/").text


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
    # which left every box unticked and looked like it had done nothing.
    body = client.get("/").text
    assert "state.alerts = new Set(alertCounts().keys());" in body


def test_none_button_clears_the_selection(client):
    assert "state.alerts.clear();" in client.get("/").text


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
    assert "cite" in selectable
    assert "cite" in ready
    assert "cite" in processed


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


def test_blocked_copy_still_shows_the_entry(client):
    body = client.get("/").text
    assert 'id="citebox"' in body
    assert "showCitation" in body


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
            if 'class="cite"' in seg and "cardstatus" in seg:
                assert seg.index('class="cite"') > seg.index("cardstatus")


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
    # The match count is relative to the alert selection, not the whole queue.
    body = client.get("/").text
    assert "byAlert(state.pending || []).filter(p => matchesQuery(p, terms))" in body


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
    # Rejected papers are suppressed on every future check; a mis-tap here
    # would bury them silently.
    body = client.get("/").text
    assert "state.lastUndo = {keys:" in body


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
    assert 'class="recover"' in body
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
    assert ">Rejected</button>" in bar
