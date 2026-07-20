"""Repair the title mangling Google Scholar introduces.

Scholar's index carries titles that were scraped from PDFs and BibTeX, and it
propagates the damage into alert emails: unrendered LaTeX accent commands,
stray quotation marks, leftover braces. This module undoes the damage that can
be undone *unambiguously*, and deliberately leaves alone the damage that
cannot -- a wrong "fix" corrupts the filename and the enrichment query at once.
"""

from __future__ import annotations

import re
import unicodedata

# LaTeX accent commands map onto Unicode combining marks, so one table plus an
# NFC normalization handles every base letter without enumerating them.
_COMBINING = {
    "'": "́",  # acute
    "`": "̀",  # grave
    "^": "̂",  # circumflex
    '"': "̈",  # diaeresis
    "~": "̃",  # tilde
    "=": "̄",  # macron
    ".": "̇",  # dot above
    "c": "̧",  # cedilla
    "v": "̌",  # caron
    "u": "̆",  # breve
    "H": "̋",  # double acute
    "r": "̊",  # ring above
}

# Scholar often inserts a space where the brace group used to be, so
# "Schr\" odinger" and "Schr\"{o}dinger" must both resolve.
_ACCENT_RE = re.compile(
    r"\\([\'`^\"~=.cvuHr])\s*\{?([A-Za-z])\}?"
)

# A brace group with nothing but letters inside is BibTeX case-protection
# ("{DNA}"); the braces are noise once the title is plain text.
_CASE_BRACES_RE = re.compile(r"\{([^{}]*)\}")

_WS_RE = re.compile(r"\s+")


def _apply_accent(m: re.Match[str]) -> str:
    return m.group(2) + _COMBINING[m.group(1)]


def unmangle_latex(text: str) -> str:
    """Resolve LaTeX accent commands into precomposed characters.

    ``Schr\\" odinger`` becomes ``Schrödinger``. Commands this table does not
    know are left untouched rather than mangled further.
    """
    text = _ACCENT_RE.sub(_apply_accent, text)
    text = _CASE_BRACES_RE.sub(r"\1", text)
    return unicodedata.normalize("NFC", text)


def strip_stray_quotes(text: str) -> str:
    """Drop unbalanced leading/trailing quote marks.

    Scholar emits titles like ``" Navigating the Quantum Revolution ...`` with
    an opening quote and no closing one. A *balanced* pair is meaningful (a
    quoted phrase in the title) and is preserved.
    """
    for quote in ('"', "'", "“", "”"):
        # Only strip when the mark is unpaired; an even count is intentional.
        if text.count(quote) % 2 == 1:
            if text.startswith(quote):
                text = text[1:]
            elif text.endswith(quote):
                text = text[:-1]
    # Curly quotes pair with each other, not themselves.
    if text.startswith("“") and "”" not in text:
        text = text[1:]
    return text.strip()


def clean_title(title: str) -> str:
    """Normalize a Scholar title for display, filenames, and lookup.

    Intentionally NOT handled: spurious spaces after a period, as in
    ``Implementation in. NET 9``. The correct reading is ``.NET``, but no rule
    distinguishes that from a legitimate sentence break ("... was tested. NASA
    reported ..."), and a wrong join corrupts the title silently. Left as-is.
    """
    title = unmangle_latex(title)
    title = strip_stray_quotes(title)
    return _WS_RE.sub(" ", title).strip()


# --- venue labels -------------------------------------------------------------

# Scholar truncates venue names mid-phrase ("ACM Transactions on"), leaving a
# dangling function word that reads like a mistake in a short label.
_DANGLING = {
    "in", "on", "of", "and", "the", "for", "at", "to", "a", "an", "&",
    "with", "from",
}

_ARXIV_RE = re.compile(r"^ar[xX]iv\b.*", re.IGNORECASE)

MAX_VENUE_CHARS = 32


def short_venue(venue: str | None, url: str | None = None, *, limit: int = MAX_VENUE_CHARS) -> str:
    """A short, readable name for where a paper lives.

    Falls back to the host when there is no venue -- a quarter of alert
    results carry none, and "researchgate.net" says more than "Publisher".
    """
    label = _tidy_venue(venue, limit)
    if label:
        return label

    host = _host_of(url)
    return host or "Publisher"


def _tidy_venue(venue: str | None, limit: int) -> str | None:
    if not venue:
        return None
    text = _WS_RE.sub(" ", venue).strip(" ,.;:")
    if not text:
        return None

    # Every arXiv variant is just arXiv; the identifier adds nothing here.
    if _ARXIV_RE.match(text):
        return "arXiv"

    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]

    # Drop a trailing function word, whether Scholar truncated it or we did.
    words = text.split()
    while words and words[-1].lower().strip(",.") in _DANGLING:
        words.pop()
    return " ".join(words).strip(" ,.;:") or None


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or None
