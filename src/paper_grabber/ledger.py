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
"""


@dataclass
class LedgerPaper:
    """A paper as the ledger holds it."""

    key: str
    title: str
    payload: dict[str, Any]
    decision: Decision
    first_seen: float
    decided_at: float | None = None


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

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

    def decide(self, title: str, decision: Decision) -> None:
        key = normalize_title(title)
        self._db.execute(
            "UPDATE papers SET decision = ?, decided_at = ? WHERE key = ?",
            (decision.value, time.time(), key),
        )
        self._db.commit()

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
            "SELECT key, title, payload, decision, first_seen, decided_at"
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
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
