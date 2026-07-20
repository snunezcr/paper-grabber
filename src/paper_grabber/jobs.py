"""A background job that at most one of runs at a time.

Shared by the manual mail check and by uploading from a card. Both take long
enough that holding a request open would time out on a tablet, and both must
refuse to run twice concurrently -- a double tap is easy, and two runs would
race on the same ledger rows.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

R = TypeVar("R")


@dataclass
class JobState(Generic[R]):
    running: bool = False
    started_at: float | None = None
    last: R | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "last": self.last.to_dict() if self.last is not None else None,
        }


class BackgroundRunner(Generic[R]):
    """Runs a callable on a worker thread, one at a time."""

    def __init__(
        self,
        job: Callable[[], R],
        *,
        on_error: Callable[[float, Exception], R],
    ) -> None:
        self._job = job
        self._on_error = on_error
        self._lock = threading.Lock()
        self._state: JobState[R] = JobState()
        self._thread: threading.Thread | None = None

    def _snapshot(self) -> JobState[R]:
        """Copy the state. Caller must already hold the lock.

        Separate from state() because start() needs a copy while holding the
        lock, and threading.Lock is not reentrant -- calling state() from
        inside the critical section deadlocks the request thread.
        """
        return JobState(
            running=self._state.running,
            started_at=self._state.started_at,
            last=self._state.last,
        )

    def state(self) -> JobState[R]:
        with self._lock:
            return self._snapshot()

    def start(self) -> tuple[bool, JobState[R]]:
        """Begin a run. Returns (started, state); False if one is in flight."""
        with self._lock:
            if self._state.running:
                return False, self._snapshot()
            self._state.running = True
            self._state.started_at = time.time()

        self._thread = threading.Thread(target=self._run, name="job", daemon=True)
        self._thread.start()
        return True, self.state()

    def _run(self) -> None:
        started = time.time()
        try:
            result = self._job()
        except Exception as exc:  # a worker thread must never die silently
            result = self._on_error(started, exc)

        with self._lock:
            self._state.running = False
            self._state.last = result

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current run finishes. For tests and the CLI."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
