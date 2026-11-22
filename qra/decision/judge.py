"""Thin, cached, rate-limited, thread-safe wrapper around the Gemini Decisions API (gemini-decision-1, v1beta).

* Responses are cached (SQLite) by sha256(model, input, questions): re-running the eval after a change to
  thresholds or the truth table replays free, and only reworded questions cost tokens.
* Token budget: each call reserves an estimate and is corrected with the reported usage (free tier: 50k/min).
* A failed call is an ERROR, never a "no": it raises JudgeError and the caller leaves the ticket open.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from typing import Any, Callable

from qra.infra.cache import DecisionCache
from qra.core.errors import FatalAuthError, looks_like_invalid_key, looks_like_safety_block
from qra.core.models import Answers
from qra.infra.ratelimit import TokenBudget
from qra.telemetry import NullTelemetry

log = logging.getLogger("qra.judge")


class JudgeError(RuntimeError):
    """The Decision call failed after retries. Not an answer."""


class PermanentJudgeError(JudgeError):
    """A 4xx that retrying cannot fix (e.g. a safety block): the ticket goes to a human."""


def _estimate_tokens(state: str, questions: dict) -> int:
    return int((len(state) + len(json.dumps(questions, ensure_ascii=False))) / 3.2)


class Judge:
    MAX_TRIES = 5

    def __init__(self, create: Callable[..., Any], model: str, cache: DecisionCache | None = None,
                 budget: TokenBudget | None = None, sleep: Callable[[float], None] = time.sleep, telemetry=None):
        self._create, self.model = create, model
        self._tel = telemetry or NullTelemetry()
        self._client: Any = None
        self._cache = cache
        self._budget = budget
        self._sleep = sleep
        self._lock = threading.Lock()
        self.calls = 0  # live (uncached) calls, for cost reporting
        self.cache_hits = 0
        self.tokens = 0  # input tokens of live calls

    @classmethod
    def from_settings(cls, settings, cache: DecisionCache | None = None, budget: TokenBudget | None = None,
                      telemetry=None) -> "Judge":
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key, http_options={"api_version": "v1beta"})
        judge = cls(client.decisions.create, settings.model_judge, cache, budget or TokenBudget(settings.gemini_tpm),
                    telemetry=telemetry)
        judge._client = client  # keep the SDK client alive: it closes its HTTP session when garbage-collected
        return judge

    def cache_stats(self) -> dict:
        return self._cache.stats() if self._cache is not None else {}

    def _key(self, state: str, questions: dict) -> str:
        blob = json.dumps([self.model, state, questions], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def ask(self, state: str, questions: dict) -> Answers:
        key = self._key(state, questions)
        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                with self._lock:
                    self.cache_hits += 1
                self._tel.emit("model", stage="judge", outcome="cached", latency_ms=0, tokens=0)
                return self._parse(hit)
        raw = self._call(state, questions)
        if self._cache is not None:
            self._cache.put(key, raw, tag=self.model)
        return self._parse(raw)

    def _call(self, state: str, questions: dict) -> dict:
        delay = 1.0
        estimate = _estimate_tokens(state, questions)
        for attempt in range(1, self.MAX_TRIES + 1):
            if self._budget:
                self._budget.reserve(estimate)
            t0 = time.monotonic()
            try:
                resp = self._create(model=self.model, input=state, questions=questions)
            except Exception as exc:  # google.genai.errors.APIError and transport errors
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                code = code if isinstance(code, int) else None
                text = str(exc)
                self._tel.emit("fault", stage="judge", latency_ms=int((time.monotonic() - t0) * 1000), detail={"attempt": attempt, "code": code}, error_class=(
                    "fatal_auth" if looks_like_invalid_key(text, code) else "judge_safety" if looks_like_safety_block(text)
                    else "judge_transient" if (code is None or code == 429 or code >= 500 or code == 408) else "judge_rejected"))
                if looks_like_invalid_key(text, code):
                    raise FatalAuthError("The Gemini API key is invalid or not authorised") from exc
                if looks_like_safety_block(text):
                    raise PermanentJudgeError(f"Decision call blocked for safety ({code}): {text[:160]}") from exc
                transient = code is None or code == 429 or code >= 500 or code == 408
                if not transient:  # our request was wrong (4xx): retrying cannot help, but it must not be hidden
                    raise JudgeError(f"Decision call rejected ({code}): {text[:160]}") from exc
                if attempt == self.MAX_TRIES:
                    raise JudgeError(f"Decision call failed after {attempt} tries: {type(exc).__name__}") from exc
                wait = delay * (0.5 + random.random())
                log.warning("Decision call failed (%s), retry %d in %.1fs", type(exc).__name__, attempt, wait)
                self._sleep(wait)
                delay *= 2
                continue
            out = self._to_dict(resp)
            self._tel.emit("model", stage="judge", outcome="ok", latency_ms=int((time.monotonic() - t0) * 1000), tokens=out["tokens"])
            if self._budget:
                self._budget.record(out["tokens"], estimate)
            with self._lock:
                self.calls += 1
                self.tokens += out["tokens"]
            return out
        raise AssertionError("unreachable")

    @staticmethod
    def _to_dict(resp: Any) -> dict:
        answers: dict[str, dict] = {}
        for key, a in resp.answers.items():
            if a.type == "noul":
                answers[key] = {"type": "noul", "p": float(a.noul)}
            elif a.type == "choice":
                answers[key] = {"type": "choice", "probs": {k: float(v) for k, v in a.probabilities.items()}}
            else:
                raise JudgeError(f"unexpected answer type {a.type!r} for {key}")
        return {"answers": answers, "tokens": int(resp.usage.total_input_tokens or 0)}

    @staticmethod
    def _parse(raw: dict) -> Answers:
        noul = {k: v["p"] for k, v in raw["answers"].items() if v["type"] == "noul"}
        choice = {k: v["probs"] for k, v in raw["answers"].items() if v["type"] == "choice"}
        return Answers(noul=noul, choice=choice, tokens=raw.get("tokens", 0))
