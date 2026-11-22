"""In-memory fake of the Roosterly helpdesk, with scripted faults, served through httpx.MockTransport."""
from __future__ import annotations

import json
from collections import defaultdict, deque

import httpx


class FakeHelpdesk:
    def __init__(self, tickets: list[dict]):
        self.tickets = {t["id"]: dict(t, handled=None) for t in tickets}
        self.faults: dict[str, deque] = defaultdict(deque)  # key "METHOD /path" -> queue of fault names
        self.accepted_posts: list[tuple[str, str, dict]] = []  # (ticket, action, body) the server SAVED
        self.calls: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def inject(self, key: str, *faults: str) -> None:
        self.faults[key].extend(faults)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        self.calls.append(key)
        fault = self.faults[key].popleft() if self.faults[key] else None
        path = request.url.path
        if fault == "503":
            return httpx.Response(503, json={"error": "service_unavailable"})
        if fault == "429":
            return httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "2"})
        if fault == "timeout_before_save":
            raise httpx.ReadTimeout("slow", request=request)

        if request.method == "GET" and path == "/tickets":
            cursor = request.url.params.get("cursor")
            open_ids = [i for i, t in self.tickets.items() if not t["handled"]]
            start = open_ids.index(cursor) if cursor in open_ids else 0
            page = open_ids[start:start + 10]
            nxt = open_ids[start + 10] if len(open_ids) > start + 10 else None
            return httpx.Response(200, json={"tickets": [self.tickets[i] for i in page], "next_cursor": nxt})
        if request.method == "GET" and path.startswith("/tickets/"):
            tid = path.rsplit("/", 1)[1]
            if tid not in self.tickets:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json=self.tickets[tid])
        if request.method == "POST" and path.endswith(("/reply", "/handoff", "/close")):
            _, _, tid, action = path.split("/")
            ticket = self.tickets.get(tid)
            if ticket is None:
                return httpx.Response(404, json={"error": "not_found"})
            if ticket["handled"]:
                return httpx.Response(409, json={"error": "already_handled"})
            body = json.loads(request.content or b"{}")
            ticket["handled"] = {"action": action}
            self.accepted_posts.append((tid, action, body))
            if fault == "timeout_after_save":
                raise httpx.ReadTimeout("saved but the response was lost", request=request)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "not_found"})
