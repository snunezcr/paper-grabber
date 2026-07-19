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
