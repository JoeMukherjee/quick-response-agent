"""Token experiments: ask the same help-centre question three ways and compare tokens against accuracy and safety.

Every variant runs over the same labelled cases (Nordavo's examples plus our authored cases). The practice tickets
are never used. Judgments are cached, so re-running a variant costs nothing.
"""
from __future__ import annotations

from qra.core.faq import Faq
from qra.decision.judge import Judge
from qra.decision.questions import FAQ_MODES
from qra.evaluation.evaluate import Case, run_cases, score

DESCRIPTIONS = {
    "per_entry": "12 yes/no questions, each with the entry text and its own long criteria (current design)",
    "shared_definition": "12 yes/no questions, entry text only; the definition of a good response is stated once in the input",
    "choice": "1 choice question over short entry summaries; the entry text is read only in the second stage",
}


def run_experiment(judge: Judge, faq: Faq, nordavo: list[Case], authored: list[Case], modes=FAQ_MODES) -> dict:
    out: dict = {}
    for mode in modes:
        rows = {"nordavo_examples": run_cases(nordavo, judge, faq, mode), "authored_cases": run_cases(authored, judge, faq, mode)}
        every = rows["nordavo_examples"] + rows["authored_cases"]
        ok = [r for r in every if "got" in r]
        tokens = sum(r["tokens"] for r in ok)
        out[mode] = {
            "description": DESCRIPTIONS[mode],
            "metrics": {k: score(v) for k, v in rows.items()},
            "tokens_total": tokens, "tokens_per_ticket": round(tokens / max(1, len(ok))),
            "judge_calls": sum(r["judge_calls"] for r in ok),
            "errors": len(every) - len(ok),
            "rows": rows,
        }
    base = out.get("per_entry")
    if base:
        for mode, block in out.items():
            block["tokens_vs_current"] = round(block["tokens_total"] / max(1, base["tokens_total"]) - 1, 3)
            flips = []
            for name in ("nordavo_examples", "authored_cases"):
                for a, b in zip(base["rows"][name], block["rows"][name]):
                    if "got" in a and "got" in b and a["got"]["action"] != b["got"]["action"]:
                        flips.append({"case": a["id"], "current": a["got"]["action"], "variant": b["got"]["action"],
                                      "label": a["label"]["action"]})
            block["action_differences_vs_current"] = flips
    return out
