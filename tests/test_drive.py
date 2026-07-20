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
                 resumable=False, chunks=1):
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

    def list(self, q=None, fields=None, pageSize=None):
        self.list_queries.append(q)
        result = self.list_results.pop(0) if self.list_results else {"files": []}
        return FakeRequest(result)


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
    return area, area.stage("2026 - A Paper.pdf", CONTENT)


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
    assert files.list_queries == []  # no lookup needed


def test_folder_path_is_resolved_segment_by_segment():
    files = FakeFiles(list_results=[
        {"files": [{"id": "RESEARCH"}]},
        {"files": [{"id": "PAPERS"}]},
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
    # papers somewhere the user did not intend.
    files = FakeFiles(list_results=[{"files": [{"id": "A"}, {"id": "B"}]}])
    with pytest.raises(DriveError, match="use a folder ID"):
        client(files).resolve_folder("Papers")


def test_apostrophe_in_folder_name_is_escaped():
    files = FakeFiles(list_results=[{"files": [{"id": "X"}]}])
    client(files).resolve_folder("Nunez's papers")
    # Unescaped, the quote would terminate the query early.
    assert "\\'" in files.list_queries[0]


# --- duplicate detection ------------------------------------------------------


def test_exists_in_folder_true_when_present():
    files = FakeFiles(list_results=[{"files": [{"id": "F1"}]}])
    assert client(files).exists_in_folder("2026 - A.pdf", "FOLDER") is True


def test_exists_in_folder_false_when_absent():
    files = FakeFiles(list_results=[{"files": []}])
    assert client(files).exists_in_folder("2026 - A.pdf", "FOLDER") is False
