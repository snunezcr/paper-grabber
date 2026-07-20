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

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .jobs import BackgroundRunner


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


class RefreshRunner(BackgroundRunner[RefreshResult]):
    """The shared runner, specialised to a mail check."""

    def __init__(self, job) -> None:
        def stamped() -> RefreshResult:
            # Every completed run reports when it ended, whether or not the
            # job bothered to say so.
            result = job()
            result.finished_at = result.finished_at or time.time()
            return result

        super().__init__(
            stamped,
            on_error=lambda started, exc: RefreshResult(
                started_at=started,
                finished_at=time.time(),
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


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
        from .chain import build_chain
        from .enrich import RateLimited
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
                        enricher = build_chain(mailto=mailto, cache=cache)
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
