"""On-disk cache for OpenAlex lookups.

OpenAlex charges per request against a daily budget, so re-running the pipeline
over papers already seen is not merely slow, it is expensive and can exhaust
the allowance mid-run. Bibliographic metadata is also close to immutable: a
paper's DOI and abstract do not change once assigned.

Negative results are cached too, but briefly. "Not in OpenAlex" is usually
permanent for a conference poster and temporary for a preprint indexed next
week, and re-asking every day is exactly what the budget cannot afford.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import normalize_title

# A matched record is effectively permanent; re-checking wastes budget.
HIT_TTL_SECONDS = 90 * 24 * 3600
# A miss may become a hit once indexing catches up, so retry within the week.
MISS_TTL_SECONDS = 7 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    matched    INTEGER NOT NULL,
    fetched_at REAL NOT NULL
);
"""


class LookupCache:
    """Keyed by normalized title -- the same key the deduper uses."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute(_SCHEMA)
        self._db.commit()

    @staticmethod
    def key_for(title: str) -> str:
        return normalize_title(title)

    def get(self, title: str) -> dict[str, Any] | None:
        """Return the cached payload, or None when absent or stale."""
        row = self._db.execute(
            "SELECT payload, matched, fetched_at FROM lookups WHERE key = ?",
            (self.key_for(title),),
        ).fetchone()
        if row is None:
            return None

        payload, matched, fetched_at = row
        ttl = HIT_TTL_SECONDS if matched else MISS_TTL_SECONDS
        if time.time() - fetched_at > ttl:
            return None
        return json.loads(payload)

    def put(self, title: str, payload: dict[str, Any], *, matched: bool) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO lookups (key, payload, matched, fetched_at)"
            " VALUES (?, ?, ?, ?)",
            (self.key_for(title), json.dumps(payload), int(matched), time.time()),
        )
        self._db.commit()

    def __len__(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM lookups").fetchone()[0]

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "LookupCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
