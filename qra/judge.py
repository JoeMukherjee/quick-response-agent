"""Thin, cached, rate-limited wrapper around the Gemini Decisions API (gemini-decision-1, v1beta).

* Responses are cached on disk by sha256(model, input, questions): re-running the eval after a change to
  thresholds or the truth table replays free and instantly, and only reworded questions cost tokens.
* Token budget: every call's reported usage feeds a one-minute window (free tier: 50k tokens per minute).
* A failed call is an ERROR, never a "no": it raises JudgeError and the caller leaves the ticket open.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

from .models import Answers
from .ratelimit import TokenBudget

log = logging.getLogger("qra.judge")


class JudgeError(RuntimeError):
    """The Decision call failed after retries. Not an answer."""


class PermanentJudgeError(JudgeError):
    """A 4xx that retrying cannot fix (e.g. a safety block): the ticket goes to a human."""


def _estimate_tokens(state: str, questions: dict) -> int:
    return int((len(state) + len(json.dumps(questions, ensure_ascii=False))) / 3.2)


class Judge:
    MAX_TRIES = 5

    def __init__(self, create: Callable[..., Any], model: str, cache_dir: Path | None = None,
                 budget: TokenBudget | None = None, sleep: Callable[[float], None] = time.sleep):
        self._create, self.model = create, model
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._budget = budget
        self._sleep = sleep
        self.calls = 0  # live (uncached) calls, for cost reporting
        self.cache_hits = 0

    @classmethod
    def from_settings(cls, settings, cache_dir: Path | None = None, budget: TokenBudget | None = None) -> "Judge":
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key, http_options={"api_version": "v1beta"})
        return cls(client.decisions.create, settings.model_judge, cache_dir,
                   budget or TokenBudget(settings.gemini_tpm))

    def _key(self, state: str, questions: dict) -> str:
        blob = json.dumps([self.model, state, questions], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def ask(self, state: str, questions: dict) -> Answers:
        key = self._key(state, questions)
        path = self._cache_dir / f"{key}.json" if self._cache_dir else None
        if path and path.exists():
            self.cache_hits += 1
            return self._parse(json.loads(path.read_text(encoding="utf-8")))
        raw = self._call(state, questions)
        if path:
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return self._parse(raw)

    def _call(self, state: str, questions: dict) -> dict:
        delay = 1.0
        for attempt in range(1, self.MAX_TRIES + 1):
            if self._budget:
                self._budget.reserve(_estimate_tokens(state, questions))
            try:
                resp = self._create(model=self.model, input=state, questions=questions)
            except Exception as exc:  # google.genai.errors.APIError and transport errors
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if isinstance(code, int) and 400 <= code < 500 and code != 429:
                    raise PermanentJudgeError(f"Decision call rejected ({code}): {str(exc)[:200]}") from exc
                if attempt == self.MAX_TRIES:
                    raise JudgeError(f"Decision call failed after {attempt} tries: {type(exc).__name__}") from exc
                wait = delay * (0.5 + random.random())
                log.warning("Decision call failed (%s), retry %d in %.1fs", type(exc).__name__, attempt, wait)
                self._sleep(wait)
                delay *= 2
                continue
            self.calls += 1
            out = self._to_dict(resp)
            if self._budget:
                self._budget.record(out["tokens"])
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
