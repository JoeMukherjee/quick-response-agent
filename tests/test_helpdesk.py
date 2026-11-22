import pytest

from qra.infra.helpdesk import AlreadyHandled, HelpdeskClient, ServiceUnavailable, UnknownOutcome


def test_pagination_follows_cursor_to_the_end(client):
    ids = [t["id"] for t in client.open_tickets()]
    assert len(ids) == 25 and ids == sorted(ids)


def test_503_is_retried_with_backoff(client, fake, sleeper):
    fake.inject("GET /tickets", "503", "503")
    assert len(list(client.open_tickets())) == 25
    backoffs = [s for s in sleeper.slept if s >= 0.2]  # ignore the limiter's sub-millisecond spacing sleeps
    assert len(backoffs) == 2 and all(0.2 <= b <= 2.0 for b in backoffs)  # jittered, so assert bounds not order


def test_503_gives_up_after_max_tries(client, fake):
    fake.inject("GET /tickets", *["503"] * 10)
    with pytest.raises(ServiceUnavailable):
        list(client.open_tickets())


def test_429_honours_retry_after(client, fake, sleeper):
    fake.inject("GET /tickets", "429")
    list(client.open_tickets())
    assert 2.0 in sleeper.slept


def test_503_on_an_action_is_retried_and_saved_once(client, fake):
    fake.inject("POST /tickets/X01/handoff", "503")
    client.act("X01", "handoff", {"handoff_note": "n"})
    assert len(fake.accepted_posts) == 1


def test_409_is_never_retried(client, fake):
    client.act("X01", "close", {"reason": "spam"})
    with pytest.raises(AlreadyHandled):
        client.act("X01", "close", {"reason": "spam"})
    assert fake.calls.count("POST /tickets/X01/close") == 2  # one accepted, one refused, no retry loop


def test_timeout_on_an_action_is_an_unknown_outcome_not_a_retry(client, fake):
    fake.inject("POST /tickets/X02/reply", "timeout_before_save")
    with pytest.raises(UnknownOutcome):
        client.act("X02", "reply", {"draft_reply": "x"})
    assert fake.calls.count("POST /tickets/X02/reply") == 1


def test_clear_text_helpdesk_urls_are_refused():
    with pytest.raises(ValueError):
        HelpdeskClient("http://helpdesk.example.com", "key", "me@example.com")


def test_redirects_are_never_followed_so_the_key_cannot_leak(fake, sleeper):
    import httpx
    from qra.infra.helpdesk import HelpdeskClient as C
    from qra.infra.helpdesk import HelpdeskError
    from qra.infra.ratelimit import RequestLimiter
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"Location": "https://evil.example.net/steal"})

    c = C("https://helpdesk.example.com", "secret", "me@example.com", transport=httpx.MockTransport(handler),
          limiter=RequestLimiter(1000, sleep=sleeper), sleep=sleeper)
    with pytest.raises(HelpdeskError):
        c.status()
    assert seen == ["https://helpdesk.example.com/status"]


def test_unknown_action_is_rejected_locally(client):
    with pytest.raises(ValueError):
        client.act("X01", "delete", {})
