"""The no-double-answer guarantee, tested against scripted faults."""
from qra.delivery import deliver
from qra.ledger import Ledger

PAYLOAD = {"draft_reply": "first answer"}


def posts_for(fake, tid):
    return [p for p in fake.accepted_posts if p[0] == tid]


def test_happy_path_sends_once_and_records_sent(client, fake, ledger):
    out = deliver(client, ledger, "X01", "reply", PAYLOAD)
    assert out.result == "sent" and ledger.state("X01") == "sent"
    assert len(posts_for(fake, "X01")) == 1


def test_rerun_after_success_does_not_send_again(client, fake, ledger):
    deliver(client, ledger, "X01", "reply", PAYLOAD)
    again = deliver(client, ledger, "X01", "reply", {"draft_reply": "different"})
    assert again.result == "skipped" and len(posts_for(fake, "X01")) == 1


def test_crash_between_decide_and_post_resends_the_stored_payload_exactly_once(client, fake, tmp_path):
    path = tmp_path / "ledger.db"
    Ledger(path).record_decided("X03", "reply", PAYLOAD)  # process dies here, nothing was POSTed
    resumed = Ledger(path)  # new process
    out = deliver(client, resumed, "X03", "reply", {"draft_reply": "REGENERATED, must be ignored"})
    assert out.result == "sent"
    assert [p[2] for p in posts_for(fake, "X03")] == [PAYLOAD]


def test_crash_after_server_saved_but_before_sent_is_reconciled_not_resent(client, fake, tmp_path):
    path = tmp_path / "ledger.db"
    led = Ledger(path)
    led.record_decided("X04", "reply", PAYLOAD)
    client.act("X04", "reply", PAYLOAD)  # server saved it, then the process died before record_sent
    out = deliver(client, Ledger(path), "X04", "reply", PAYLOAD)
    assert out.result == "reconciled" and len(posts_for(fake, "X04")) == 1


def test_timeout_after_save_is_confirmed_by_get_and_not_resent(client, fake, ledger):
    fake.inject("POST /tickets/X05/reply", "timeout_after_save")
    out = deliver(client, ledger, "X05", "reply", PAYLOAD)
    assert out.result == "sent" and len(posts_for(fake, "X05")) == 1


def test_timeout_before_save_is_resent_once_after_get_shows_unhandled(client, fake, ledger):
    fake.inject("POST /tickets/X06/reply", "timeout_before_save")
    out = deliver(client, ledger, "X06", "reply", PAYLOAD)
    assert out.result == "sent" and len(posts_for(fake, "X06")) == 1


def test_409_marks_reconciled_and_is_not_retried(client, fake, ledger):
    client.act("X07", "close", {"reason": "spam"})  # someone else handled it
    out = deliver(client, ledger, "X07", "reply", PAYLOAD)
    assert out.result == "reconciled" and ledger.state("X07") == "reconciled"
    assert len(posts_for(fake, "X07")) == 1


def test_persistent_503_leaves_ticket_decided_for_the_next_run(client, fake, ledger):
    fake.inject("POST /tickets/X08/reply", *["503"] * 10)
    out = deliver(client, ledger, "X08", "reply", PAYLOAD)
    assert out.result == "deferred" and ledger.state("X08") == "decided"
    assert not posts_for(fake, "X08")
    again = deliver(client, ledger, "X08", "reply", {"draft_reply": "ignored"})
    assert again.result == "sent" and [p[2] for p in posts_for(fake, "X08")] == [PAYLOAD]


def test_a_second_decision_for_the_same_ticket_is_refused_by_the_database(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    assert led.record_decided("X09", "reply", PAYLOAD) is True
    assert led.record_decided("X09", "reply", {"draft_reply": "a different answer"}) is False
    assert led.decision("X09") == ("reply", PAYLOAD)  # the first decision is never overwritten


def test_ledger_survives_a_restart(tmp_path):
    path = tmp_path / "ledger.db"
    Ledger(path).record_decided("X10", "handoff", {"handoff_note": "n"})
    again = Ledger(path)
    assert again.state("X10") == "decided" and again.decision("X10") == ("handoff", {"handoff_note": "n"})


def test_new_epoch_reopens_every_ticket(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    led.record_decided("X11", "reply", PAYLOAD)
    led.record_sent("X11")
    led.new_epoch()  # after /practice/reset
    assert led.state("X11") is None
