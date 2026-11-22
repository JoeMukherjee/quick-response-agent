"""Typed client for the Roosterly helpdesk API.

Error contract (from api_reference.md):
  503  nothing was saved -> retry with backoff
  429  honour Retry-After, then retry
  409  already handled -> never retried, surfaced as AlreadyHandled
  timeout / connection reset on an action -> outcome unknown -> UnknownOutcome; the caller
  must GET the ticket before any retry, because the server may have saved it.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import httpx

from qra.core.errors import FatalAuthError
from qra.infra.ratelimit import RequestLimiter

log = logging.getLogger("qra.helpdesk")


class HelpdeskError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status, self.body = status, body


class Unauthorized(HelpdeskError, FatalAuthError):
    """401: the helpdesk key is invalid. Fatal: nothing can succeed."""
class NotFound(HelpdeskError): ...
class AlreadyHandled(HelpdeskError): ...
class ValidationFailed(HelpdeskError): ...
class RateLimited(HelpdeskError): ...
class ServiceUnavailable(HelpdeskError): ...


class UnknownOutcome(HelpdeskError):
    """The request may or may not have reached the server."""


class HelpdeskClient:
    MAX_TRIES = 6

    def __init__(self, base_url: str, key: str, email: str, name: str = "", *,
                 limiter: RequestLimiter | None = None, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 20.0):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and not (parsed.hostname or "").endswith((".test", "localhost")):
            raise ValueError("HELPDESK_URL must be https: the bearer key must never travel in clear text")
        headers = {"Authorization": f"Bearer {key}", "X-Candidate-Email": email}
        if name:
            headers["X-Candidate-Name"] = name
        # TLS verification stays on (httpx default). Redirects are NOT followed, so the key can never be
        # replayed to a different host; explicit timeouts bound every call.
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport,
                                  follow_redirects=False, verify=True)
        self._limiter = limiter or RequestLimiter(4.0)
        self._sleep = sleep

    # ---- low level -------------------------------------------------------------------
    def _request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                 idempotent: bool) -> Any:
        """`idempotent` is True for GETs: transport errors are retried. For actions they raise UnknownOutcome."""
        delay = 0.5
        for attempt in range(1, self.MAX_TRIES + 1):
            self._limiter.wait()
            try:
                resp = self._http.request(method, path, json=json, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if not idempotent:
                    raise UnknownOutcome(f"{method} {path}: {type(exc).__name__}") from exc
                if attempt == self.MAX_TRIES:
                    raise ServiceUnavailable(f"{method} {path}: {type(exc).__name__}") from exc
                self._backoff(delay, attempt)
                delay *= 2
                continue
            status = resp.status_code
            if status < 300:
                return resp.json() if resp.content else None
            body = self._body(resp)
            if status == 503:
                if attempt == self.MAX_TRIES:
                    raise ServiceUnavailable(f"{method} {path}: 503 after {attempt} tries", status, body)
                self._backoff(delay, attempt)
                delay *= 2
                continue
            if status == 429:
                if attempt == self.MAX_TRIES:
                    raise RateLimited(f"{method} {path}: 429 after {attempt} tries", status, body)
                wait = float(resp.headers.get("Retry-After", "1") or 1)
                log.warning("429 on %s %s, waiting %.1fs", method, path, wait)
                self._sleep(wait)
                continue
            if status == 409:
                raise AlreadyHandled(f"{method} {path}: already handled", status, body)
            if status == 401:
                raise Unauthorized("401 unauthorized: check HELPDESK_KEY", status, body)
            if status == 404:
                raise NotFound(f"{method} {path}: not found", status, body)
            if status in (400, 422):
                raise ValidationFailed(f"{method} {path}: {status} {body}", status, body)
            raise HelpdeskError(f"{method} {path}: unexpected {status}", status, body)
        raise AssertionError("unreachable")

    @staticmethod
    def _body(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text[:300]

    def _backoff(self, delay: float, attempt: int) -> None:
        wait = delay * (0.5 + random.random())
        log.warning("503/transport error, retry %d in %.2fs", attempt, wait)
        self._sleep(wait)

    # ---- reads -------------------------------------------------------------------------
    def status(self) -> dict:
        return self._request("GET", "/status", idempotent=True)

    def examples(self) -> list[dict]:
        data = self._request("GET", "/examples", idempotent=True)
        return data["examples"] if isinstance(data, dict) else data

    def open_tickets(self) -> Iterator[dict]:
        """Follow next_cursor until it is null (max 10 per page)."""
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 10, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            page = self._request("GET", "/tickets", params=params, idempotent=True)
            yield from page["tickets"]
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def get_ticket(self, ticket_id: str) -> dict:
        return self._request("GET", f"/tickets/{ticket_id}", idempotent=True)

    # ---- actions (never retried after an unknown outcome) ------------------------------
    def act(self, ticket_id: str, action: str, payload: dict) -> Any:
        if action not in ("reply", "handoff", "close"):
            raise ValueError(f"unknown action {action!r}")
        return self._request("POST", f"/tickets/{ticket_id}/{action}", json=payload, idempotent=False)

    def reset(self) -> Any:
        return self._request("POST", "/practice/reset", idempotent=False)
