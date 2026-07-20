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

    def __init__(self, tree=None, fail=False):
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
