"""Manual mail-check tests. No network: the mail source is a stub."""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from paper_grabber.ledger import Ledger
from paper_grabber.refresh import RefreshResult, RefreshRunner, make_refresh_job
from paper_grabber.server import create_app

DATA = Path(__file__).parent / "data"
EMLS = sorted(DATA.glob("*.eml"))


class StubSource:
    """Serves the real .eml fixtures as if they had just arrived."""

    def __init__(self, paths=None, error=None):
        self.paths = list(paths if paths is not None else EMLS)
        self.error = error
        self.calls = []

    def fetch_alerts(self, *, since_days=2, skip=None, limit=None):
        self.calls.append({"since_days": since_days, "skip": set(skip or ())})
        if self.error:
            raise self.error
        from paper_grabber.imap_source import RawMessage, extract_message_id

        for p in self.paths:
            raw = p.read_bytes()
            mid = extract_message_id(raw)
            if mid in (skip or set()):
                continue
            yield RawMessage(message_id=mid, raw=raw)


class StubEnricher:
    """Stands in for OpenAlex so the suite never touches the network."""

    def __init__(self, error=None):
        self.error = error
        self.seen = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def enrich(self, paper):
        if self.error:
            raise self.error
        self.seen.append(paper.title)

        class _E:
            @staticmethod
            def to_dict():
                return {"abstract": "stub abstract", "year": 2026}

        return _E()


def job_for(tmp_path, source, enricher=None, **kw):
    return make_refresh_job(
        ledger_path=tmp_path / "state.db",
        cache_path=None,
        mailto=None,
        days=kw.get("days", 7),
        source_factory=lambda: source,
        enricher_factory=lambda: enricher or StubEnricher(),
    )


# --- the job ------------------------------------------------------------------


def test_job_records_papers_from_mail(tmp_path):
    result = job_for(tmp_path, StubSource())()
    assert result.messages == len(EMLS)
    assert result.new_papers == 16
    assert result.ok


def test_job_is_idempotent(tmp_path):
    job = job_for(tmp_path, StubSource())
    job()
    second = job()
    assert second.messages == 0      # every message already marked
    assert second.new_papers == 0


def test_job_skips_messages_already_processed(tmp_path):
    source = StubSource()
    job_for(tmp_path, source)()
    job_for(tmp_path, source)()
    # The second call must have been told what to skip.
    assert source.calls[1]["skip"]


def test_job_reports_a_mail_failure(tmp_path):
    source = StubSource(error=RuntimeError("mailbox unavailable"))
    with pytest.raises(RuntimeError):
        job_for(tmp_path, source)()


def test_job_passes_the_configured_window(tmp_path):
    source = StubSource()
    job_for(tmp_path, source, days=30)()
    assert source.calls[0]["since_days"] == 30


def test_empty_mailbox_is_a_clean_no_op(tmp_path):
    result = job_for(tmp_path, StubSource(paths=[]))()
    assert result.messages == 0
    assert result.new_papers == 0
    assert result.ok


# --- the runner ---------------------------------------------------------------


def test_runner_reports_idle_before_anything():
    r = RefreshRunner(lambda: RefreshResult(started_at=0))
    s = r.state()
    assert s.running is False and s.last is None


def test_runner_records_the_result():
    r = RefreshRunner(lambda: RefreshResult(started_at=1, new_papers=3))
    r.start()
    r.wait(5)
    assert r.state().last.new_papers == 3


def test_runner_refuses_a_concurrent_run():
    gate = threading.Event()

    def slow():
        gate.wait(5)
        return RefreshResult(started_at=0)

    r = RefreshRunner(slow)
    assert r.start()[0] is True
    # A double tap on a tablet is easy; two syncs would race on the ledger.
    assert r.start()[0] is False
    gate.set()
    r.wait(5)


def test_runner_becomes_available_again():
    r = RefreshRunner(lambda: RefreshResult(started_at=0))
    r.start()
    r.wait(5)
    assert r.start()[0] is True
    r.wait(5)


def test_worker_exception_is_captured_not_lost():
    def boom():
        raise ValueError("mail exploded")

    r = RefreshRunner(boom)
    r.start()
    r.wait(5)
    last = r.state().last
    assert last.ok is False
    assert "mail exploded" in last.error


def test_finished_at_is_always_set():
    r = RefreshRunner(lambda: RefreshResult(started_at=0))
    r.start()
    r.wait(5)
    assert r.state().last.finished_at is not None


# --- endpoints ----------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    runner = RefreshRunner(lambda: RefreshResult(started_at=0, messages=1, new_papers=2))
    app = create_app(tmp_path / "s.db", refresh_runner=runner)
    return TestClient(app), runner


def test_post_starts_a_check(client):
    c, runner = client
    r = c.post("/api/refresh")
    assert r.status_code == 202
    assert r.json()["started"] is True
    runner.wait(5)


def test_status_reports_the_outcome(client):
    c, runner = client
    c.post("/api/refresh")
    runner.wait(5)
    last = c.get("/api/refresh").json()["last"]
    assert last["new_papers"] == 2
    assert last["ok"] is True


def test_second_tap_while_running_is_not_an_error():
    gate = threading.Event()
    runner = RefreshRunner(lambda: (gate.wait(5), RefreshResult(started_at=0))[1])
    import tempfile

    c = TestClient(create_app(Path(tempfile.mkdtemp()) / "s.db", refresh_runner=runner))
    first = c.post("/api/refresh")
    second = c.post("/api/refresh")
    assert first.json()["started"] is True
    assert second.status_code == 202
    assert second.json()["started"] is False   # joined the run already in flight
    gate.set()
    runner.wait(5)


def test_status_is_readable_before_any_run(client):
    c, _ = client
    s = c.get("/api/refresh").json()
    assert s["running"] is False and s["last"] is None


def test_failure_surfaces_in_status(tmp_path):
    def boom():
        raise RuntimeError("no mailbox")

    runner = RefreshRunner(boom)
    c = TestClient(create_app(tmp_path / "s.db", refresh_runner=runner))
    c.post("/api/refresh")
    runner.wait(5)
    last = c.get("/api/refresh").json()["last"]
    assert last["ok"] is False
    assert "no mailbox" in last["error"]


def test_page_has_the_check_button(tmp_path):
    c = TestClient(create_app(tmp_path / "s.db"))
    body = c.get("/").text
    assert 'id="check"' in body
    assert "Check now" in body


def test_job_enriches_the_new_papers(tmp_path):
    enricher = StubEnricher()
    result = job_for(tmp_path, StubSource(), enricher)()
    assert result.enriched == 16
    assert len(enricher.seen) == 16


def test_enrichment_failure_is_a_warning_not_an_error(tmp_path):
    # The papers did arrive; OpenAlex being down must not make the check look
    # broken, or the user re-runs it pointlessly.
    from paper_grabber.enrich import RateLimited

    enricher = StubEnricher(error=RateLimited("Insufficient budget."))
    result = job_for(tmp_path, StubSource(), enricher)()
    assert result.ok is True
    assert result.new_papers == 16
    assert "out of budget" in result.warning


def test_unexpected_enrichment_failure_is_also_a_warning(tmp_path):
    enricher = StubEnricher(error=ValueError("weird"))
    result = job_for(tmp_path, StubSource(), enricher)()
    assert result.ok is True
    assert "weird" in result.warning


def test_no_enrichment_when_nothing_is_new(tmp_path):
    enricher = StubEnricher()
    job = job_for(tmp_path, StubSource(), enricher)
    job()
    enricher.seen.clear()
    job()
    assert enricher.seen == []      # a second check must not re-enrich
