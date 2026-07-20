"""Render a paper as a BibTeX entry.

Built from what enrichment already knows, which is never quite a full record:
Scholar gives initials, OpenAlex gives full names, and plenty of papers have
no venue at all. The entry is therefore best-effort but always syntactically
valid -- a broken entry breaks the whole .bib file it is pasted into, which is
far worse than a sparse one.
"""

from __future__ import annotations

import re
import unicodedata

# BibTeX treats these as markup; left raw they corrupt the entry.
_ESCAPE = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9]+")

# Words too generic to identify a paper in a citation key.
_STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "on", "in", "to", "with", "from",
    "using", "towards", "toward", "via", "into", "by", "at", "is", "are",
    "new", "novel", "study", "review", "analysis", "approach",
}

_CONFERENCE_HINTS = ("conference", "proceedings", "symposium", "workshop", "congress")


def escape(text: str) -> str:
    """Escape BibTeX's special characters."""
    return "".join(_ESCAPE.get(ch, ch) for ch in text)


def _ascii_fold(text: str) -> str:
    """Strip accents, for citation keys only -- never for field values."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def format_author(name: str) -> str:
    """Render one name as BibTeX's "Surname, Given" form.

    Scholar supplies "AM Hafiz" and OpenAlex "Abdul Mueed Hafiz"; both are
    treated as given-names-then-surname, which is right for the overwhelming
    majority of what these sources return.
    """
    parts = _WS_RE.sub(" ", name).strip().split(" ")
    if len(parts) < 2:
        return name.strip()
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def citation_key(authors: list[str], year: int | None, title: str) -> str:
    """A readable, mostly-unique key: surname + year + first real title word."""
    surname = ""
    if authors:
        surname = _NONWORD_RE.sub("", _ascii_fold(authors[0]).lower().split(" ")[-1])

    word = ""
    for candidate in _NONWORD_RE.sub(" ", _ascii_fold(title).lower()).split():
        if candidate not in _STOPWORDS and len(candidate) > 2:
            word = candidate
            break

    key = f"{surname}{year or ''}{word}"
    return key or "paper"


def entry_type(venue: str | None, url: str | None) -> str:
    """Pick the entry type from what little we know about where it appeared."""
    lowered = (venue or "").lower()
    if any(hint in lowered for hint in _CONFERENCE_HINTS):
        return "inproceedings"
    if venue:
        return "article"
    # No venue: a preprint or a page somewhere. @misc is the honest choice --
    # @article with no journal is a malformed record.
    return "misc"


def to_bibtex(view: dict) -> str:
    """Render a paper_view dict as a BibTeX entry."""
    title = (view.get("title") or "Untitled").strip()
    authors = [a for a in (view.get("authors") or []) if a]
    year = view.get("year")
    venue = view.get("venue")
    kind = entry_type(venue, view.get("source_url"))

    fields: list[tuple[str, str]] = []
    if authors:
        fields.append(("author", " and ".join(format_author(a) for a in authors)))
    # Braced so BibTeX preserves the capitalisation of proper nouns.
    fields.append(("title", "{" + escape(title) + "}"))
    if venue:
        fields.append(("booktitle" if kind == "inproceedings" else "journal", escape(venue)))
    if year:
        fields.append(("year", str(year)))
    if view.get("doi"):
        fields.append(("doi", escape(view["doi"])))

    # A URL is only worth recording when it is not merely the DOI resolver.
    url = view.get("pdf_url") or view.get("source_url")
    if url and not (view.get("doi") and "doi.org" in url):
        fields.append(("url", escape(url)))

    key = citation_key(authors, year, title)
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{kind}{{{key},\n{body}\n}}"


def to_bib_file(views: list[dict]) -> str:
    """Render several papers as one .bib file, with keys made unique."""
    seen: dict[str, int] = {}
    entries = []
    for view in views:
        entry = to_bibtex(view)
        key = entry.split("{", 1)[1].split(",", 1)[0]
        if key in seen:
            seen[key] += 1
            # BibTeX silently uses the last of a duplicated key, losing the
            # earlier entry; suffixes keep every paper addressable.
            # Second occurrence gets "a", third "b", and so on.
            suffix = chr(ord("a") + seen[key] - 2)
            entry = entry.replace(f"{{{key},", f"{{{key}{suffix},", 1)
        else:
            seen[key] = 1
        entries.append(entry)
    return "\n\n".join(entries) + "\n"
