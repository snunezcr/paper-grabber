"""Durable record of what the pipeline has already seen and decided.

Two things are remembered:

* which Gmail messages have been processed, so a re-run -- or a laptop that
  woke late and caught two days of alerts at once -- does not reprocess them;
* every paper ever surfaced, with the accept/reject decision. Rejections are
  kept deliberately: the same paper reaches us from several alerts and over
  several weeks, and without a record it would be offered again every time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .clean import short_venue
from .models import AlertPaper, normalize_title


class Decision(str, Enum):
    """Where a paper stands in triage."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


# Papers with no saved-search attribution are bucketed under this label, which
# must match the frontend's alertOf() so the two agree on alert identity.
NO_ALERT = "(no alert)"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    processed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    decision    TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    decided_at  REAL
);

CREATE INDEX IF NOT EXISTS papers_decision ON papers (decision);

CREATE TABLE IF NOT EXISTS settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Where a paper should be filed. Held separately from the decision so that
# accepting and filing stay independent steps: triage is a fast yes/no pass,
# and destinations are chosen in bulk afterwards.
_DESTINATION_COLUMNS = """
ALTER TABLE papers ADD COLUMN dest_folder_id TEXT;
ALTER TABLE papers ADD COLUMN dest_folder_name TEXT;
ALTER TABLE papers ADD COLUMN staged_name TEXT;
ALTER TABLE papers ADD COLUMN drive_file_id TEXT;
ALTER TABLE papers ADD COLUMN uploaded_at REAL;
ALTER TABLE papers ADD COLUMN note TEXT;
"""

# Reading is a separate axis from filing: a paper can be in Drive but unread,
# or read but not yet filed. read_state is unread/reading/read (NULL, for rows
# predating this, is read as unread); pinned floats one to the top of the queue.
_READING_COLUMNS = """
ALTER TABLE papers ADD COLUMN read_state TEXT;
ALTER TABLE papers ADD COLUMN pinned INTEGER;
"""

SETTING_BASE_FOLDER_ID = "base_folder_id"
SETTING_BASE_FOLDER_NAME = "base_folder_name"


@dataclass
class LedgerPaper:
    """A paper as the ledger holds it."""

    key: str
    title: str
    payload: dict[str, Any]
    decision: Decision
    first_seen: float
    decided_at: float | None = None
    dest_folder_id: str | None = None
    dest_folder_name: str | None = None
    staged_name: str | None = None
    drive_file_id: str | None = None
    uploaded_at: float | None = None
    note: str | None = None
    read_state: str | None = None
    pinned: bool = False


def _scholar_pdf_url(url: str | None) -> bool:
    """Whether Scholar's own link is plainly a PDF we could fetch."""
    lowered = (url or "").lower()
    return lowered.endswith(".pdf") or "/pdf/" in lowered or "arxiv.org/abs/" in lowered


def can_read(p: LedgerPaper) -> bool:
    """Whether there is a PDF to open: a copy we hold, or somewhere to fetch one.

    Defined once so the reading queue and the card agree. A paper that cannot be
    opened is not something you can read, so it stays out of the reading list --
    which is the nudge to go get the file.
    """
    d = p.payload
    e = d.get("enrichment") or {}
    return bool(
        p.drive_file_id
        or p.staged_name
        or e.get("pdf_candidates")
        or e.get("pdf_url")
        or _scholar_pdf_url(d.get("url"))
    )


def paper_view(p: LedgerPaper) -> dict[str, Any]:
    """Flatten a ledger row into the fields every consumer wants.

    Enrichment supersedes the alert record where both have a value, and the
    abstract falls back to Scholar's snippet -- flagged, so a caller can say
    which it is rather than passing two truncated lines off as an abstract.

    Shared by the CLI and the web app deliberately: they showed different
    years for the same paper when each did this itself.
    """
    d = p.payload
    e = d.get("enrichment") or {}
    abstract = e.get("abstract")
    return {
        "key": p.key,
        "title": p.title,
        "authors": e.get("authors") or d.get("authors") or [],
        "venue": d.get("venue"),
        "year": e.get("year") or d.get("year"),
        "abstract": abstract or d.get("snippet"),
        "abstract_is_snippet": not abstract,
        # Kept for callers that want "wherever this paper is": the PDF when
        # one is known, the publisher page otherwise.
        "url": e.get("pdf_url") or d.get("url"),
        # The three distinct destinations, so a card can offer each rather
        # than collapsing them into one link whose target is a surprise.
        "pdf_url": e.get("pdf_url"),
        "doi_url": f"https://doi.org/{e['doi']}" if e.get("doi") else None,
        "source_url": e.get("landing_url") or d.get("url"),
        # What to call that link: the venue where known, else the host.
        "source_label": short_venue(
            d.get("venue"), e.get("landing_url") or d.get("url")
        ),
        "alert_query": d.get("alert_query"),
        "has_pdf": bool(e.get("pdf_url")) or bool(d.get("has_pdf_badge")),
        "doi": e.get("doi"),
        "dest_folder_id": p.dest_folder_id,
        "dest_folder_name": p.dest_folder_name,
        "note": p.note,
        "staged": p.staged_name is not None,
        "uploaded": p.drive_file_id is not None,
        # An open-access location the server could fetch from on its own,
        # separate from any copy we already hold. When false the paper can only
        # be filed by attaching a local PDF.
        "has_oa_pdf": bool(
            e.get("pdf_candidates")
            or e.get("pdf_url")
            or _scholar_pdf_url(d.get("url"))
        ),
        # Whether the reader can show it: a copy we hold, or somewhere to
        # fetch one from. has_pdf is not enough -- Scholar's [PDF] badge often
        # points at a landing page.
        "can_read": can_read(p),
        "uploaded_at": p.uploaded_at,
        # The reading axis: unread until you open it, reading once you do, read
        # when you say so. Pinned floats it to the top of the queue.
        "read_state": p.read_state or "unread",
        "pinned": bool(p.pinned),
        # Opens the PDF in Drive, which is where reading and annotation happen.
        "drive_url": (
            f"https://drive.google.com/file/d/{p.drive_file_id}/view"
            if p.drive_file_id
            else None
        ),
    }


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release.

        Existing ledgers must survive an upgrade: a user who has already
        triaged a hundred papers should not lose them to a schema change.
        """
        have = {r[1] for r in self._db.execute("PRAGMA table_info(papers)")}
        for block in (_DESTINATION_COLUMNS, _READING_COLUMNS):
            for statement in block.strip().split(";"):
                statement = statement.strip()
                if not statement:
                    continue
                column = statement.rsplit(" ", 2)[-2]
                if column not in have:
                    self._db.execute(statement)

    # --- processed messages ---------------------------------------------------

    def message_seen(self, message_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_message(self, message_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO messages (message_id, processed_at) VALUES (?, ?)",
            (message_id, time.time()),
        )
        self._db.commit()

    def seen_message_ids(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT message_id FROM messages")}

    # --- settings -------------------------------------------------------------

    def set_setting(self, name: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (name, value)
        )
        self._db.commit()

    def get_setting(self, name: str, default: str | None = None) -> str | None:
        row = self._db.execute(
            "SELECT value FROM settings WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else default

    def clear_setting(self, name: str) -> None:
        self._db.execute("DELETE FROM settings WHERE name = ?", (name,))
        self._db.commit()

    # --- papers ---------------------------------------------------------------

    def record(self, paper: AlertPaper) -> bool:
        """Register a paper if new. Returns True when it was not seen before.

        An already-known paper is left exactly as it is: re-recording must not
        resurrect something the user already rejected.
        """
        key = normalize_title(paper.title)
        existing = self._db.execute(
            "SELECT 1 FROM papers WHERE key = ?", (key,)
        ).fetchone()
        if existing:
            return False

        self._db.execute(
            "INSERT INTO papers (key, title, payload, decision, first_seen)"
            " VALUES (?, ?, ?, ?, ?)",
            (key, paper.title, json.dumps(paper.to_dict()), Decision.PENDING.value, time.time()),
        )
        self._db.commit()
        return True

    def attach_enrichment(self, key: str, enrichment: dict[str, Any]) -> bool:
        """Store OpenAlex results alongside the alert record.

        Kept in the same payload rather than a second table: the triage UI
        wants one object, and enrichment is strictly additive metadata about a
        paper we already have.
        """
        row = self._db.execute(
            "SELECT payload FROM papers WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return False
        payload = json.loads(row[0])
        payload["enrichment"] = enrichment
        self._db.execute(
            "UPDATE papers SET payload = ? WHERE key = ?", (json.dumps(payload), key)
        )
        self._db.commit()
        return True

    def needing_abstract(self) -> list[LedgerPaper]:
        """Undecided papers that have been enriched but still have no abstract.

        Distinct from needing_enrichment: these already carry a DOI and OA
        data, so re-running the full lookup would spend budget to learn
        nothing. Only the abstract is missing.
        """
        return [
            p
            for p in self.pending()
            if p.payload.get("enrichment")
            and not (p.payload["enrichment"] or {}).get("abstract")
        ]

    def needing_enrichment(self) -> list[LedgerPaper]:
        """Pending papers that have not been looked up yet."""
        return [p for p in self.pending() if not p.payload.get("enrichment")]

    def decide(self, title: str, decision: Decision) -> None:
        self.decide_by_key(normalize_title(title), decision)

    def decide_by_key(self, key: str, decision: Decision) -> bool:
        """Record a decision against a stored key. False if no such paper."""
        cur = self._db.execute(
            "UPDATE papers SET decision = ?, decided_at = ? WHERE key = ?",
            (decision.value, time.time(), key),
        )
        self._db.commit()
        return cur.rowcount > 0

    def set_note(self, key: str, note: str | None) -> bool:
        """Attach a note to a paper, or clear it with an empty string.

        Held here rather than sent to Drive immediately: a note is written
        while filing, and the file it belongs to does not exist in Drive until
        the upload happens.
        """
        text = (note or "").strip() or None
        cur = self._db.execute(
            "UPDATE papers SET note = ? WHERE key = ?", (text, key)
        )
        self._db.commit()
        return cur.rowcount > 0

    def set_destination(self, key: str, folder_id: str, folder_name: str) -> bool:
        """Choose where an accepted paper will be filed."""
        cur = self._db.execute(
            "UPDATE papers SET dest_folder_id = ?, dest_folder_name = ? WHERE key = ?",
            (folder_id, folder_name, key),
        )
        self._db.commit()
        return cur.rowcount > 0

    def accepted(self, *, filed: bool | None = None) -> list[LedgerPaper]:
        """Accepted papers that are not yet in Drive.

        Papers already uploaded are excluded entirely: they belong to the
        processed list, not the filing queue, and showing them in both makes
        "ready to upload" meaningless.

        `filed=False` is the queue awaiting a destination; `filed=True` is what
        upload will act on.
        """
        sql = (
            "SELECT key, title, payload, decision, first_seen, decided_at,"
            " dest_folder_id, dest_folder_name, staged_name, drive_file_id, uploaded_at, note"
            " FROM papers WHERE decision = ? AND drive_file_id IS NULL"
        )
        if filed is True:
            sql += " AND dest_folder_id IS NOT NULL"
        elif filed is False:
            sql += " AND dest_folder_id IS NULL"
        sql += " ORDER BY decided_at"
        rows = self._db.execute(sql, (Decision.ACCEPTED.value,)).fetchall()
        return [self._row(r) for r in rows]

    def rejected(self) -> list[LedgerPaper]:
        """Rejected papers, most recently rejected first."""
        rows = self._db.execute(
            "SELECT key, title, payload, decision, first_seen, decided_at,"
            " dest_folder_id, dest_folder_name, staged_name, drive_file_id, uploaded_at, note"
            " FROM papers WHERE decision = ? ORDER BY decided_at DESC",
            (Decision.REJECTED.value,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def processed(self) -> list[LedgerPaper]:
        """Papers safely in Drive, most recent first."""
        rows = self._db.execute(
            "SELECT key, title, payload, decision, first_seen, decided_at,"
            " dest_folder_id, dest_folder_name, staged_name, drive_file_id, uploaded_at, note"
            " FROM papers WHERE drive_file_id IS NOT NULL"
            " ORDER BY uploaded_at DESC",
        ).fetchall()
        return [self._row(r) for r in rows]

    def reading(self) -> list[LedgerPaper]:
        """Kept papers you can actually open, newest decision first.

        Spans the whole accepted set -- unfiled, filed, and already in Drive --
        because whether a paper is read has nothing to do with whether its PDF
        has been archived. But a paper with no PDF to open is not something you
        can read, so it waits in Filing until it has a file rather than sitting
        unreadable in the queue.
        """
        rows = self._db.execute(
            "SELECT key, title, payload, decision, first_seen, decided_at,"
            " dest_folder_id, dest_folder_name, staged_name, drive_file_id,"
            " uploaded_at, note, read_state, pinned"
            " FROM papers WHERE decision = ? ORDER BY decided_at DESC",
            (Decision.ACCEPTED.value,),
        ).fetchall()
        return [p for p in (self._row(r) for r in rows) if can_read(p)]

    def set_read_state(self, key: str, state: str) -> bool:
        """Move a paper along the reading axis: unread, reading, or read."""
        if state not in ("unread", "reading", "read"):
            raise ValueError(f"unknown read state: {state}")
        cur = self._db.execute(
            "UPDATE papers SET read_state = ? WHERE key = ?", (state, key)
        )
        self._db.commit()
        return cur.rowcount > 0

    def set_pinned(self, key: str, pinned: bool) -> bool:
        """Pin (or unpin) a paper to the top of the reading queue."""
        cur = self._db.execute(
            "UPDATE papers SET pinned = ? WHERE key = ?", (1 if pinned else 0, key)
        )
        self._db.commit()
        return cur.rowcount > 0

    # --- fetch and upload progress ---------------------------------------------

    def set_staged(self, key: str, staged_name: str | None) -> bool:
        """Record the filename a paper was staged under, or clear it.

        Stored rather than recomputed: regenerating the name at upload time
        breaks as soon as enrichment revises a title, and the file on disk then
        matches nothing.
        """
        cur = self._db.execute(
            "UPDATE papers SET staged_name = ? WHERE key = ?", (staged_name, key)
        )
        self._db.commit()
        return cur.rowcount > 0

    def set_uploaded(self, key: str, drive_file_id: str) -> bool:
        """Mark a paper as safely in Drive and no longer staged."""
        cur = self._db.execute(
            "UPDATE papers SET drive_file_id = ?, uploaded_at = ?, staged_name = NULL"
            " WHERE key = ?",
            (drive_file_id, time.time(), key),
        )
        self._db.commit()
        return cur.rowcount > 0

    def clear_uploaded(self, key: str) -> bool:
        """Forget that a paper reached Drive, keeping its destination.

        Used when the file is found to be gone: the paper returns to the
        filing queue already pointed at the folder it was meant for, so it can
        simply be uploaded again.
        """
        cur = self._db.execute(
            "UPDATE papers SET drive_file_id = NULL, uploaded_at = NULL WHERE key = ?",
            (key,),
        )
        self._db.commit()
        return cur.rowcount > 0

    def awaiting_download(self) -> list[LedgerPaper]:
        """Accepted papers not yet staged and not already in Drive."""
        rows = self._db.execute(
            f"SELECT key, title, payload, decision, first_seen, decided_at, dest_folder_id, dest_folder_name, staged_name, drive_file_id, uploaded_at, note FROM papers"
            " WHERE decision = ? AND staged_name IS NULL AND drive_file_id IS NULL"
            " ORDER BY decided_at",
            (Decision.ACCEPTED.value,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def awaiting_upload(self) -> list[LedgerPaper]:
        """Staged papers with a destination chosen, ready to send."""
        rows = self._db.execute(
            f"SELECT key, title, payload, decision, first_seen, decided_at, dest_folder_id, dest_folder_name, staged_name, drive_file_id, uploaded_at, note FROM papers"
            " WHERE decision = ? AND staged_name IS NOT NULL"
            " AND dest_folder_id IS NOT NULL AND drive_file_id IS NULL"
            " ORDER BY decided_at",
            (Decision.ACCEPTED.value,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, key: str) -> LedgerPaper | None:
        row = self._db.execute(
            "SELECT key, title, payload, decision, first_seen, decided_at, dest_folder_id, dest_folder_name, staged_name, drive_file_id, uploaded_at, note, read_state, pinned"
            " FROM papers WHERE key = ?",
            (key,),
        ).fetchone()
        return self._row(row) if row else None

    def decision_for(self, title: str) -> Decision | None:
        row = self._db.execute(
            "SELECT decision FROM papers WHERE key = ?", (normalize_title(title),)
        ).fetchone()
        return Decision(row[0]) if row else None

    def known(self, title: str) -> bool:
        return self.decision_for(title) is not None

    def pending(self) -> list[LedgerPaper]:
        """Papers awaiting a decision, oldest first."""
        rows = self._db.execute(
            "SELECT key, title, payload, decision, first_seen, decided_at, dest_folder_id, dest_folder_name, staged_name, drive_file_id, uploaded_at, note"
            " FROM papers WHERE decision = ? ORDER BY first_seen",
            (Decision.PENDING.value,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        """Papers by decision, plus how many have a destination chosen.

        "filed" is not a fourth decision -- it is a subset of accepted -- but
        the UI wants both in one payload, so it rides along here.
        """
        rows = self._db.execute(
            "SELECT decision, COUNT(*) FROM papers GROUP BY decision"
        ).fetchall()
        counts = {d: n for d, n in rows}
        counts["filed"] = self._db.execute(
            "SELECT COUNT(*) FROM papers WHERE decision = ? AND dest_folder_id IS NOT NULL"
            " AND drive_file_id IS NULL",
            (Decision.ACCEPTED.value,),
        ).fetchone()[0]
        counts["processed"] = self._db.execute(
            "SELECT COUNT(*) FROM papers WHERE drive_file_id IS NOT NULL"
        ).fetchone()[0]
        # The reading queue's depth: readable kept papers not yet read. Counted
        # through reading() so "has a PDF" stays defined in exactly one place,
        # matching what the Reading tab actually shows.
        counts["unread"] = sum(
            1 for p in self.reading() if (p.read_state or "unread") == "unread"
        )
        return counts

    def alert_stats(self) -> dict[str, dict[str, int]]:
        """Per-alert lifetime tallies by decision.

        Keyed by the same alert identity paper_view exposes -- the payload's
        alert_query, with a missing or empty one bucketed as NO_ALERT. Lets the
        sidebar show how selective each saved search has been, so a noisy one
        can be pruned. A paper keeps its decision after being filed or
        uploaded, so an accepted paper counts as accepted for good.
        """
        rows = self._db.execute(
            "SELECT json_extract(payload, '$.alert_query') AS alert,"
            " decision, COUNT(*) FROM papers GROUP BY alert, decision"
        ).fetchall()
        stats: dict[str, dict[str, int]] = {}
        for alert, decision, n in rows:
            bucket = stats.setdefault(
                alert or NO_ALERT, {"accepted": 0, "rejected": 0, "pending": 0}
            )
            if decision in bucket:
                bucket[decision] = n
        return stats

    @staticmethod
    def _row(r) -> LedgerPaper:
        return LedgerPaper(
            key=r[0],
            title=r[1],
            payload=json.loads(r[2]),
            decision=Decision(r[3]),
            first_seen=r[4],
            decided_at=r[5],
            dest_folder_id=r[6] if len(r) > 6 else None,
            dest_folder_name=r[7] if len(r) > 7 else None,
            staged_name=r[8] if len(r) > 8 else None,
            drive_file_id=r[9] if len(r) > 9 else None,
            uploaded_at=r[10] if len(r) > 10 else None,
            note=r[11] if len(r) > 11 else None,
            read_state=r[12] if len(r) > 12 else None,
            pinned=bool(r[13]) if len(r) > 13 else False,
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
