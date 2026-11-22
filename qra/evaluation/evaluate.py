"""Eval harness.

Two label sources, never a model:
  * Nordavo's 10 labelled examples, fetched live from GET /examples (not stored in the repo).
  * eval/authored_cases.jsonl: cases and labels WE wrote from the client's rules before running anything.
    They are reported separately and labelled as authored; the 30 practice tickets are never used here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from qra.core.faq import Faq
from qra.decision.judge import Judge, JudgeError
from qra.core.models import Action, Ticket
from qra.decision.questions import DEFAULT_FAQ_MODE
from qra.runtime.pipeline import Planned, plan_ticket

AUTHORED = Path(__file__).resolve().parent.parent.parent / "eval" / "authored_cases.jsonl"
FAQ_ID = re.compile(r"\bF\d{2}\b")


@dataclass
class Case:
    id: str
    ticket: Ticket
    label: dict
    source: str  # "nordavo" | "authored"


def nordavo_cases(raw_examples: list[dict]) -> list[Case]:
    cases = []
    for e in raw_examples:
        label = dict(e["label"])
        cited = FAQ_ID.findall(label.get("reason", ""))
        if label.get("action") == "reply" and cited:
            label["faq"] = cited[0]
        cases.append(Case(e["id"], Ticket(e["id"], e.get("subject", ""), e.get("body", ""),
                                          e.get("channel", ""), e.get("customer_plan", "")), label, "nordavo"))
    return cases


def authored_cases(path: Path = AUTHORED) -> list[Case]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            cases.append(Case(r["id"], Ticket(r["id"], r["subject"], r["body"], "email", r.get("plan", "")),
                              r["label"], "authored"))
    return cases


def run_cases(cases: list[Case], judge: Judge, faq: Faq, mode: str = DEFAULT_FAQ_MODE) -> list[dict]:
    rows = []
    for c in cases:
        try:
            p: Planned = plan_ticket(c.ticket, judge, faq, mode)
        except JudgeError as exc:
            rows.append({"id": c.id, "source": c.source, "error": str(exc), "label": c.label})
            continue
        plan = p.plan
        rows.append({
            "id": c.id, "source": c.source, "label": c.label,
            "got": {"language": plan.language, "category": plan.category, "priority": plan.priority,
                    "action": plan.action.value, "faq": plan.faq_ids[0] if plan.faq_ids else None},
            "row": plan.row, "confidence": plan.confidence, "reason": plan.reason, "faq_ids": plan.faq_ids,
            "coverage": p.coverage, "triggers": {k: round(v, 3) for k, v in plan.triggers.items()},
            "tokens": p.tokens, "judge_calls": p.judge_calls,
        })
    return rows


def _rate(hits: int, total: int) -> str:
    return f"{hits}/{total}" if total else "n/a"


def score(rows: list[dict]) -> dict:
    ok = [r for r in rows if "got" in r]
    m: dict = {"cases": len(rows), "errors": len(rows) - len(ok)}
    for f in ("language", "category", "priority", "action"):
        graded = [r for r in ok if f in r["label"]]
        m[f] = _rate(sum(r["got"][f] == r["label"][f] for r in graded), len(graded))
    must = [r for r in ok if r["label"]["action"] in ("handoff", "close")]
    m["unsafe_replies"] = sum(r["got"]["action"] == "reply" for r in must)  # headline: must be 0
    hand = [r for r in ok if r["label"]["action"] == "handoff"]
    m["handoff_recall"] = _rate(sum(r["got"]["action"] == "handoff" for r in hand), len(hand))
    reply = [r for r in ok if r["label"]["action"] == "reply"]
    m["over_escalation"] = _rate(sum(r["got"]["action"] != "reply" for r in reply), len(reply))
    faq = [r for r in reply if r["label"].get("faq")]
    m["faq_match"] = _rate(sum(r["label"]["faq"] in r["faq_ids"] for r in faq), len(faq))
    close = [r for r in ok if r["got"]["action"] == "close"]
    m["close_precision"] = _rate(sum(r["label"]["action"] == "close" for r in close), len(close))
    return m


def format_report(rows: list[dict]) -> str:
    out = []
    for r in rows:
        if "got" not in r:
            out.append(f"{r['id']:5} ERROR {r['error']}")
            continue
        g, l = r["got"], r["label"]
        flags = "".join(
            f" {f}:{g[f]}!={l[f]}" for f in ("language", "category", "priority", "action") if f in l and g[f] != l[f])
        out.append(f"{r['id']:5} {'ok ' if not flags else 'DIFF'} row{r['row']} {g['action']:7} {g['priority']} "
                   f"{g['category']:15} {g['language']} conf={r['confidence']:.2f}{flags}")
    return "\n".join(out)
