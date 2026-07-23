"""Fetch-and-upload-from-a-card tests. No network, no real Drive."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from paper_grabber.ledger import Decision, Ledger
from paper_grabber.models import AlertPaper
from paper_grabber.server import create_app
from paper_grabber.staging import RemoteFile, StagingArea
from paper_grabber.uploader import UploadRunner, make_upload_job

PDF = b"%PDF-1.7\npretend paper\n"


class FakeDriveUpload:
    def __init__(self, *, md5=None, error=None):
        self.md5 = md5
        self.error = error
        self.uploads = []
        self.descriptions = []

    def upload(self, path, *, folder_id, name=None, description=None):
        if self.error:
            raise self.error
        data = path.read_bytes()
        self.uploads.append((path.name, folder_id))
        self.descriptions.append(description)
        return RemoteFile(
            file_id="DRIVE1",
            size=len(data),
            md5=self.md5 if self.md5 is not None else hashlib.md5(data).hexdigest(),
        )

    def close(self):
        pass


class FakeHTTP:
    def __init__(self, ok=True):
        self.ok = ok

    def close(self):
        pass


class FakeFetch:
    """Stands in for fetch.download_first_available via monkeypatch."""


@pytest.fixture
def seeded(tmp_path):
    db = tmp_path / "s.db"
    with Ledger(db) as led:
        led.record(AlertPaper(
            title="A Fetchable Paper", year=2026,
            url="https://arxiv.org/pdf/1234",
        ))
        key = led.pending()[0].key
        led.decide_by_key(key, Decision.ACCEPTED)
        led.set_destination(key, "FOLDER1", "Quantum")
        led.attach_enrichment(key, {"pdf_candidates": ["https://arxiv.org/pdf/1234"]})
    return db, key, tmp_path / "staging"


def run_job(db, key, staging, drive, monkeypatch, *, content=PDF, ok=True, reason=None):
    import paper_grabber.fetch as fetch_mod

    class Result:
        def __init__(self):
            self.ok = ok
            self.content = content
            self.reason = reason
            self.size = len(content or b"")

    monkeypatch.setattr(fetch_mod, "download_first_available",
                        lambda urls, client, **kw: Result())
    job = make_upload_job(
        ledger_path=db, staging_path=staging, keys=[key],
        drive_factory=lambda: drive, http_factory=lambda: FakeHTTP(),
    )
    return job()


# --- the happy path -----------------------------------------------------------


def test_fetches_then_uploads(seeded, monkeypatch):
    db, key, staging = seeded
    drive = FakeDriveUpload()
    result = run_job(db, key, staging, drive, monkeypatch)
    assert result.uploaded == 1
    assert result.ok
    assert drive.uploads[0][1] == "FOLDER1"


def test_local_copy_is_removed_after_verification(seeded, monkeypatch):
    db, key, staging = seeded
    run_job(db, key, staging, FakeDriveUpload(), monkeypatch)
    assert list(StagingArea(staging).pending()) == []


def test_ledger_records_the_drive_id(seeded, monkeypatch):
    db, key, staging = seeded
    run_job(db, key, staging, FakeDriveUpload(), monkeypatch)
    with Ledger(db) as led:
        assert led.get(key).drive_file_id == "DRIVE1"
        assert led.get(key).staged_name is None


def test_already_staged_file_is_not_re_downloaded(seeded, monkeypatch):
    db, key, staging = seeded
    area = StagingArea(staging)
    area.stage("2026 A Fetchable Paper.pdf", PDF)
    with Ledger(db) as led:
        led.set_staged(key, "2026 A Fetchable Paper.pdf")

    calls = []
    import paper_grabber.fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "download_first_available",
                        lambda *a, **kw: calls.append(1))
    job = make_upload_job(ledger_path=db, staging_path=staging, keys=[key],
                          drive_factory=lambda: FakeDriveUpload(),
                          http_factory=lambda: FakeHTTP())
    assert job().uploaded == 1
    assert calls == []


# --- refusals -----------------------------------------------------------------


def test_unverified_upload_keeps_the_local_file(seeded, monkeypatch):
    db, key, staging = seeded
    drive = FakeDriveUpload(md5="0" * 32)      # wrong checksum
    result = run_job(db, key, staging, drive, monkeypatch)
    assert result.failed == 1
    assert "unverified" in result.outcomes[0].detail
    assert len(StagingArea(staging).pending()) == 1     # the only copy survives


def test_paper_without_a_destination_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    with Ledger(db) as led:
        led.record(AlertPaper(title="No Destination", year=2026))
        key = led.pending()[0].key
        led.decide_by_key(key, Decision.ACCEPTED)
    result = run_job(db, key, tmp_path / "st", FakeDriveUpload(), monkeypatch)
    assert result.failed == 1
    assert "no destination" in result.outcomes[0].detail


def test_paper_with_no_pdf_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    with Ledger(db) as led:
        led.record(AlertPaper(title="Closed Access", year=2026,
                              url="https://dl.acm.org/doi/abs/10.1/x"))
        key = led.pending()[0].key
        led.decide_by_key(key, Decision.ACCEPTED)
        led.set_destination(key, "F1", "Folder")
    result = run_job(db, key, tmp_path / "st", FakeDriveUpload(), monkeypatch)
    assert result.failed == 1
    assert "no open-access PDF" in result.outcomes[0].detail


def test_download_failure_is_reported(seeded, monkeypatch):
    db, key, staging = seeded
    result = run_job(db, key, staging, FakeDriveUpload(), monkeypatch,
                     ok=False, reason="HTTP 403")
    assert result.failed == 1
    assert "403" in result.outcomes[0].detail


def test_drive_error_is_reported(seeded, monkeypatch):
    db, key, staging = seeded
    drive = FakeDriveUpload(error=RuntimeError("drive down"))
    result = run_job(db, key, staging, drive, monkeypatch)
    assert result.failed == 1
    assert "drive down" in result.outcomes[0].detail


def test_already_uploaded_is_a_no_op(seeded, monkeypatch):
    db, key, staging = seeded
    with Ledger(db) as led:
        led.set_uploaded(key, "EXISTING")
    result = run_job(db, key, staging, FakeDriveUpload(), monkeypatch)
    assert result.uploaded == 0
    assert "already in Drive" in result.outcomes[0].detail


def test_unknown_key_is_reported(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    Ledger(db).close()
    result = run_job(db, "nope", tmp_path / "st", FakeDriveUpload(), monkeypatch)
    assert result.failed == 1
    assert "no such paper" in result.outcomes[0].detail


# --- endpoints ----------------------------------------------------------------


def test_unfile_returns_a_paper_to_the_queue(seeded):
    db, key, staging = seeded
    c = TestClient(create_app(db, staging_path=staging))
    assert c.post(f"/api/papers/{key}/unfile").status_code == 200
    acc = c.get("/api/accepted").json()
    assert len(acc["filed"]) == 0 and len(acc["unfiled"]) == 1


def test_unfile_refuses_an_uploaded_paper(seeded):
    db, key, staging = seeded
    with Ledger(db) as led:
        led.set_uploaded(key, "DRIVE1")
    c = TestClient(create_app(db, staging_path=staging))
    # Clearing the destination would not remove it from Drive, so the button
    # must not pretend otherwise.
    assert c.post(f"/api/papers/{key}/unfile").status_code == 409


def test_unfile_unknown_paper_is_404(seeded):
    db, _, staging = seeded
    c = TestClient(create_app(db, staging_path=staging))
    assert c.post("/api/papers/nope/unfile").status_code == 404


def test_upload_endpoint_starts_a_job(seeded):
    db, key, staging = seeded
    runner = UploadRunner(lambda: __import__("paper_grabber.uploader", fromlist=["x"]).UploadResult(started_at=0, uploaded=1))
    c = TestClient(create_app(db, staging_path=staging, upload_runner=runner))
    r = c.post("/api/upload", json={"keys": [key]})
    assert r.status_code == 202
    runner.wait(5)
    assert c.get("/api/upload").json()["last"]["uploaded"] == 1


def test_upload_with_no_keys_is_refused(seeded):
    db, _, staging = seeded
    c = TestClient(create_app(db, staging_path=staging))
    assert c.post("/api/upload", json={"keys": []}).status_code == 400


def test_upload_status_before_any_run(seeded):
    db, _, staging = seeded
    c = TestClient(create_app(db, staging_path=staging))
    assert c.get("/api/upload").json()["running"] is False


def test_filed_cards_have_both_buttons(seeded):
    db, _, staging = seeded
    body = TestClient(create_app(db, staging_path=staging)).get("/").text
    assert "textBtn('up'" in body and "textBtn('unfile'" in body


# --- notes reach Drive at upload time -----------------------------------------


def test_note_becomes_the_drive_description(seeded, monkeypatch):
    db, key, staging = seeded
    with Ledger(db) as led:
        led.set_note(key, "Compare with Plaquette.")
    drive = FakeDriveUpload()
    run_job(db, key, staging, drive, monkeypatch)
    assert drive.descriptions == ["Compare with Plaquette."]


def test_no_note_sends_no_description(seeded, monkeypatch):
    db, key, staging = seeded
    drive = FakeDriveUpload()
    run_job(db, key, staging, drive, monkeypatch)
    assert drive.descriptions == [None]


def test_note_survives_until_upload(seeded, monkeypatch):
    # The whole point: it lives in the ledger until the file exists in Drive.
    db, key, staging = seeded
    with Ledger(db) as led:
        led.set_note(key, "Read section 4.")
        assert led.get(key).note == "Read section 4."
        assert led.get(key).drive_file_id is None
    run_job(db, key, staging, FakeDriveUpload(), monkeypatch)
    with Ledger(db) as led:
        assert led.get(key).drive_file_id == "DRIVE1"
