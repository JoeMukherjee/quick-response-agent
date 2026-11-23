"""The token window waits instead of failing, and says how long it waited, so pacing is never mistaken for model latency."""
from qra.infra.ratelimit import TokenBudget


def test_a_full_window_makes_the_caller_wait_and_the_wait_is_counted():
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s  # time passes while waiting

    b = TokenBudget(1000, clock=lambda: now[0], sleep=sleep)
    b.reserve(900)
    assert b.waited == 0.0          # it fitted: no wait, nothing to report
    b.reserve(900)                  # does not fit until the first 900 leave the one-minute window
    assert sleeps and abs(b.waited - sum(sleeps)) < 1e-9 and 59 <= b.waited <= 61


def test_reserve_reports_its_own_wait_and_the_runner_subtracts_it(tmp_path):
    now = [0.0]

    def sleep(s):
        now[0] += s

    b = TokenBudget(1000, clock=lambda: now[0], sleep=sleep)
    assert b.reserve(900) == 0.0
    assert 59 <= b.reserve(900) <= 61          # this call waited for the window, and says so
    from qra.decision.judge import Judge
    j = Judge(lambda **k: None, "m", budget=b)
    j.reset_waited()
    assert j.waited_here() == 0.0
