"""Which questions are noisy?

Each labelled case is asked about several times with slightly different inputs (the same variation that resampling
uses), with the cache off. For every question we report how much its probability moves and, for the questions that
gate a decision, how often the draws land on opposite sides of the threshold. Those threshold crossings are the
cases where run-to-run noise changes the outcome, and the "caught" column says how many of them the resample margin
would have flagged from the first draw alone.
"""
from __future__ import annotations

import statistics

from qra.core.config import THRESHOLDS as T
from qra.core.faq import Faq
from qra.decision.judge import Judge
from qra.decision.questions import DEFAULT_FAQ_MODE, HARD_RULES, faq_key, ticket_state, triage_questions
from qra.evaluation.evaluate import Case


def gating_thresholds(faq: Faq) -> dict[str, float]:
    gates = {"injection": T["injection"], "spam": T["spam_close"], "many_users_blocked": T["yes"], "customer_blocked": 0.5}
    gates.update({r.key: T["hard_rule"] for r in HARD_RULES})
    gates.update({faq_key(i): T["faq_resolves"] for i in faq.ids()})
    return gates


def measure(judge: Judge, faq: Faq, cases: list[Case], draws: int = 3, mode: str = DEFAULT_FAQ_MODE) -> dict:
    questions = triage_questions(faq, mode)
    gates = gating_thresholds(faq)
    margin = T["resample_margin"]
    spread: dict[str, list[float]] = {}
    crossings: dict[str, int] = {}
    caught: dict[str, int] = {}
    tokens = 0
    for c in cases:
        samples = []
        for k in range(draws):
            a = judge.ask(ticket_state(c.ticket, mode, sample=k), questions)
            tokens += a.tokens
            samples.append(a)
        for key in samples[0].noul:
            ps = [s.noul[key] for s in samples]
            spread.setdefault(key, []).append(statistics.pstdev(ps))
            thr = gates.get(key)
            if thr is not None and min(ps) < thr <= max(ps):
                crossings[key] = crossings.get(key, 0) + 1
                if abs(ps[0] - thr) <= margin:
                    caught[key] = caught.get(key, 0) + 1
    rows = []
    for key, sds in spread.items():
        rows.append({"question": key, "mean_std": round(statistics.mean(sds), 4), "max_std": round(max(sds), 4),
                     "threshold": gates.get(key), "crossings": crossings.get(key, 0), "caught_by_margin": caught.get(key, 0)})
    rows.sort(key=lambda r: (-r["crossings"], -r["mean_std"]))
    total_cross = sum(crossings.values())
    return {"cases": len(cases), "draws": draws, "tokens": tokens, "margin": margin,
            "threshold_crossings": total_cross, "caught_by_margin": sum(caught.values()),
            "catch_rate": round(sum(caught.values()) / total_cross, 3) if total_cross else None,
            "questions": rows}
