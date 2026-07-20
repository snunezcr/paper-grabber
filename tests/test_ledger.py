import pytest

from paper_grabber.ledger import Decision, Ledger
from paper_grabber.models import AlertPaper


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "state.db") as led:
        yield led


def paper(title="Quantum Error Correction", **kw):
    return AlertPaper(title=title, **kw)


# --- processed messages -------------------------------------------------------


def test_message_starts_unseen(ledger):
    assert not ledger.message_seen("m1")


def test_marked_message_is_seen(ledger):
    ledger.mark_message("m1")
    assert ledger.message_seen("m1")


def test_marking_twice_is_harmless(ledger):
    ledger.mark_message("m1")
    ledger.mark_message("m1")
    assert ledger.seen_message_ids() == {"m1"}


def test_seen_ids_feed_the_skip_set(ledger):
    ledger.mark_message("a")
    ledger.mark_message("b")
    assert ledger.seen_message_ids() == {"a", "b"}


# --- papers -------------------------------------------------------------------


def test_new_paper_is_recorded_as_pending(ledger):
    assert ledger.record(paper()) is True
    assert ledger.decision_for("Quantum Error Correction") is Decision.PENDING


def test_recording_the_same_paper_twice_reports_not_new(ledger):
    ledger.record(paper())
    assert ledger.record(paper()) is False


def test_title_variants_collapse_to_one_paper(ledger):
    # Scholar varies case and punctuation between alerts.
    ledger.record(paper(title="Quantum Error Correction"))
    assert ledger.record(paper(title="quantum  error correction!")) is False


def test_decision_is_recorded(ledger):
    ledger.record(paper())
    ledger.decide("Quantum Error Correction", Decision.ACCEPTED)
    assert ledger.decision_for("Quantum Error Correction") is Decision.ACCEPTED


def test_rejection_survives_re_recording(ledger):
    # The whole reason rejections are stored: the same paper arrives again
    # from another alert next week and must not be offered afresh.
    ledger.record(paper())
    ledger.decide("Quantum Error Correction", Decision.REJECTED)
    assert ledger.record(paper()) is False
    assert ledger.decision_for("Quantum Error Correction") is Decision.REJECTED


def test_pending_excludes_decided_papers(ledger):
    ledger.record(paper(title="A"))
    ledger.record(paper(title="B"))
    ledger.decide("A", Decision.REJECTED)
    assert [p.title for p in ledger.pending()] == ["B"]


def test_pending_is_oldest_first(ledger):
    for t in ("first", "second", "third"):
        ledger.record(paper(title=t))
    assert [p.title for p in ledger.pending()] == ["first", "second", "third"]


def test_payload_round_trips(ledger):
    p = paper(authors=["A Author"], year=2026, url="https://x.example/a.pdf")
    ledger.record(p)
    stored = ledger.pending()[0]
    assert stored.payload["authors"] == ["A Author"]
    assert stored.payload["year"] == 2026


def test_counts_by_decision(ledger):
    ledger.record(paper(title="A"))
    ledger.record(paper(title="B"))
    ledger.record(paper(title="C"))
    ledger.decide("A", Decision.ACCEPTED)
    ledger.decide("B", Decision.REJECTED)
    assert ledger.counts() == {"accepted": 1, "rejected": 1, "pending": 1}


def test_unknown_paper_has_no_decision(ledger):
    assert ledger.decision_for("Never seen") is None
    assert not ledger.known("Never seen")


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "s.db"
    with Ledger(path) as led:
        led.record(paper())
        led.decide("Quantum Error Correction", Decision.REJECTED)
        led.mark_message("m1")
    with Ledger(path) as again:
        assert again.decision_for("Quantum Error Correction") is Decision.REJECTED
        assert again.message_seen("m1")


# --- enrichment ---------------------------------------------------------------


def test_enrichment_attaches_to_an_existing_paper(ledger):
    ledger.record(paper())
    key = ledger.pending()[0].key
    assert ledger.attach_enrichment(key, {"doi": "10.1/x", "abstract": "Real abstract."})
    assert ledger.pending()[0].payload["enrichment"]["doi"] == "10.1/x"


def test_enrichment_for_unknown_key_is_reported(ledger):
    assert ledger.attach_enrichment("nosuchkey", {"doi": "10.1/x"}) is False


def test_enrichment_does_not_disturb_the_alert_fields(ledger):
    ledger.record(paper(authors=["A Author"], year=2026))
    key = ledger.pending()[0].key
    ledger.attach_enrichment(key, {"doi": "10.1/x"})
    payload = ledger.pending()[0].payload
    assert payload["authors"] == ["A Author"]
    assert payload["year"] == 2026


def test_needing_enrichment_excludes_the_enriched(ledger):
    ledger.record(paper(title="A"))
    ledger.record(paper(title="B"))
    ledger.attach_enrichment(ledger.pending()[0].key, {"doi": "10.1/x"})
    assert [p.title for p in ledger.needing_enrichment()] == ["B"]


def test_needing_enrichment_excludes_decided_papers(ledger):
    ledger.record(paper(title="A"))
    ledger.decide("A", Decision.REJECTED)
    assert ledger.needing_enrichment() == []


def test_re_enriching_overwrites(ledger):
    ledger.record(paper())
    key = ledger.pending()[0].key
    ledger.attach_enrichment(key, {"doi": "10.1/old"})
    ledger.attach_enrichment(key, {"doi": "10.1/new"})
    assert ledger.pending()[0].payload["enrichment"]["doi"] == "10.1/new"
