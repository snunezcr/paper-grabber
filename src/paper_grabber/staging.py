"""Local staging for downloaded PDFs.

A downloaded paper lives on local disk until Google Drive confirms it arrived
intact, and only then is the local copy removed. The local file is the *only*
copy in between, so the delete is gated on positive proof -- a matching size
and MD5 reported by Drive -- rather than on the upload call merely not raising.

Failing to verify leaves the file in place. Keeping a redundant copy costs
disk; deleting on a bad assumption loses the paper.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

# Downloads land here first; the suffix marks a file that is not yet complete
# and may be swept away on the next run.
PARTIAL_SUFFIX = ".part"

_CHUNK = 1024 * 1024


def md5_of(path: Path) -> str:
    """Streaming MD5, so a 300 MB thesis does not land in memory."""
    h = hashlib.md5()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class VerificationError(Exception):
    """Drive's copy did not match the local file, so nothing was deleted."""


@dataclass
class RemoteFile:
    """What Drive reported after an upload.

    ``md5`` is optional because Drive omits it for Google-native formats; for
    an uploaded PDF it is always present, and its absence is treated as a
    reason not to delete.
    """

    file_id: str
    size: int | None = None
    md5: str | None = None


class StagingArea:
    """A directory of downloaded-but-not-yet-uploaded PDFs."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        return self.root / name

    def stage(self, name: str, content: bytes) -> Path:
        """Write a download to staging atomically.

        The bytes go to a ``.part`` file which is renamed into place only once
        fully written, so an interrupted run can never leave a truncated file
        that looks like a finished paper.
        """
        final = self.path_for(name)
        partial = final.with_name(final.name + PARTIAL_SUFFIX)
        partial.write_bytes(content)
        os.replace(partial, final)
        return final

    def pending(self) -> list[Path]:
        """Staged files awaiting upload, oldest first."""
        files = [
            p
            for p in self.root.iterdir()
            if p.is_file() and not p.name.endswith(PARTIAL_SUFFIX)
        ]
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def sweep_partials(self) -> list[Path]:
        """Delete leftover ``.part`` files from interrupted runs."""
        removed = []
        for p in self.root.iterdir():
            if p.is_file() and p.name.endswith(PARTIAL_SUFFIX):
                p.unlink()
                removed.append(p)
        return removed

    def verify(self, path: Path, remote: RemoteFile) -> None:
        """Raise unless Drive's copy provably matches the local file."""
        if not path.exists():
            raise VerificationError(f"{path.name}: local file is gone")

        local_size = path.stat().st_size
        if remote.size is not None and remote.size != local_size:
            raise VerificationError(
                f"{path.name}: size mismatch (local {local_size}, remote {remote.size})"
            )

        if remote.md5 is None:
            # No checksum means no proof. Drive always supplies one for an
            # uploaded PDF, so its absence signals something unexpected.
            raise VerificationError(f"{path.name}: Drive reported no MD5")

        local_md5 = md5_of(path)
        if local_md5 != remote.md5:
            raise VerificationError(
                f"{path.name}: MD5 mismatch (local {local_md5}, remote {remote.md5})"
            )

    def confirm(self, path: Path, remote: RemoteFile) -> bool:
        """Verify the remote copy and, only then, delete the local one.

        Returns True when the local file was removed. Raises
        ``VerificationError`` -- leaving the file untouched -- when the remote
        copy cannot be proven identical.
        """
        self.verify(path, remote)
        path.unlink()
        return True
