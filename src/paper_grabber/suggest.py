"""Suggest where a paper should be filed.

Two signals, because either alone is weak:

* **The folder's own name.** "Ion trap hardware" tells you what belongs in it
  without any history at all, which matters because a fresh ledger has none and
  a suggester that needs twenty examples before it says anything is useless on
  the day it ships.
* **What was filed there before.** Once a few papers are in a folder, their
  vocabulary describes it better than its name does -- "Spin qubit platforms"
  never mentions silicon or donors.

Matching is prefix-based rather than exact: "trap" should match "trapped" and
"photon" should match "photonic", which stemming would also do but with more
machinery and more surprises.

Tokens are weighted by how rare they are in the queue. Without that, a folder
whose name reduces to one common word -- "Quantum networks" becomes {network}
once the domain stopwords go -- scores a perfect match on any paper mentioning
neural networks. Tested against a real 59-folder taxonomy, unweighted scoring
sent a chemistry paper to "Quantum networks" and a toxicity-prediction paper to
"Quantum circuits". Rare words like "photonic" or "trap" now count for far more
than "data" or "application".

The title dominates and the abstract barely counts. A 1400-word abstract
mentions qubits, spins, photons and error correction somewhere regardless of
what the paper is about, so scoring against it saturates every folder at a
perfect match -- tested against a real queue, it sent a paper titled "Optimal
operating temperature for silicon spin quantum computing" to "Error
correction". The abstract can now only corroborate, never decide.

A weak match yields no suggestion. A wrong folder that gets tapped is worse
than an empty space, because it files the paper somewhere the user then has to
notice and undo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Words that appear in every quantum-computing paper and so distinguish
# nothing, plus ordinary English filler.
_STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "on", "in", "to", "with",
    "from", "using", "via", "into", "by", "at", "is", "are", "be", "as",
    "we", "our", "this", "that", "these", "those", "it", "its",
    "new", "novel", "study", "review", "analysis", "approach", "based",
    "towards", "toward", "paper", "results", "method", "methods",
    # Domain-wide terms: true of nearly everything here, so useless as a signal.
    "quantum", "computing", "computer", "computation", "computational",
}

# Below this, a folder name is barely matched and the suggestion would be a
# guess dressed up as a recommendation.
MIN_SCORE = 0.5

# A token shorter than this matches too much as a prefix ("ion" in "ionic",
# "iontophoresis"); require a real word before allowing prefix equivalence.
MIN_PREFIX = 4

# One matched word is not evidence. Most folder names here reduce to a single
# common term once the domain stopwords go -- "Quantum networks" becomes
# {network} -- and any paper mentioning neural networks then matched it
# perfectly. Requiring two distinct matches is what actually separates a real
# topic match from a coincidence of vocabulary.
MIN_MATCHED_TOKENS = 2


def build_idf(documents: list[str]) -> dict[str, float]:
    """Inverse document frequency over the papers currently in play.

    A word in half the queue says almost nothing about which folder a paper
    belongs to; one appearing twice is highly diagnostic.
    """
    import math

    total = max(1, len(documents))
    seen: dict[str, int] = {}
    for doc in documents:
        for t in tokens(doc):
            seen[t] = seen.get(t, 0) + 1
    return {t: math.log(total / n) + 1.0 for t, n in seen.items()}


def _weight(token: str, idf: dict[str, float] | None) -> float:
    if not idf:
        return 1.0
    # Unseen in the corpus means rare, so worth at least as much as the most
    # distinctive word we did see.
    return idf.get(token, max(idf.values(), default=1.0))


@dataclass
class Suggestion:
    folder_id: str
    folder_name: str
    score: float
    reason: str


def _fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    ).lower()


def tokens(text: str | None) -> set[str]:
    """Meaningful lowercase words, accents folded, plurals normalised."""
    words = _TOKEN_RE.findall(_fold(text or ""))
    out = set()
    for w in words:
        if len(w) < 3 or w in _STOPWORDS:
            continue
        # Crude singularisation: qubits -> qubit, platforms -> platform.
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _matches(a: str, b: str) -> bool:
    """Whether two tokens are the same word for our purposes."""
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= MIN_PREFIX and long.startswith(short)


def overlap(needles: set[str], haystack: set[str]) -> int:
    """How many of `needles` appear in `haystack`, allowing prefix matches."""
    return sum(1 for n in needles if any(_matches(n, h) for h in haystack))


def weighted_overlap(
    needles: set[str], haystack: set[str], idf: dict[str, float] | None
) -> tuple[float, float]:
    """(matched weight, total weight) for `needles` against `haystack`."""
    matched = total = 0.0
    for n in needles:
        w = _weight(n, idf)
        total += w
        if any(_matches(n, h) for h in haystack):
            matched += w
    return matched, total


# The abstract corroborates but cannot carry a suggestion on its own: at this
# weight, a folder found only in the abstract scores below MIN_SCORE.
ABSTRACT_WEIGHT = 0.3


def score_folder(
    strong: set[str],
    weak: set[str],
    folder_name: str,
    filed_before: list[str] | None = None,
    idf: dict[str, float] | None = None,
) -> tuple[float, str]:
    """Score one folder for one paper.

    `strong` is the title and venue, `weak` the abstract. Returns (score, why).
    """
    name_tokens = tokens(folder_name)
    name_score = 0.0
    if name_tokens:
        matched_in_title = overlap(name_tokens, strong)
        matched_anywhere = overlap(name_tokens, strong | weak)
        # A folder whose whole name is one word can still be suggested, but
        # only when the paper corroborates it elsewhere too.
        enough = matched_anywhere >= min(MIN_MATCHED_TOKENS, len(name_tokens)) and (
            matched_in_title >= MIN_MATCHED_TOKENS
            or (len(name_tokens) >= MIN_MATCHED_TOKENS and matched_in_title >= 1)
        )
        if enough:
            hit_strong, total = weighted_overlap(name_tokens, strong, idf)
            hit_weak, _ = weighted_overlap(name_tokens, weak, idf)
            if total:
                name_score = min(1.0, (hit_strong + ABSTRACT_WEIGHT * hit_weak) / total)

    history_score = 0.0
    for title in filed_before or []:
        prior = tokens(title)
        if not prior:
            continue
        # History is title-to-title, so it stays on the strong signal.
        if overlap(prior, strong) < MIN_MATCHED_TOKENS:
            continue
        matched, prior_total = weighted_overlap(prior, strong, idf)
        _, paper_total = weighted_overlap(strong, prior, idf)
        # Normalised by the shorter description, not the earlier one: a long
        # previous title would otherwise make every later match look weak.
        denominator = min(prior_total, paper_total)
        if denominator:
            history_score = max(history_score, min(1.0, matched / denominator))

    if name_score >= history_score:
        return name_score, "matches the folder name"
    return history_score, "similar to papers filed there"


def suggest_folder(
    *,
    title: str,
    abstract: str | None,
    venue: str | None,
    folders: list[dict],
    history: dict[str, list[str]] | None = None,
    idf: dict[str, float] | None = None,
    min_score: float = MIN_SCORE,
) -> Suggestion | None:
    """Best destination for a paper, or None when nothing matches well enough.

    `folders` are candidates as ``{"id", "name"}``; `history` maps folder id to
    the titles already filed there.
    """
    strong = tokens(title) | tokens(venue)
    weak = tokens(abstract)
    if not strong:
        return None

    history = history or {}
    ranked: list[Suggestion] = []
    for folder in folders:
        name = folder.get("name") or ""
        fid = folder.get("id") or ""
        if not fid or not name:
            continue
        score, reason = score_folder(strong, weak, name, history.get(fid), idf)
        ranked.append(Suggestion(fid, name, round(score, 3), reason))

    if not ranked:
        return None

    ranked.sort(key=lambda s: (-s.score, s.folder_name))
    best = ranked[0]
    if best.score < min_score:
        return None

    # A tie means the signal does not actually distinguish these folders, and
    # picking one arbitrarily would file papers somewhere on a coin flip.
    if len(ranked) > 1 and abs(ranked[1].score - best.score) < 1e-9:
        return None

    return best
