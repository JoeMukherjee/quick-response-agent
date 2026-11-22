# Live fault evidence (practice helpdesk, not mocks)

`python scripts/live_fault_probe.py` provokes the helpdesk's real failure modes and shows how the agent handles them.
Last run:

| Fault | What the real server did | What the agent did |
|---|---|---|
| 409 already_handled | Refused a second answer to a ticket | Never retried; delivery recorded `reconciled` |
| 429 rate_limited | 25 raw requests in a burst: 14 x 429 (`Retry-After: 2`), 11 x 200 | The client slept `Retry-After` and still completed all 25 calls |
| Timeout after save | With a 50 ms client timeout the server SAVED the answer but we never saw the reply | `UnknownOutcome`, then a GET showed it handled; delivery `reconciled`, no second answer |

Also observed in ordinary runs: random 503s retried transparently (logged as warnings).

A hard kill of `python -m qra run` after 10 of 30 tickets left a stale lock (the next run refused with a clear
message), and after removing it the resume handled the remaining 20 with 0 errors and exactly one answer per ticket.
