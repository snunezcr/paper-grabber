"""Build the saved PDF's filename: ``{YYYY} - {Title}.pdf``.

Android, Google Drive, and every FAT-derived filesystem in between reject a
handful of characters outright. Colons are the interesting case: they are both
illegal *and* extremely common in academic titles ("Qyn: FPGA-Based ..."), so
they are rewritten to a spaced dash rather than deleted, preserving the
subtitle break that the colon was carrying.
"""

from __future__ import annotations

import re

# Illegal on Windows/FAT/exFAT, and rejected or mangled by Android and Drive.
# Colon and slash are handled separately because they carry meaning.
_STRIP_CHARS = re.compile(r'[*?"<>|\x00-\x1f]')
_SLASHES = re.compile(r"[\\/]+")
_COLONS = re.compile(r"\s*:\s*")
# Collapse the dashes that colon rewriting can pile up ("A: B: C").
_DASH_RUN = re.compile(r"(?:\s-\s*){2,}")
_WS = re.compile(r"\s+")

# Leave room for the "YYYY - " prefix, the ".pdf" suffix, and a possible
# " (2)" collision marker inside the common 255-byte limit.
MAX_TITLE_CHARS = 180


def sanitize_title(title: str) -> str:
    """Make a title safe to use as a filename component.

    Colons become " - " so "Test: this is a test" reads "Test - this is a test"
    rather than losing the break entirely.
    """
    title = _COLONS.sub(" - ", title)
    # A slash would create a directory level; a hyphen is the closest reading.
    title = _SLASHES.sub("-", title)
    title = _STRIP_CHARS.sub("", title)
    title = _DASH_RUN.sub(" - ", title)
    title = _WS.sub(" ", title).strip()
    # A leading or trailing dash is an artifact of the rewrite, never intended.
    title = title.strip("-").strip()
    # Trailing dots are silently dropped by Windows and confuse Drive.
    return title.rstrip(".").strip()


def truncate_title(title: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Shorten a title without cutting a word in half."""
    if len(title) <= limit:
        return title
    cut = title[:limit].rsplit(" ", 1)[0].rstrip(" -,;")
    return cut or title[:limit]


def pdf_filename(title: str, year: int | None, *, suffix: str = ".pdf") -> str:
    """Return ``{YYYY} - {Title}.pdf``.

    An unknown year yields "Unknown" rather than a bare dash, so the missing
    value is visible in a file listing instead of looking like a typo.
    """
    safe = truncate_title(sanitize_title(title))
    stamp = str(year) if year else "Unknown"
    return f"{stamp} - {safe}{suffix}"


def deduplicate_filename(name: str, existing: set[str]) -> str:
    """Append " (2)", " (3)", ... when a name is already taken."""
    if name not in existing:
        return name
    stem, dot, ext = name.rpartition(".")
    stem, ext = (stem, f"{dot}{ext}") if dot else (name, "")
    n = 2
    while f"{stem} ({n}){ext}" in existing:
        n += 1
    return f"{stem} ({n}){ext}"
