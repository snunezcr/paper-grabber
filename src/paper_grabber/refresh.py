"""On-demand mail check, run in the background.

The daily timer covers the unattended case. This is for the moment you know an
alert has arrived and do not want to wait until tomorrow.

It runs on a worker thread rather than inside the request because a check can
take tens of seconds -- IMAP or Gmail, then an OpenAlex lookup per new paper --
and a tablet request that hangs that long will simply time out. The page starts
a run, then polls for the outcome.

At most one run happens at a time. A double tap on a tablet is easy, and two
concurrent syncs would race on the same ledger rows.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class RefreshResult:
    """What a completed run did."""

    started_at: float
    finished_at: float | None = None
    messages: int = 0
    new_papers: int = 0
    already_known: int = 0
    enriched: int = 0
    error: str | None = None
    # Set when mail succeeded but enrichment could not finish, so the page can
    # say the papers arrived without abstracts rather than claiming failure.
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d


@dataclass
class RefreshState:
    running: bool = False
    started_at: float | None = None
    last: RefreshResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "last": self.last.to_dict() if self.last else None,
        }


class RefreshRunner:
    """Runs a mail check on a worker thread, one at a time."""

    def __init__(self, job: Callable[[], RefreshResult]) -> None:
        self._job = job
        self._lock = threading.Lock()
        self._state = RefreshState()
        self._thread: threading.Thread | None = None

    def _snapshot(self) -> RefreshState:
        """Copy the state. Caller must already hold the lock.

        Separate from state() because start() needs a copy while holding the
        lock, and threading.Lock is not reentrant -- calling state() from
        inside the critical section deadlocks the request thread.
        """
        return RefreshState(
            running=self._state.running,
            started_at=self._state.started_at,
            last=self._state.last,
        )

    def state(self) -> RefreshState:
        with self._lock:
            # Copy so a caller cannot observe a half-updated record.
            return self._snapshot()

    def start(self) -> tuple[bool, RefreshState]:
        """Begin a run. Returns (started, state); False if one is in flight."""
        with self._lock:
            if self._state.running:
                return False, self._snapshot()
            self._state.running = True
            self._state.started_at = time.time()

        self._thread = threading.Thread(target=self._run, name="refresh", daemon=True)
        self._thread.start()
        return True, self.state()

    def _run(self) -> None:
        started = time.time()
        try:
            result = self._job()
        except Exception as exc:  # a worker thread must never die silently
            result = RefreshResult(
                started_at=started,
                finished_at=time.time(),
                error=f"{type(exc).__name__}: {exc}",
            )
        result.finished_at = result.finished_at or time.time()

        with self._lock:
            self._state.running = False
            self._state.last = result

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current run finishes. For tests and the CLI."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


def make_refresh_job(
    *,
    ledger_path: Path,
    cache_path: Path | None,
    mailto: str | None,
    days: int,
    source_factory: Callable[[], Any],
    # Injectable so tests -- and a mail-only check -- can skip the network.
    enricher_factory: Callable[[], Any] | None = None,
) -> Callable[[], RefreshResult]:
    """Build the job the runner executes: fetch mail, record, then enrich.

    Enrichment failure is reported as a warning rather than an error: the
    papers did arrive, and OpenAlex being out of budget should not make the
    check look broken.
    """

    def job() -> RefreshResult:
        # Imported here so this module stays importable without the whole
        # pipeline, which keeps the tests cheap.
        from .cache import LookupCache
        from .enrich import OpenAlexClient, RateLimited
        from .ledger import Ledger
        from .models import AlertPaper
        from .parse import dedupe, parse_alert_email

        result = RefreshResult(started_at=time.time())
        source = source_factory()

        with Ledger(ledger_path) as ledger:
            seen = ledger.seen_message_ids()
            for msg in source.fetch_alerts(since_days=days, skip=seen):
                result.messages += 1
                for paper in dedupe(parse_alert_email(msg.raw)):
                    if ledger.record(paper):
                        result.new_papers += 1
                    else:
                        result.already_known += 1
                # Marked only after its papers are stored, so an interruption
                # re-reads the message rather than losing it.
                ledger.mark_message(msg.message_id)

            if result.new_papers:
                cache = None
                try:
                    if enricher_factory is not None:
                        enricher = enricher_factory()
                    else:
                        cache = LookupCache(cache_path) if cache_path else None
                        enricher = OpenAlexClient(mailto=mailto, cache=cache)
                    with enricher as oa:
                        for entry in ledger.needing_enrichment():
                            paper = AlertPaper(
                                **{
                                    k: v
                                    for k, v in entry.payload.items()
                                    if k in AlertPaper.__dataclass_fields__
                                }
                            )
                            ledger.attach_enrichment(entry.key, oa.enrich(paper).to_dict())
                            result.enriched += 1
                except RateLimited as exc:
                    result.warning = (
                        f"{result.new_papers} new paper(s) arrived, but OpenAlex "
                        f"is out of budget: {exc}"
                    )
                except Exception as exc:
                    result.warning = f"lookup failed: {type(exc).__name__}: {exc}"
                finally:
                    if cache is not None:
                        cache.close()

        result.finished_at = time.time()
        return result

    return job
