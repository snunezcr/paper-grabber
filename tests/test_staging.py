import hashlib

import pytest

from paper_grabber.staging import (
    PARTIAL_SUFFIX,
    RemoteFile,
    StagingArea,
    VerificationError,
    md5_of,
)

CONTENT = b"%PDF-1.7\nfake paper bytes\n"
GOOD_MD5 = hashlib.md5(CONTENT).hexdigest()


@pytest.fixture
def area(tmp_path):
    return StagingArea(tmp_path / "staging")


def remote(size=len(CONTENT), md5=GOOD_MD5, file_id="drive-1"):
    return RemoteFile(file_id=file_id, size=size, md5=md5)


# --- staging ------------------------------------------------------------------


def test_stage_writes_the_file(area):
    p = area.stage("2026 - A Paper.pdf", CONTENT)
    assert p.read_bytes() == CONTENT
    assert p.name == "2026 - A Paper.pdf"


def test_stage_leaves_no_partial_behind(area):
    area.stage("2026 - A Paper.pdf", CONTENT)
    assert not any(f.name.endswith(PARTIAL_SUFFIX) for f in area.root.iterdir())


def test_staging_root_is_created(tmp_path):
    root = tmp_path / "nested" / "staging"
    StagingArea(root)
    assert root.is_dir()


def test_pending_lists_staged_files(area):
    area.stage("a.pdf", CONTENT)
    area.stage("b.pdf", CONTENT)
    assert {p.name for p in area.pending()} == {"a.pdf", "b.pdf"}


def test_pending_ignores_partials(area):
    area.stage("a.pdf", CONTENT)
    (area.root / ("b.pdf" + PARTIAL_SUFFIX)).write_bytes(b"half")
    assert [p.name for p in area.pending()] == ["a.pdf"]


def test_sweep_removes_partials_only(area):
    area.stage("a.pdf", CONTENT)
    (area.root / ("b.pdf" + PARTIAL_SUFFIX)).write_bytes(b"half")
    removed = area.sweep_partials()
    assert len(removed) == 1
    assert [p.name for p in area.pending()] == ["a.pdf"]


# --- the point of the module: delete only on proof ----------------------------


def test_confirm_deletes_after_a_verified_upload(area):
    p = area.stage("a.pdf", CONTENT)
    assert area.confirm(p, remote()) is True
    assert not p.exists()


def test_md5_mismatch_keeps_the_local_file(area):
    p = area.stage("a.pdf", CONTENT)
    with pytest.raises(VerificationError, match="MD5 mismatch"):
        area.confirm(p, remote(md5="0" * 32))
    assert p.exists()  # the only copy survives
    assert p.read_bytes() == CONTENT


def test_size_mismatch_keeps_the_local_file(area):
    p = area.stage("a.pdf", CONTENT)
    with pytest.raises(VerificationError, match="size mismatch"):
        area.confirm(p, remote(size=999))
    assert p.exists()


def test_missing_remote_md5_keeps_the_local_file(area):
    # No checksum means no proof; a truncated upload could still report the
    # right size, so absence of an MD5 must block the delete.
    p = area.stage("a.pdf", CONTENT)
    with pytest.raises(VerificationError, match="no MD5"):
        area.confirm(p, remote(md5=None))
    assert p.exists()


def test_truncated_upload_with_matching_size_is_still_caught(area):
    # Same length, different bytes -- size alone would wave this through.
    p = area.stage("a.pdf", CONTENT)
    other_md5 = hashlib.md5(b"X" * len(CONTENT)).hexdigest()
    with pytest.raises(VerificationError, match="MD5 mismatch"):
        area.confirm(p, remote(md5=other_md5))
    assert p.exists()


def test_verify_reports_a_vanished_local_file(area):
    p = area.stage("a.pdf", CONTENT)
    p.unlink()
    with pytest.raises(VerificationError, match="local file is gone"):
        area.confirm(p, remote())


def test_remote_without_size_still_verifies_by_md5(area):
    # Drive always sends size, but the MD5 is what actually proves identity.
    p = area.stage("a.pdf", CONTENT)
    assert area.confirm(p, remote(size=None)) is True
    assert not p.exists()


# --- checksum -----------------------------------------------------------------


def test_md5_matches_hashlib(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(CONTENT)
    assert md5_of(f) == GOOD_MD5


def test_md5_streams_large_files(tmp_path):
    # Bigger than one read chunk, to exercise the loop.
    blob = b"z" * (3 * 1024 * 1024 + 7)
    f = tmp_path / "big.bin"
    f.write_bytes(blob)
    assert md5_of(f) == hashlib.md5(blob).hexdigest()
