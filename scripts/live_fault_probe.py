"""Provoke the helpdesk's real failure modes in PRACTICE mode and show how the agent's client and delivery handle them.

Run:  python scripts/live_fault_probe.py        (needs .env; resets the practice helpdesk first and again at the end)

Probes (all against the real helpdesk, nothing mocked):
  1. 409: answer a ticket, then try to answer it again.
  2. 429: burst far above 5 requests per second and read the Retry-After header.
  3. timeout after save: POST with a tiny client timeout so the server saves the answer but we never see the reply,
     then show that delivery reconciles with a GET instead of sending a second answer.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qra.core.config import Settings  # noqa: E402
from qra.infra.helpdesk import AlreadyHandled, HelpdeskClient, UnknownOutcome  # noqa: E402
from qra.infra.ledger import Ledger  # noqa: E402
from qra.infra.ratelimit import RequestLimiter  # noqa: E402
from qra.runtime.delivery import deliver  # noqa: E402

PAYLOAD = {"language": "nl", "category": "question", "priority": "P4", "reason": "live fault probe",
           "draft_reply": "Hoi, dit is een testantwoord voor de foutafhandeling."}


def client(s: Settings, **kw) -> HelpdeskClient:
    return HelpdeskClient(s.helpdesk_url, s.helpdesk_key, s.candidate_email, s.candidate_name,
                          limiter=kw.pop("limiter", RequestLimiter(s.helpdesk_rps)), **kw)


def main() -> int:
    s = Settings.from_env(require_gemini=False)
    c = client(s)
    c.reset()
    ids = [t["id"] for t in c.open_tickets()]
    a, b = ids[0], ids[1]
    report = []

    # 1. real 409
    c.act(a, "reply", PAYLOAD)
    try:
        c.act(a, "reply", PAYLOAD)
        report.append(("409 already_handled", "FAIL: second answer was accepted"))
    except AlreadyHandled as exc:
        ledger = Ledger(Path(tempfile.mkdtemp()) / "l.db")
        out = deliver(c, ledger, a, "reply", PAYLOAD)
        report.append(("409 already_handled", f"real 409 received (status {exc.status}); delivery outcome: {out.result}"))

    # 2. real 429 (raw requests, no limiter) then the client recovering from it
    raw = httpx.Client(base_url=s.helpdesk_url, timeout=20.0, headers={
        "Authorization": f"Bearer {s.helpdesk_key}", "X-Candidate-Email": s.candidate_email})
    codes, retry_after, lock = [], [], threading.Lock()

    def hit():
        r = raw.get("/status")
        with lock:
            codes.append(r.status_code)
            if r.status_code == 429:
                retry_after.append(r.headers.get("Retry-After"))

    threads = [threading.Thread(target=hit) for _ in range(25)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    fast = client(s, limiter=RequestLimiter(1000))  # limiter disabled on purpose: only the 429 handling protects us
    sleeps = []
    fast._sleep = lambda sec: (sleeps.append(sec), time.sleep(sec))[1]
    burst = [threading.Thread(target=lambda: fast.status()) for _ in range(25)]
    [t.start() for t in burst]
    [t.join() for t in burst]
    report.append(("429 rate_limited", f"burst of 25 raw requests -> {codes.count(429)} x 429, {codes.count(200)} x 200, "
                                       f"Retry-After={sorted(set(retry_after))}; the client then completed all 25 calls, "
                                       f"waiting {len(sleeps)} time(s) (max {max(sleeps, default=0):.1f}s)"))

    # 3. real timeout after save
    short = client(s, timeout=0.05)
    ledger = Ledger(Path(tempfile.mkdtemp()) / "l.db")
    ledger.record_decided(b, "reply", PAYLOAD)
    try:
        short.act(b, "reply", PAYLOAD)
        report.append(("timeout after save", "no timeout happened (the server answered within 50 ms)"))
    except UnknownOutcome as exc:
        time.sleep(4)  # let the server finish what it was doing
        saved = bool(c.get_ticket(b).get("handled"))
        out = deliver(c, ledger, b, "reply", PAYLOAD)
        report.append(("timeout after save", f"client saw {exc}; server had {'SAVED' if saved else 'NOT saved'} it; "
                                              f"delivery outcome: {out.result} ({out.detail}); "
                                              f"handled on server: {bool(c.get_ticket(b).get('handled'))}"))
    c.reset()
    for name, text in report:
        print(f"[{name}] {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
