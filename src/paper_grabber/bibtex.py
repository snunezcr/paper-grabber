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

# Characters with a name in LaTeX. Applied before accent decomposition, so the
# letters that have their own command -- aa, o, ss -- take it rather than being
# rebuilt from a base and a mark.
_SYMBOLS = {
    # Punctuation that Scholar and publishers emit as Unicode. The plain
    # hyphen is the most common non-ASCII character in a real queue.
    "\u2010": "-", "\u2011": "-", "\u2212": "-",
    "\u2012": "--", "\u2013": "--", "\u2014": "---",
    "\u2018": "`", "\u2019": "'", "\u201c": "``", "\u201d": "''",
    "\u2032": "'", "\u2033": "''",
    "\u2026": r"\ldots{}", "\u00a0": " ", "\u200b": "",
    # Symbols.
    "\u00d7": r"$\times$", "\u00b1": r"$\pm$", "\u00b7": r"$\cdot$",
    "\u00b0": r"\textdegree{}", "\u2265": r"$\geq$", "\u2264": r"$\leq$",
    "\u2260": r"$\neq$", "\u2248": r"$\approx$", "\u221e": r"$\infty$",
    "\u2192": r"$\rightarrow$", "\u2190": r"$\leftarrow$",
    "\u00ae": r"\textregistered{}", "\u00a9": r"\copyright{}",
    "\u2122": r"\texttrademark{}",
    # Letters with their own command rather than an accent.
    "\u00e5": r"\aa{}", "\u00c5": r"\AA{}",
    "\u00e6": r"\ae{}", "\u00c6": r"\AE{}",
    "\u0153": r"\oe{}", "\u0152": r"\OE{}",
    "\u00f8": r"\o{}", "\u00d8": r"\O{}",
    "\u00df": r"\ss{}", "\u0142": r"\l{}", "\u0141": r"\L{}",
    "\u0111": r"\dj{}", "\u0110": r"\DJ{}",
    "\u00f0": r"\dh{}", "\u00de": r"\TH{}", "\u00fe": r"\th{}",
    "\u0131": r"\i{}",
    # Greek, which arXiv titles use freely.
    "\u03b1": r"$\alpha$", "\u03b2": r"$\beta$", "\u03b3": r"$\gamma$",
    "\u03b4": r"$\delta$", "\u03b5": r"$\epsilon$", "\u03b8": r"$\theta$",
    "\u03bb": r"$\lambda$", "\u03bc": r"$\mu$", "\u00b5": r"$\mu$",
    "\u03c0": r"$\pi$", "\u03c1": r"$\rho$", "\u03c3": r"$\sigma$",
    "\u03c4": r"$\tau$", "\u03c6": r"$\phi$", "\u03c8": r"$\psi$",
    "\u03c9": r"$\omega$", "\u0394": r"$\Delta$", "\u03a9": r"$\Omega$",
}

# Combining marks, as LaTeX accent commands.
_COMBINING = {
    "\u0301": "'", "\u0300": "`", "\u0302": "^", "\u0308": '"',
    "\u0303": "~", "\u0304": "=", "\u0307": ".", "\u0327": "c",
    "\u030c": "v", "\u0306": "u", "\u030b": "H", "\u030a": "r",
    "\u0328": "k", "\u0323": "d", "\u0331": "b",
}
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


def to_latex(text: str) -> str:
    """Render text as portable LaTeX.

    Escapes BibTeX's own metacharacters, then rewrites Unicode punctuation and
    accented letters as LaTeX commands: "Schrödinger" becomes
    ``Schr\\"{o}dinger``. UTF-8 works in a modern biber pipeline but not in a
    classic BibTeX one, and an entry that only compiles on the author's own
    machine is not much use.

    Order matters. Escaping runs first, so a literal backslash in the source is
    neutralised before this function starts adding its own; the commands it
    then emits are left alone.

    Characters with no mapping are passed through rather than dropped -- a
    missing glyph is a compile error the user can see and fix, where a silently
    deleted one corrupts a name.
    """
    out = escape(text)
    out = "".join(_SYMBOLS.get(ch, ch) for ch in out)

    # Decompose so an accented letter becomes a base plus a mark, then rebuild
    # each pair as an accent command.
    result: list[str] = []
    for ch in unicodedata.normalize("NFD", out):
        cmd = _COMBINING.get(ch)
        if cmd and result:
            base = result.pop()
            result.append(f"\\{cmd}{{{base}}}")
        else:
            result.append(ch)
    return "".join(result)


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
        fields.append(
            ("author", " and ".join(to_latex(format_author(a)) for a in authors))
        )
    # Braced so BibTeX preserves the capitalisation of proper nouns.
    fields.append(("title", "{" + to_latex(title) + "}"))
    if venue:
        fields.append(
            ("booktitle" if kind == "inproceedings" else "journal", to_latex(venue))
        )
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
