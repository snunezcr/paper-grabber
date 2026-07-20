"""Drive uploader tests.

The Drive service is a fake, so the suite never authenticates or touches the
network. What matters here is that a successful upload yields the checksum
confirmation depends on, and that anything less is reported rather than
assumed.
"""

import hashlib

import pytest
from googleapiclient.errors import HttpError

from paper_grabber.drive import DriveClient, DriveError
from paper_grabber.staging import StagingArea, VerificationError

CONTENT = b"%PDF-1.7\npretend paper\n"
MD5 = hashlib.md5(CONTENT).hexdigest()


class FakeRequest:
    def __init__(self, result, *, resumable=False, error=None, chunks=1):
        self._result = result
        self.resumable = resumable
        self._error = error
        self._chunks = chunks
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result

    def next_chunk(self):
        self.calls += 1
        if self._error:
            raise self._error
        if self.calls < self._chunks:
            return (None, None)  # still uploading
        return (None, self._result)


class FakeFiles:
    def __init__(self, *, create_result=None, list_results=None, create_error=None,
                 resumable=False, chunks=1, get_results=None, get_error=None):
        self.get_results = get_results or {}
        self.get_error = get_error
        self.get_calls = []
        self.create_result = create_result
        self.list_results = list(list_results or [])
        self.create_error = create_error
        self.resumable = resumable
        self.chunks = chunks
        self.create_calls = []
        self.list_queries = []

    def create(self, body=None, media_body=None, fields=None):
        self.create_calls.append({"body": body, "fields": fields, "media": media_body})
        return FakeRequest(
            self.create_result, resumable=self.resumable,
            error=self.create_error, chunks=self.chunks,
        )

    def list(self, q=None, fields=None, pageSize=None, orderBy=None, pageToken=None):
        self.list_queries.append(q)
        result = self.list_results.pop(0) if self.list_results else {"files": []}
        return FakeRequest(result)

    def get(self, fileId=None, fields=None):
        self.get_calls.append(fileId)
        if self.get_error:
            return FakeRequest(None, error=self.get_error)
        return FakeRequest(self.get_results.get(fileId, {}))


class FakeService:
    def __init__(self, files: FakeFiles):
        self._files = files

    def files(self):
        return self._files


def client(files: FakeFiles) -> DriveClient:
    return DriveClient(credentials=None, service=FakeService(files))


@pytest.fixture
def staged(tmp_path):
    area = StagingArea(tmp_path / "staging")
    return area, area.stage("2026 A Paper.pdf", CONTENT)


def http_error(status):
    class Resp:
        def __init__(self, s):
            self.status = s
            self.reason = "error"

    return HttpError(Resp(status), b"{}")


# --- upload -------------------------------------------------------------------


def test_upload_returns_checksum_and_size(staged):
    _, path = staged
    files = FakeFiles(create_result={"id": "F1", "name": path.name,
                                     "size": str(len(CONTENT)), "md5Checksum": MD5})
    remote = client(files).upload(path, folder_id="FOLDER")
    assert remote.file_id == "F1"
    assert remote.size == len(CONTENT)
    assert remote.md5 == MD5


def test_upload_requests_the_confirmation_fields(staged):
    _, path = staged
    files = FakeFiles(create_result={"id": "F1", "size": "1", "md5Checksum": MD5})
    client(files).upload(path, folder_id="FOLDER")
    fields = files.create_calls[0]["fields"]
    assert "md5Checksum" in fields and "size" in fields


def test_upload_targets_the_requested_folder(staged):
    _, path = staged
    files = FakeFiles(create_result={"id": "F1", "size": "1", "md5Checksum": MD5})
    client(files).upload(path, folder_id="FOLDER")
    assert files.create_calls[0]["body"]["parents"] == ["FOLDER"]


def test_upload_can_override_the_name(staged):
    _, path = staged
    files = FakeFiles(create_result={"id": "F1", "size": "1", "md5Checksum": MD5})
    client(files).upload(path, folder_id="F", name="2026 - Renamed.pdf")
    assert files.create_calls[0]["body"]["name"] == "2026 - Renamed.pdf"


def test_missing_local_file_is_reported(tmp_path):
    files = FakeFiles(create_result={"id": "F1"})
    with pytest.raises(DriveError, match="does not exist"):
        client(files).upload(tmp_path / "nope.pdf", folder_id="F")


def test_http_error_becomes_drive_error(staged):
    _, path = staged
    files = FakeFiles(create_error=http_error(403))
    with pytest.raises(DriveError, match="upload of .* failed"):
        client(files).upload(path, folder_id="F")


def test_response_without_an_id_is_an_error(staged):
    _, path = staged
    files = FakeFiles(create_result={})
    with pytest.raises(DriveError, match="no file id"):
        client(files).upload(path, folder_id="F")


def test_resumable_upload_runs_to_completion(staged):
    _, path = staged
    files = FakeFiles(
        create_result={"id": "F1", "size": str(len(CONTENT)), "md5Checksum": MD5},
        resumable=True,
        chunks=3,
    )
    remote = client(files).upload(path, folder_id="F")
    assert remote.file_id == "F1"


# --- the handshake with staging ----------------------------------------------


def test_verified_upload_lets_staging_delete(staged):
    area, path = staged
    files = FakeFiles(create_result={"id": "F1", "size": str(len(CONTENT)), "md5Checksum": MD5})
    remote = client(files).upload(path, folder_id="F")
    assert area.confirm(path, remote) is True
    assert not path.exists()


def test_drive_reporting_a_different_md5_keeps_the_local_file(staged):
    # A corrupted or truncated upload: the local copy must survive.
    area, path = staged
    files = FakeFiles(create_result={"id": "F1", "size": str(len(CONTENT)),
                                     "md5Checksum": "0" * 32})
    remote = client(files).upload(path, folder_id="F")
    with pytest.raises(VerificationError, match="MD5 mismatch"):
        area.confirm(path, remote)
    assert path.exists()


def test_drive_omitting_the_md5_keeps_the_local_file(staged):
    area, path = staged
    files = FakeFiles(create_result={"id": "F1", "size": str(len(CONTENT))})
    remote = client(files).upload(path, folder_id="F")
    assert remote.md5 is None
    with pytest.raises(VerificationError, match="no MD5"):
        area.confirm(path, remote)
    assert path.exists()


# --- folders ------------------------------------------------------------------


def test_folder_id_passes_through_untouched():
    files = FakeFiles()
    fid = "1a2b3c4d5e6f7g8h9i0jKLMNOPqrstuv"
    assert client(files).resolve_folder(fid) == fid
    assert files.list_queries == []


def test_folder_path_is_resolved_segment_by_segment():
    files = FakeFiles(list_results=[
        {"files": [{"id": "RESEARCH", "name": "Research"}]},
        {"files": [{"id": "PAPERS", "name": "Papers"}]},
    ])
    assert client(files).resolve_folder("Research/Papers") == "PAPERS"
    assert "'root' in parents" in files.list_queries[0]
    assert "'RESEARCH' in parents" in files.list_queries[1]


def test_missing_folder_is_reported():
    files = FakeFiles(list_results=[{"files": []}])
    with pytest.raises(DriveError, match="no folder named"):
        client(files).resolve_folder("Nope")


def test_ambiguous_folder_name_is_refused():
    # Drive allows duplicate folder names; picking one at random would file
    # papers somewhere the user did not choose.
    files = FakeFiles(list_results=[
        {"files": [{"id": "A", "name": "Papers"}, {"id": "B", "name": "Papers"}]}
    ])
    with pytest.raises(DriveError, match="use a folder ID"):
        client(files).resolve_folder("Papers")


# --- browsing -----------------------------------------------------------------


def test_list_child_folders_returns_id_and_name():
    files = FakeFiles(list_results=[
        {"files": [{"id": "A", "name": "Quantum"}, {"id": "B", "name": "Networks"}]}
    ])
    assert client(files).list_child_folders("ROOT") == [
        {"id": "A", "name": "Quantum"},
        {"id": "B", "name": "Networks"},
    ]


def test_listing_asks_only_for_folders():
    files = FakeFiles(list_results=[{"files": []}])
    client(files).list_child_folders("ROOT")
    q = files.list_queries[0]
    assert "application/vnd.google-apps.folder" in q
    assert "trashed = false" in q


def test_listing_follows_pages():
    files = FakeFiles(list_results=[
        {"files": [{"id": "A", "name": "One"}], "nextPageToken": "t"},
        {"files": [{"id": "B", "name": "Two"}]},
    ])
    assert len(client(files).list_child_folders("ROOT")) == 2


def test_listing_escapes_an_apostrophe():
    files = FakeFiles(list_results=[{"files": []}])
    client(files).list_child_folders("Nunez's folder")
    assert "\\'" in files.list_queries[0]


def test_folder_info_returns_name_and_parents():
    files = FakeFiles(get_results={"F1": {
        "id": "F1", "name": "Papers", "parents": ["ROOT"],
        "mimeType": "application/vnd.google-apps.folder"}})
    info = client(files).folder_info("F1")
    assert info["name"] == "Papers"
    assert info["parents"] == ["ROOT"]


def test_folder_info_refuses_a_non_folder():
    files = FakeFiles(get_results={"F1": {
        "id": "F1", "name": "a.pdf", "mimeType": "application/pdf"}})
    with pytest.raises(DriveError, match="not a folder"):
        client(files).folder_info("F1")


def test_breadcrumb_walks_up_to_my_drive():
    files = FakeFiles(get_results={
        "PAPERS": {"id": "PAPERS", "name": "Papers", "parents": ["RESEARCH"],
                   "mimeType": "application/vnd.google-apps.folder"},
        "RESEARCH": {"id": "RESEARCH", "name": "Research", "parents": [],
                     "mimeType": "application/vnd.google-apps.folder"},
    })
    trail = client(files).breadcrumb("PAPERS")
    assert [c["name"] for c in trail] == ["My Drive", "Research", "Papers"]


def test_breadcrumb_stops_at_the_base_folder():
    # The picker must not invite navigation above the configured base.
    files = FakeFiles(get_results={
        "SUB": {"id": "SUB", "name": "Quantum", "parents": ["BASE"],
                "mimeType": "application/vnd.google-apps.folder"},
        "BASE": {"id": "BASE", "name": "Papers", "parents": ["ROOT"],
                 "mimeType": "application/vnd.google-apps.folder"},
    })
    trail = client(files).breadcrumb("SUB", stop_at="BASE")
    assert [c["name"] for c in trail] == ["Papers", "Quantum"]


def test_breadcrumb_of_root_is_my_drive():
    assert client(FakeFiles()).breadcrumb("root") == [{"id": "root", "name": "My Drive"}]


def test_breadcrumb_survives_a_parent_cycle():
    # Drive should never produce this, but an unbounded walk would hang.
    files = FakeFiles(get_results={
        "A": {"id": "A", "name": "A", "parents": ["B"],
              "mimeType": "application/vnd.google-apps.folder"},
        "B": {"id": "B", "name": "B", "parents": ["A"],
              "mimeType": "application/vnd.google-apps.folder"},
    })
    trail = client(files).breadcrumb("A")
    assert len(trail) <= 65


def test_create_folder_returns_the_new_folder():
    files = FakeFiles(create_result={"id": "NEW", "name": "Quantum"})
    assert client(files).create_folder("Quantum", parent_id="BASE") == {
        "id": "NEW", "name": "Quantum"}


def test_create_folder_uses_the_folder_mime_type():
    files = FakeFiles(create_result={"id": "NEW", "name": "Quantum"})
    client(files).create_folder("Quantum", parent_id="BASE")
    body = files.create_calls[0]["body"]
    assert body["mimeType"] == "application/vnd.google-apps.folder"
    assert body["parents"] == ["BASE"]


# --- duplicate detection ------------------------------------------------------


def test_exists_in_folder_true_when_present():
    files = FakeFiles(list_results=[{"files": [{"id": "F1"}]}])
    assert client(files).exists_in_folder("2026 - A.pdf", "FOLDER") is True


def test_exists_in_folder_false_when_absent():
    files = FakeFiles(list_results=[{"files": []}])
    assert client(files).exists_in_folder("2026 - A.pdf", "FOLDER") is False
