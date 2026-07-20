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

from .models import AlertPaper, normalize_title


class Decision(str, Enum):
    """Where a paper stands in triage."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


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
        "url": e.get("pdf_url") or d.get("url"),
        "alert_query": d.get("alert_query"),
        "has_pdf": bool(e.get("pdf_url")) or bool(d.get("has_pdf_badge")),
        "doi": e.get("doi"),
        "dest_folder_id": p.dest_folder_id,
        "dest_folder_name": p.dest_folder_name,
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
        for statement in _DESTINATION_COLUMNS.strip().split(";"):
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

    def set_destination(self, key: str, folder_id: str, folder_name: str) -> bool:
        """Choose where an accepted paper will be filed."""
        cur = self._db.execute(
            "UPDATE papers SET dest_folder_id = ?, dest_folder_name = ? WHERE key = ?",
            (folder_id, folder_name, key),
        )
        self._db.commit()
        return cur.rowcount > 0

    def accepted(self, *, filed: bool | None = None) -> list[LedgerPaper]:
        """Accepted papers, optionally split by whether a destination is set.

        `filed=False` is the filing queue; `filed=True` is what upload will act
        on.
        """
        sql = (
            "SELECT key, title, payload, decision, first_seen, decided_at,"
            " dest_folder_id, dest_folder_name FROM papers WHERE decision = ?"
        )
        if filed is True:
            sql += " AND dest_folder_id IS NOT NULL"
        elif filed is False:
            sql += " AND dest_folder_id IS NULL"
        sql += " ORDER BY decided_at"
        rows = self._db.execute(sql, (Decision.ACCEPTED.value,)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, key: str) -> LedgerPaper | None:
        row = self._db.execute(
            "SELECT key, title, payload, decision, first_seen, decided_at,"
            " dest_folder_id, dest_folder_name FROM papers WHERE key = ?",
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
            "SELECT key, title, payload, decision, first_seen, decided_at,"
            " dest_folder_id, dest_folder_name"
            " FROM papers WHERE decision = ? ORDER BY first_seen",
            (Decision.PENDING.value,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT decision, COUNT(*) FROM papers GROUP BY decision"
        ).fetchall()
        return {d: n for d, n in rows}

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
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
