"""Upload staged PDFs to Google Drive.

Writes are confined to drive.file, so the app can only modify files it created
itself. Folder browsing uses drive.metadata.readonly, which exposes names and
hierarchy but never file contents -- enough to let the destination picker walk
an existing Drive tree, and no more.

Every upload requests ``md5Checksum`` and ``size`` back, because those are what
StagingArea.confirm() needs to prove the remote copy is intact before the local
one is deleted. An upload that cannot report them is treated as unverified, and
the local file survives.

Uploads are resumable: a 30 MB thesis over a laptop's wifi is exactly the case
where a single-shot upload fails halfway and reports success for a truncated
file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import IO, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .staging import RemoteFile

PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"

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
        """Turn a folder ID or a path into a folder ID."""
        if "/" not in spec and _looks_like_id(spec):
            return spec

        parent = "root"
        for part in [p for p in spec.strip("/").split("/") if p]:
            parent = self._child_folder_id(parent, part)
        return parent

    def _child_folder_id(self, parent: str, name: str) -> str:
        matches = [f for f in self.list_child_folders(parent) if f["name"] == name]
        if not matches:
            raise DriveError(f"no folder named {name!r} under {parent}")
        if len(matches) > 1:
            # Drive permits duplicate names; guessing would file papers into an
            # arbitrary one of them.
            raise DriveError(
                f"{len(matches)} folders named {name!r} under {parent}; "
                "use a folder ID instead"
            )
        return matches[0]["id"]

    # --- browsing -------------------------------------------------------------

    def list_child_folders(self, parent_id: str = "root") -> list[dict[str, str]]:
        """Immediate subfolders of a folder, name-ordered.

        Only folders: the destination picker has no use for files, and omitting
        them keeps a Drive full of PDFs navigable.
        """
        query = (
            f"mimeType = '{FOLDER_MIME}' and '{_escape(parent_id)}' in parents "
            "and trashed = false"
        )
        folders: list[dict[str, str]] = []
        page_token = None
        try:
            while True:
                resp = (
                    self._service.files()
                    .list(
                        q=query,
                        fields="nextPageToken, files(id,name)",
                        orderBy="name",
                        pageSize=200,
                        pageToken=page_token,
                    )
                    .execute()
                )
                folders.extend(
                    {"id": f["id"], "name": f["name"]} for f in resp.get("files", [])
                )
                page_token = resp.get("nextPageToken")
                if not page_token:
                    return folders
        except HttpError as exc:
            raise DriveError(f"could not list folders under {parent_id}: {exc}") from exc

    def folder_info(self, folder_id: str) -> dict[str, Any]:
        """Name and parent of one folder."""
        try:
            resp = (
                self._service.files()
                .get(fileId=folder_id, fields="id,name,parents,mimeType")
                .execute()
            )
        except HttpError as exc:
            raise DriveError(f"could not read folder {folder_id}: {exc}") from exc

        if resp.get("mimeType") and resp["mimeType"] != FOLDER_MIME:
            raise DriveError(f"{folder_id} is not a folder")
        return {
            "id": resp["id"],
            "name": resp.get("name", ""),
            "parents": resp.get("parents") or [],
        }

    def breadcrumb(self, folder_id: str, *, stop_at: str | None = None) -> list[dict[str, str]]:
        """Ancestors of a folder, outermost first, ending with the folder.

        `stop_at` bounds the walk at the configured base folder so the picker
        never invites navigation above it. Without a bound the walk ends at My
        Drive. A depth cap guards against a parent cycle, which Drive should
        not produce but which would otherwise hang the request.
        """
        if folder_id in ("root", ""):
            return [{"id": "root", "name": "My Drive"}]

        trail: list[dict[str, str]] = []
        current = folder_id
        for _ in range(64):
            info = self.folder_info(current)
            trail.append({"id": info["id"], "name": info["name"]})
            if stop_at and info["id"] == stop_at:
                break
            parents = info["parents"]
            if not parents:
                trail.append({"id": "root", "name": "My Drive"})
                break
            current = parents[0]
        trail.reverse()
        return trail

    def create_folder(self, name: str, *, parent_id: str) -> dict[str, str]:
        """Create a subfolder and return it."""
        try:
            resp = (
                self._service.files()
                .create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                    fields="id,name",
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveError(f"could not create folder {name!r}: {exc}") from exc
        return {"id": resp["id"], "name": resp["name"]}

    # --- upload ---------------------------------------------------------------

    def upload(
        self,
        path: Path,
        *,
        folder_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> RemoteFile:
        """Upload one file and return what Drive says it stored.

        `description` becomes Drive's own description field, which is where a
        note written during filing ends up: visible in Drive's details pane
        and searchable there, rather than living only in this app.
        """
        path = Path(path)
        if not path.exists():
            raise DriveError(f"{path} does not exist")

        size = path.stat().st_size
        media = MediaFileUpload(
            str(path),
            mimetype=PDF_MIME,
            resumable=size >= RESUMABLE_THRESHOLD,
        )
        metadata: dict[str, Any] = {"name": name or path.name, "parents": [folder_id]}
        if description:
            metadata["description"] = description

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

    def download(self, file_id: str, *, spool_bytes: int = 8 * 1024 * 1024) -> IO[bytes]:
        """Fetch a file's content, returned as a rewound file-like object.

        Spooled rather than held in memory: a small paper stays in RAM, a large
        one spills to disk. The reader runs against a 1 GB VM, where buffering
        a whole thesis alongside everything else is exactly the kind of thing
        that gets a process killed.

        The caller owns the handle and should close it.
        """
        buffer: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=spool_bytes)
        try:
            request = self._service.files().get_media(fileId=file_id)
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as exc:
            buffer.close()
            raise DriveError(f"could not download {file_id}: {exc}") from exc
        except Exception:
            buffer.close()
            raise

        buffer.seek(0)
        return buffer

    def file_status(self, file_id: str) -> dict[str, Any]:
        """Report whether an uploaded file is still in Drive.

        Returns ``{"present": bool, "trashed": bool, "name": str | None}``.

        A 404 means the file is genuinely gone. Any other failure raises,
        because the caller must be able to tell "definitely deleted" from
        "could not reach Drive" -- treating the latter as deletion would
        silently undo an upload that actually succeeded.
        """
        try:
            resp = (
                self._service.files()
                .get(fileId=file_id, fields="id,name,trashed")
                .execute()
            )
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and exc.resp.status == 404:
                return {"present": False, "trashed": False, "name": None}
            raise DriveError(f"could not check {file_id}: {exc}") from exc

        trashed = bool(resp.get("trashed"))
        return {
            # A file in the bin is gone as far as the user is concerned.
            "present": not trashed,
            "trashed": trashed,
            "name": resp.get("name"),
        }

    def set_description(self, file_id: str, description: str | None) -> None:
        """Update a file's Drive description -- where a paper's note lives."""
        try:
            self._service.files().update(
                fileId=file_id, body={"description": description or ""}
            ).execute()
        except HttpError as exc:
            raise DriveError(f"could not update {file_id}: {exc}") from exc

    def set_app_property(self, file_id: str, key: str, value: str | None) -> None:
        """Store a private key/value blob in the file's appProperties.

        Metadata attached to the file itself -- where a paper's highlights are
        mirrored -- without touching its content. App-private and portable with
        the file. Drive caps total properties near 124 KB per file; the caller
        keeps the payload well under that.
        """
        try:
            self._service.files().update(
                fileId=file_id, body={"appProperties": {key: value or ""}}
            ).execute()
        except HttpError as exc:
            raise DriveError(f"could not update {file_id}: {exc}") from exc

    def exists_in_folder(self, name: str, folder_id: str) -> bool:
        """True when a file of this name is already in the folder.

        Drive happily stores duplicates, so a re-run would otherwise pile up
        copies of the same paper.
        """
        query = (
            f"name = '{_escape(name)}' and '{_escape(folder_id)}' in parents "
            "and trashed = false"
        )
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


def _escape(value: str) -> str:
    """Escape a value for a Drive query string.

    A folder called "Nunez's papers" would otherwise terminate the quoted
    literal early and produce a syntax error, or worse, a different query.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _looks_like_id(value: str) -> bool:
    """Drive IDs are long opaque strings; folder names rarely are."""
    return len(value) > 20 and " " not in value
