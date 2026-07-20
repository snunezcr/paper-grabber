"""Upload staged PDFs to Google Drive.

Scope is drive.file only: this app can see and touch nothing in Drive except
the files it created itself. The destination is therefore given as a folder ID
rather than a path, since looking a folder up by name would need the sensitive
drive.metadata.readonly scope.

Every upload requests ``md5Checksum`` and ``size`` back, because those are what
StagingArea.confirm() needs to prove the remote copy is intact before the local
one is deleted. An upload that cannot report them is treated as unverified, and
the local file survives.

Uploads are resumable: a 30 MB thesis over a laptop's wifi is exactly the case
where a single-shot upload fails halfway and reports success for a truncated
file.
"""

from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .staging import RemoteFile

PDF_MIME = "application/pdf"

# Ask for exactly the fields confirmation depends on.
_UPLOAD_FIELDS = "id,name,size,md5Checksum"

# Above this, a single request is a bad bet on a home connection.
RESUMABLE_THRESHOLD = 5 * 1024 * 1024


class DriveError(Exception):
    """Drive refused an operation."""


class DriveClient:
    """Minimal Drive wrapper: resolve a folder, upload a file, verify it."""

    def __init__(self, credentials, *, service=None) -> None:
        # `service` is injectable so tests never touch the network.
        self._service = service or build("drive", "v3", credentials=credentials)

    # --- folders --------------------------------------------------------------

    def resolve_folder(self, spec: str) -> str:
        """Turn a folder ID into a folder ID, or explain why a path will not do.

        Path lookup needs drive.metadata.readonly, a sensitive scope this app
        deliberately does not request: drive.file alone cannot see folders it
        did not create. The folder ID is the last path segment of the folder's
        URL in Drive.
        """
        if _looks_like_id(spec):
            return spec
        raise DriveError(
            f"{spec!r} is not a Drive folder ID. This app requests only the "
            "drive.file scope, which cannot look folders up by name. Open the "
            "folder in Drive and copy the ID from the URL after /folders/."
        )

    # --- upload ---------------------------------------------------------------

    def upload(self, path: Path, *, folder_id: str, name: str | None = None) -> RemoteFile:
        """Upload one file and return what Drive says it stored."""
        path = Path(path)
        if not path.exists():
            raise DriveError(f"{path} does not exist")

        size = path.stat().st_size
        media = MediaFileUpload(
            str(path),
            mimetype=PDF_MIME,
            resumable=size >= RESUMABLE_THRESHOLD,
        )
        metadata = {"name": name or path.name, "parents": [folder_id]}

        try:
            request = self._service.files().create(
                body=metadata, media_body=media, fields=_UPLOAD_FIELDS
            )
            response = _execute(request)
        except HttpError as exc:
            raise DriveError(f"upload of {path.name} failed: {exc}") from exc

        if not response or "id" not in response:
            raise DriveError(f"upload of {path.name} returned no file id")

        remote_size = response.get("size")
        return RemoteFile(
            file_id=response["id"],
            size=int(remote_size) if remote_size is not None else None,
            md5=response.get("md5Checksum"),
        )

    def exists_in_folder(self, name: str, folder_id: str) -> bool:
        """True when a file of this name is already in the folder.

        Drive happily stores duplicates, so a re-run would otherwise pile up
        copies of the same paper.
        """
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{safe}' and '{folder_id}' in parents and trashed = false"
        try:
            resp = (
                self._service.files().list(q=query, fields="files(id)", pageSize=1).execute()
            )
        except HttpError as exc:
            raise DriveError(f"could not check for {name!r}: {exc}") from exc
        return bool(resp.get("files"))


def _execute(request):
    """Run a request, driving a resumable upload to completion."""
    if not getattr(request, "resumable", None):
        return request.execute()

    response = None
    while response is None:
        _, response = request.next_chunk()
    return response


def _looks_like_id(value: str) -> bool:
    """Drive IDs are long opaque strings; folder names rarely are."""
    return len(value) > 20 and " " not in value
