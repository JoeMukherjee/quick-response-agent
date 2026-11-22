"""TypeSafe's System One API (Jev) as the judge, in place of gemini-decision-1.

Both services answer the same typed questions (noul, choice) over a `state` and return calibrated probabilities, so the
rest of the system does not change: the same question set, the same `Judge` (cache, retries, telemetry, error classes)
and the same rule table. Only the transport differs, and this module is all of it.

    POST https://api.typesafe.ai/v1/systemone      Authorization: Bearer <TYPESAFE_API_KEY>
    {"model": "jev-1.14.0", "state": {...}, "questions": {...}}  ->  {"model", "answers": {...}, "usage": {...}}

Choices made here:
  * The model is pinned to a version (`jev-1.14.0`), not the moving `jev-latest` alias: thresholds are tuned against one
    version, and an alias can change its answers without any change to our code.
  * A 429 is handled where it is seen: the `Retry-After` header is honoured (a few waits, capped), because TypeSafe rate
    limits are per organisation and measured per second, unlike the per-minute token window used for Gemini.
  * https only and no redirects, like every other outbound client in this project, so a redirect cannot carry the key
    to another host.
  * Failures keep a `status_code`, so `Judge` classifies them exactly as it does for Gemini: 401 or 403 is the fatal
    invalid-key case, 429 and 5xx are retried, other 4xx are rejected loudly.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, Callable

import httpx

BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-1.14.0"
MAX_RATE_WAITS = 5
MAX_WAIT_S = 30.0


class TypeSafeHTTPError(RuntimeError):
    """A non-2xx answer. `status_code` lets the judge classify it."""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"TypeSafe HTTP {status_code}: {body[:200]}")
        self.status_code = status_code


def make_create(api_key: str, base_url: str = BASE_URL, timeout: float = 60.0, sleep: Callable[[float], None] = time.sleep,
                client: httpx.Client | None = None) -> Callable[..., Any]:
    """A `create(model=, input=, questions=)` callable with the shape `Judge` expects."""
    if not base_url.startswith("https://"):
        raise ValueError("the TypeSafe client only talks https")
    http = client or httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=False,
                                  limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def create(*, model: str, input: str, questions: dict) -> Any:
        try:
            state: Any = json.loads(input)  # named fields, as the TypeSafe guidance recommends
        except ValueError:
            state = input
        body = {"model": model, "state": state, "questions": questions}
        for wait_no in range(MAX_RATE_WAITS + 1):
            resp = http.post("/v1/systemone", headers=headers, json=body)
            if resp.status_code == 429 and wait_no < MAX_RATE_WAITS:
                try:
                    wait = float(resp.headers.get("retry-after", 1.0))
                except ValueError:
                    wait = 1.0
                sleep(min(max(wait, 0.2), MAX_WAIT_S))
                continue
            break
        if resp.status_code != 200:
            raise TypeSafeHTTPError(resp.status_code, resp.text)
        data = resp.json()
        answers = {}
        for key, a in data["answers"].items():
            kind = a.get("type")
            answers[key] = SimpleNamespace(type=kind, noul=a.get("noul"), probabilities=a.get("probabilities"))
        usage = data.get("usage", {})
        return SimpleNamespace(answers=answers, model=data.get("model", model),
                               usage=SimpleNamespace(total_input_tokens=usage.get("input_tokens", 0),
                                                     output_tokens=usage.get("output_tokens", 0)))

    return create
