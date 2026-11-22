import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_helpdesk import FakeHelpdesk  # noqa: E402
from qra.helpdesk import HelpdeskClient  # noqa: E402
from qra.ledger import Ledger  # noqa: E402
from qra.ratelimit import RequestLimiter  # noqa: E402


class Sleeper:
    def __init__(self):
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
def sleeper():
    return Sleeper()


@pytest.fixture
def fake():
    return FakeHelpdesk([{"id": f"X{n:02d}", "subject": "s", "body": "b"} for n in range(1, 26)])


@pytest.fixture
def client(fake, sleeper):
    return HelpdeskClient("http://helpdesk.test", "key", "me@example.com", transport=fake.transport,
                          limiter=RequestLimiter(1000, sleep=sleeper), sleep=sleeper)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl")
