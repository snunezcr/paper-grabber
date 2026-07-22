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
    # "filed" and "processed" ride along with the decision counts.
    assert ledger.counts() == {
        "accepted": 1, "rejected": 1, "pending": 1, "filed": 0, "processed": 0,
    }


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


# --- settings -----------------------------------------------------------------


def test_setting_roundtrip(ledger):
    ledger.set_setting("base_folder_id", "FOLDER123")
    assert ledger.get_setting("base_folder_id") == "FOLDER123"


def test_missing_setting_returns_default(ledger):
    assert ledger.get_setting("nope") is None
    assert ledger.get_setting("nope", "fallback") == "fallback"


def test_setting_overwrites(ledger):
    ledger.set_setting("k", "a")
    ledger.set_setting("k", "b")
    assert ledger.get_setting("k") == "b"


def test_setting_can_be_cleared(ledger):
    ledger.set_setting("k", "a")
    ledger.clear_setting("k")
    assert ledger.get_setting("k") is None


def test_settings_persist(tmp_path):
    path = tmp_path / "s.db"
    with Ledger(path) as led:
        led.set_setting("base_folder_id", "F1")
    with Ledger(path) as again:
        assert again.get_setting("base_folder_id") == "F1"


# --- destinations -------------------------------------------------------------


def test_destination_is_recorded(ledger):
    ledger.record(paper())
    key = ledger.pending()[0].key
    ledger.decide("Quantum Error Correction", Decision.ACCEPTED)
    assert ledger.set_destination(key, "F1", "Quantum")
    filed = ledger.accepted(filed=True)[0]
    assert filed.dest_folder_id == "F1"
    assert filed.dest_folder_name == "Quantum"


def test_destination_for_unknown_key_is_reported(ledger):
    assert ledger.set_destination("nosuch", "F1", "X") is False


def test_accepted_splits_by_whether_a_destination_is_set(ledger):
    for t in ("A", "B"):
        ledger.record(paper(title=t))
        ledger.decide(t, Decision.ACCEPTED)
    keys = {p.title: p.key for p in ledger.accepted()}
    ledger.set_destination(keys["A"], "F1", "Folder One")

    assert [p.title for p in ledger.accepted(filed=True)] == ["A"]
    assert [p.title for p in ledger.accepted(filed=False)] == ["B"]
    assert len(ledger.accepted()) == 2


def test_rejected_papers_are_not_in_the_filing_queue(ledger):
    ledger.record(paper(title="A"))
    ledger.decide("A", Decision.REJECTED)
    assert ledger.accepted(filed=False) == []


def test_destination_can_be_changed(ledger):
    ledger.record(paper())
    key = ledger.pending()[0].key
    ledger.decide("Quantum Error Correction", Decision.ACCEPTED)
    ledger.set_destination(key, "F1", "One")
    ledger.set_destination(key, "F2", "Two")
    assert ledger.accepted(filed=True)[0].dest_folder_id == "F2"


def test_existing_ledger_gains_the_new_columns(tmp_path):
    # A ledger created before destinations existed must survive the upgrade
    # with its papers intact.
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE papers (
            key TEXT PRIMARY KEY, title TEXT NOT NULL, payload TEXT NOT NULL,
            decision TEXT NOT NULL, first_seen REAL NOT NULL, decided_at REAL
        );
        CREATE TABLE messages (message_id TEXT PRIMARY KEY, processed_at REAL NOT NULL);
        """
    )
    con.execute(
        "INSERT INTO papers VALUES ('k','Old Paper','{}','accepted',1.0,2.0)"
    )
    con.commit()
    con.close()

    with Ledger(path) as led:
        assert [p.title for p in led.accepted()] == ["Old Paper"]
        assert led.set_destination("k", "F1", "Dest")
        assert led.accepted(filed=True)[0].dest_folder_name == "Dest"


def test_filed_count_tracks_destinations(ledger):
    for t in ("A", "B"):
        ledger.record(paper(title=t))
        ledger.decide(t, Decision.ACCEPTED)
    assert ledger.counts()["filed"] == 0

    keys = {p.title: p.key for p in ledger.accepted()}
    ledger.set_destination(keys["A"], "F1", "Folder One")
    assert ledger.counts()["filed"] == 1

    ledger.set_destination(keys["B"], "F1", "Folder One")
    assert ledger.counts()["filed"] == 2


def test_filed_counts_only_accepted_papers(ledger):
    # A destination on a rejected paper must not inflate the filed count.
    ledger.record(paper(title="A"))
    key = ledger.pending()[0].key
    ledger.set_destination(key, "F1", "One")
    ledger.decide("A", Decision.REJECTED)
    assert ledger.counts()["filed"] == 0


# --- fetch and upload progress ------------------------------------------------


def accepted_key(ledger, title="Quantum Error Correction"):
    ledger.record(paper(title=title))
    key = ledger.pending()[0].key
    ledger.decide(title, Decision.ACCEPTED)
    return key


def test_awaiting_download_lists_accepted_papers(ledger):
    key = accepted_key(ledger)
    assert [p.key for p in ledger.awaiting_download()] == [key]


def test_pending_papers_are_not_awaiting_download(ledger):
    ledger.record(paper())
    assert ledger.awaiting_download() == []


def test_staged_paper_leaves_the_download_queue(ledger):
    key = accepted_key(ledger)
    ledger.set_staged(key, "2026 A.pdf")
    assert ledger.awaiting_download() == []


def test_uploaded_paper_is_never_re_downloaded(ledger):
    key = accepted_key(ledger)
    ledger.set_staged(key, "2026 A.pdf")
    ledger.set_uploaded(key, "DRIVE1")
    assert ledger.awaiting_download() == []
    assert ledger.awaiting_upload() == []


def test_awaiting_upload_needs_both_a_file_and_a_destination(ledger):
    key = accepted_key(ledger)
    ledger.set_staged(key, "2026 A.pdf")
    assert ledger.awaiting_upload() == []  # no destination yet

    ledger.set_destination(key, "F1", "Folder")
    assert [p.key for p in ledger.awaiting_upload()] == [key]


def test_destination_without_a_staged_file_is_not_uploadable(ledger):
    key = accepted_key(ledger)
    ledger.set_destination(key, "F1", "Folder")
    assert ledger.awaiting_upload() == []


def test_upload_clears_the_staging_claim(ledger):
    key = accepted_key(ledger)
    ledger.set_staged(key, "2026 A.pdf")
    ledger.set_destination(key, "F1", "Folder")
    ledger.set_uploaded(key, "DRIVE1")
    stored = ledger.get(key)
    assert stored.staged_name is None
    assert stored.drive_file_id == "DRIVE1"
    assert stored.uploaded_at is not None


def test_staging_claim_can_be_released(ledger):
    # A vanished staged file must return to the download queue.
    key = accepted_key(ledger)
    ledger.set_staged(key, "2026 A.pdf")
    ledger.set_staged(key, None)
    assert [p.key for p in ledger.awaiting_download()] == [key]


def test_staged_name_survives_a_title_change(ledger):
    # The whole reason the name is stored: enrichment can revise a title after
    # the file is already on disk under the old one.
    key = accepted_key(ledger)
    ledger.set_staged(key, "2026 Original Title.pdf")
    ledger.attach_enrichment(key, {"title": "A Completely Revised Title"})
    assert ledger.get(key).staged_name == "2026 Original Title.pdf"


# --- processed ----------------------------------------------------------------


def test_uploaded_paper_moves_out_of_the_filing_queue(ledger):
    # "Ready to upload" is meaningless if it also lists what is already there.
    key = accepted_key(ledger)
    ledger.set_destination(key, "F1", "Folder")
    assert len(ledger.accepted(filed=True)) == 1

    ledger.set_uploaded(key, "DRIVE1")
    assert ledger.accepted(filed=True) == []
    assert ledger.accepted() == []
    assert [p.key for p in ledger.processed()] == [key]


def test_processed_is_empty_before_any_upload(ledger):
    accepted_key(ledger)
    assert ledger.processed() == []


def test_processed_counts_separately(ledger):
    key = accepted_key(ledger)
    ledger.set_destination(key, "F1", "Folder")
    ledger.set_uploaded(key, "DRIVE1")
    counts = ledger.counts()
    assert counts["processed"] == 1
    assert counts["filed"] == 0


def test_processed_is_newest_first(ledger):
    import time

    for t in ("first", "second"):
        ledger.record(paper(title=t))
        ledger.decide(t, Decision.ACCEPTED)
    keys = {p.title: p.key for p in ledger.accepted()}
    ledger.set_uploaded(keys["first"], "D1")
    time.sleep(0.01)
    ledger.set_uploaded(keys["second"], "D2")
    assert [p.title for p in ledger.processed()] == ["second", "first"]


def test_processed_paper_exposes_a_drive_link(ledger):
    from paper_grabber.ledger import paper_view

    key = accepted_key(ledger)
    ledger.set_uploaded(key, "ABC123")
    view = paper_view(ledger.processed()[0])
    assert view["drive_url"] == "https://drive.google.com/file/d/ABC123/view"
    assert view["uploaded"] is True


def test_unprocessed_paper_has_no_drive_link(ledger):
    from paper_grabber.ledger import paper_view

    accepted_key(ledger)
    assert paper_view(ledger.accepted()[0])["drive_url"] is None


# --- notes --------------------------------------------------------------------


def test_note_roundtrip(ledger):
    ledger.record(paper())
    key = ledger.pending()[0].key
    assert ledger.set_note(key, "Read section 4.")
    assert ledger.get(key).note == "Read section 4."


def test_note_is_trimmed(ledger):
    ledger.record(paper())
    key = ledger.pending()[0].key
    ledger.set_note(key, "   spaced   ")
    assert ledger.get(key).note == "spaced"


def test_blank_note_clears_it(ledger):
    ledger.record(paper())
    key = ledger.pending()[0].key
    ledger.set_note(key, "something")
    ledger.set_note(key, "   ")
    assert ledger.get(key).note is None


def test_note_for_an_unknown_key_is_reported(ledger):
    assert ledger.set_note("nope", "x") is False


def test_every_query_returns_the_same_columns(ledger):
    """Guards against column drift.

    Adding `note` initially updated four of the seven paper queries; the
    others silently returned rows one column short, so notes saved fine and
    were invisible everywhere they were read back.
    """
    ledger.record(paper(title="Everywhere"))
    key = ledger.pending()[0].key
    ledger.set_note(key, "visible everywhere")

    assert ledger.get(key).note == "visible everywhere"
    assert ledger.pending()[0].note == "visible everywhere"

    ledger.decide("Everywhere", Decision.ACCEPTED)
    assert ledger.accepted()[0].note == "visible everywhere"
    assert ledger.awaiting_download()[0].note == "visible everywhere"

    ledger.set_destination(key, "F1", "Folder")
    assert ledger.accepted(filed=True)[0].note == "visible everywhere"
    ledger.set_staged(key, "a.pdf")
    assert ledger.awaiting_upload()[0].note == "visible everywhere"

    ledger.set_uploaded(key, "DRIVE1")
    assert ledger.processed()[0].note == "visible everywhere"

    ledger.decide("Everywhere", Decision.REJECTED)
    assert ledger.rejected()[0].note == "visible everywhere"


# --- alert stats --------------------------------------------------------------


def test_alert_stats_group_by_alert_and_decision(ledger):
    for t in ("A", "B", "C"):
        ledger.record(paper(title=t, alert_query="cs.AI"))
    ledger.record(paper(title="D", alert_query="stats.ML"))
    ledger.decide("A", Decision.ACCEPTED)
    ledger.decide("B", Decision.REJECTED)
    ledger.decide("C", Decision.REJECTED)
    # D stays pending.

    stats = ledger.alert_stats()
    assert stats["cs.AI"] == {"accepted": 1, "rejected": 2, "pending": 0}
    assert stats["stats.ML"] == {"accepted": 0, "rejected": 0, "pending": 1}


def test_alert_stats_buckets_missing_query_as_no_alert(ledger):
    from paper_grabber.ledger import NO_ALERT

    ledger.record(paper(title="X"))            # no alert_query at all
    ledger.decide("X", Decision.REJECTED)
    assert ledger.alert_stats()[NO_ALERT] == {"accepted": 0, "rejected": 1, "pending": 0}


def test_alert_stats_still_counts_accepted_after_upload(ledger):
    # A paper keeps decision=accepted through filing and upload, so it must go
    # on counting toward its alert's kept share -- otherwise a productive alert
    # would look noisier the more of it you actually used.
    ledger.record(paper(title="A", alert_query="cs.AI"))
    key = ledger.pending()[0].key
    ledger.decide("A", Decision.ACCEPTED)
    ledger.set_uploaded(key, "DRIVE1")
    assert ledger.alert_stats()["cs.AI"]["accepted"] == 1
