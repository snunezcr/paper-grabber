"""Core data types shared across the pipeline."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any

# Scholar usually separates the author list from the venue with NBSP +
# hyphen, but sometimes emits a plain ASCII hyphen; the NBSP is normalized
# before the split so one rule covers both.
_YEAR_RE = re.compile(r",\s*((?:19|20|21)\d{2})\s*$")
# A byline can end at the year with no venue at all ("L Muller - 2026").
_BARE_YEAR_RE = re.compile(r"^((?:19|20|21)\d{2})$")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_title(title: str) -> str:
    """Fold a title to a dedupe key.

    The same paper reaches us from several alerts with different bolding,
    casing, and stray punctuation, so the key strips all of that. Accents are
    decomposed rather than dropped, keeping non-ASCII titles distinguishable.
    """
    folded = unicodedata.normalize("NFKD", title).casefold()
    folded = _PUNCT_RE.sub(" ", folded)
    return _WS_RE.sub(" ", folded).strip()


@dataclass
class AlertPaper:
    """One result as it appeared in a Scholar alert email.

    This is strictly what the email told us. Enrichment (abstract, DOI, OA
    location) lands on a separate record so a re-parse never clobbers it.
    """

    title: str
    authors: list[str] = field(default_factory=list)
    venue: str | None = None
    year: int | None = None
    url: str | None = None
    snippet: str | None = None
    has_pdf_badge: bool = False
    alert_query: str | None = None
    alert_id: str | None = None
    message_id: str | None = None
    position: int | None = None

    @property
    def dedupe_key(self) -> str:
        return normalize_title(self.title)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dedupe_key"] = self.dedupe_key
        return d


def split_author_venue(line: str) -> tuple[list[str], str | None, int | None]:
    """Split Scholar's byline into authors, venue, and year.

    Handles the three shapes Scholar emits:
      "A Hafiz, M Hassaballah\xa0- Image and Vision Computing, 2026"
      "A De Lorenzis\xa0- arXiv preprint arXiv:2607.13699, 2026"
      "H Ishida, A Elsokary, MJC Henshaw, S Ji"        (no venue at all)
    """
    line = line.replace("\xa0", " ").strip()
    # Re-find the separator post-NBSP-normalization; a bare " - " inside a
    # venue name is common, so only the first occurrence splits.
    if " - " in line:
        author_part, venue_part = line.split(" - ", 1)
    else:
        author_part, venue_part = line, ""

    authors = [a.strip() for a in author_part.split(",") if a.strip()]
    # A trailing ellipsis means Scholar truncated the list, not a real author.
    if authors and authors[-1].endswith("\u2026"):
        authors[-1] = authors[-1].rstrip("\u2026").strip()
        authors = [a for a in authors if a]

    year: int | None = None
    venue: str | None = None
    if venue_part:
        venue_part = venue_part.strip()
        bare = _BARE_YEAR_RE.match(venue_part)
        if bare:
            # "L Muller - 2026": the whole venue field is the year. Without
            # this the year is lost and the filename becomes "???? - Title".
            year = int(bare.group(1))
            venue_part = ""
        else:
            m = _YEAR_RE.search(venue_part)
            if m:
                year = int(m.group(1))
                venue_part = venue_part[: m.start()]
        venue = venue_part.replace("\u2026", "").strip(" ,") or None

    return authors, venue, year
