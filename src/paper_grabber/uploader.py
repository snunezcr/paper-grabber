"""Fetch-and-upload for a paper chosen in the UI.

The CLI splits this into `fetch` then `upload` because the timer runs them at
different points. From a card there is only one intention -- "put this in
Drive" -- so the job does both, downloading first if the PDF is not staged yet.

Like the mail check it runs on a worker thread: a download plus an upload can
take a minute, and a tablet request will not wait that long.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .jobs import BackgroundRunner


@dataclass
class PaperOutcome:
    key: str
    title: str
    ok: bool
    detail: str
    folder: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UploadResult:
    started_at: float
    finished_at: float | None = None
    uploaded: int = 0
    failed: int = 0
    outcomes: list[PaperOutcome] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "uploaded": self.uploaded,
            "failed": self.failed,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "error": self.error,
            "ok": self.ok,
        }


class UploadRunner(BackgroundRunner[UploadResult]):
    """The shared runner, specialised to fetch-and-upload."""

    def __init__(self, job) -> None:
        def stamped() -> UploadResult:
            result = job()
            result.finished_at = result.finished_at or time.time()
            return result

        super().__init__(
            stamped,
            on_error=lambda started, exc: UploadResult(
                started_at=started,
                finished_at=time.time(),
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def make_upload_job(
    *,
    ledger_path: Path,
    staging_path: Path,
    keys: list[str],
    drive_factory: Callable[[], Any],
    http_factory: Callable[[], Any] | None = None,
) -> Callable[[], UploadResult]:
    """Build a job that stages any missing PDFs, then uploads the given papers."""

    def job() -> UploadResult:
        from .enrich import direct_pdf_url
        from .fetch import download_first_available, make_client
        from .filename import deduplicate_filename, pdf_filename
        from .ledger import Ledger, paper_view
        from .models import AlertPaper
        from .staging import StagingArea, VerificationError

        result = UploadResult(started_at=time.time())
        staging = StagingArea(staging_path)
        staging.sweep_partials()
        drive = drive_factory()
        http = (http_factory or make_client)()

        try:
            with Ledger(ledger_path) as ledger:
                taken = {p.name for p in staging.pending()}

                for key in keys:
                    entry = ledger.get(key)
                    if entry is None:
                        result.failed += 1
                        result.outcomes.append(
                            PaperOutcome(key, key, False, "no such paper")
                        )
                        continue

                    view = paper_view(entry)
                    title = view["title"]

                    if entry.drive_file_id:
                        result.outcomes.append(
                            PaperOutcome(key, title, True, "already in Drive",
                                         entry.dest_folder_name)
                        )
                        continue

                    if not entry.dest_folder_id:
                        result.failed += 1
                        result.outcomes.append(
                            PaperOutcome(key, title, False, "no destination chosen")
                        )
                        continue

                    # Stage it if the file is not on disk yet.
                    name = entry.staged_name
                    if not name or not staging.path_for(name).exists():
                        candidates = _candidates(entry, AlertPaper, direct_pdf_url)
                        if not candidates:
                            result.failed += 1
                            result.outcomes.append(
                                PaperOutcome(key, title, False, "no open-access PDF")
                            )
                            continue

                        fetched = download_first_available(candidates, client=http)
                        if not fetched.ok:
                            result.failed += 1
                            result.outcomes.append(
                                PaperOutcome(key, title, False,
                                             f"download failed: {fetched.reason}")
                            )
                            continue

                        name = deduplicate_filename(
                            pdf_filename(title, view["year"]), taken
                        )
                        taken.add(name)
                        staging.stage(name, fetched.content)
                        ledger.set_staged(key, name)

                    path = staging.path_for(name)
                    try:
                        remote = drive.upload(path, folder_id=entry.dest_folder_id)
                        staging.confirm(path, remote)
                        ledger.set_uploaded(key, remote.file_id)
                        result.uploaded += 1
                        result.outcomes.append(
                            PaperOutcome(key, title, True, "uploaded",
                                         entry.dest_folder_name)
                        )
                    except VerificationError as exc:
                        # Drive's copy could not be proven identical, so the
                        # local file stays. Safe, not lost.
                        result.failed += 1
                        result.outcomes.append(
                            PaperOutcome(key, title, False,
                                         f"unverified, kept locally: {exc}")
                        )
                    except Exception as exc:
                        result.failed += 1
                        result.outcomes.append(
                            PaperOutcome(key, title, False,
                                         f"{type(exc).__name__}: {exc}")
                        )
        finally:
            for closeable in (http, drive):
                if hasattr(closeable, "close"):
                    try:
                        closeable.close()
                    except Exception:  # pragma: no cover - best effort
                        pass

        result.finished_at = time.time()
        return result

    return job


def _candidates(entry, alert_cls, direct) -> list[str]:
    """PDF URLs for a ledger row: enrichment first, Scholar's own link last."""
    enrichment = entry.payload.get("enrichment") or {}
    urls = list(enrichment.get("pdf_candidates") or [])
    if not urls and enrichment.get("pdf_url"):
        urls.append(enrichment["pdf_url"])

    paper = alert_cls(
        **{k: v for k, v in entry.payload.items() if k in alert_cls.__dataclass_fields__}
    )
    scholar = direct(paper)
    if scholar and scholar not in urls:
        urls.append(scholar)
    return urls
