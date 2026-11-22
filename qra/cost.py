"""Cost per 100 tickets, computed from the per-ticket token counts in a run log (never estimated).

Prices in USD per 1M tokens (Gemini pricing page, valid through 31 Dec 2026; docs/pricing snapshot):
  gemini-decision-1   input 0.10, no output tokens
  gemini-3.8-flash    input 0.75, output 3.75 (thinking tokens are billed as output)
"""
from __future__ import annotations

import json
from pathlib import Path

PRICE = {"decision_in": 0.10, "flash_in": 0.75, "flash_out": 3.75}


def summarize(log_path: Path) -> dict:
    rows = [json.loads(l) for l in Path(log_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r.get("ticket_id") not in (None, "-") and r.get("model_calls") is not None]
    tot = {k: 0 for k in ("judge_in", "verify_in", "draft_in", "draft_out", "draft_thought", "note_in", "note_out")}
    for r in rows:
        for k in tot:
            tot[k] += int(r["model_calls"].get(k, 0) or 0)
    n = len(rows)
    decision = (tot["judge_in"] + tot["verify_in"]) / 1e6 * PRICE["decision_in"]
    flash_in = (tot["draft_in"] + tot["note_in"]) / 1e6 * PRICE["flash_in"]
    flash_out = (tot["draft_out"] + tot["draft_thought"] + tot["note_out"]) / 1e6 * PRICE["flash_out"]
    total = decision + flash_in + flash_out
    per100 = (lambda x: round(x / n * 100, 4)) if n else (lambda x: 0.0)
    return {"tickets": n, "tokens": tot,
            "usd": {"decision_1": round(decision, 5), "flash_in": round(flash_in, 5), "flash_out": round(flash_out, 5),
                    "total": round(total, 5)},
            "usd_per_100_tickets": {"decision_1": per100(decision), "flash": per100(flash_in + flash_out),
                                    "total": per100(total)}}
