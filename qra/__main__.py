"""Command line: python -m qra <eval|explain|status> ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .evaluate import (authored_cases, format_report, nordavo_cases, run_cases, score)
from .faq import load_faq
from .helpdesk import HelpdeskClient
from .judge import Judge
from .models import Ticket
from .pipeline import plan_ticket
from .ratelimit import RequestLimiter


def _client(s: Settings) -> HelpdeskClient:
    return HelpdeskClient(s.helpdesk_url, s.helpdesk_key, s.candidate_email, s.candidate_name,
                          limiter=RequestLimiter(s.helpdesk_rps))


def _judge(s: Settings) -> Judge:
    return Judge.from_settings(s, cache_dir=s.state_dir / "decision_cache")


def cmd_eval(args, s: Settings) -> int:
    faq, judge = load_faq(), _judge(s)
    nd = nordavo_cases(_client(s).examples())
    au = authored_cases()
    result = {}
    for name, cases in (("nordavo_examples", nd), ("authored_cases", au)):
        rows = run_cases(cases, judge, faq)
        result[name] = {"metrics": score(rows), "rows": rows}
        print(f"\n== {name} ==\n{format_report(rows)}\nmetrics: {json.dumps(score(rows))}")
    print(f"\nlive Decision calls: {judge.calls}, cache hits: {judge.cache_hits}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


def cmd_explain(args, s: Settings) -> int:
    client, faq, judge = _client(s), load_faq(), _judge(s)
    ticket = Ticket.from_api(client.get_ticket(args.ticket))
    p = plan_ticket(ticket, judge, faq)
    print(f"ticket {ticket.id}: {ticket.subject!r}")
    print(f"action={p.plan.action.value} row={p.plan.row} priority={p.plan.priority} category={p.plan.category} "
          f"language={p.plan.language} confidence={p.plan.confidence}")
    print(f"reason: {p.plan.reason}")
    if p.triage:
        print("probabilities:")
        for k, v in sorted(p.triage.noul.items(), key=lambda kv: -kv[1]):
            print(f"  {k:24} {v:.3f}")
        for k, v in p.triage.choice.items():
            print(f"  {k:24} " + " ".join(f"{o}={x:.2f}" for o, x in sorted(v.items(), key=lambda kv: -kv[1])))
    if p.coverage is not None:
        print(f"coverage (entries answer everything): {p.coverage:.3f}")
    return 0


def cmd_status(args, s: Settings) -> int:
    print(json.dumps(_client(s).status(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qra")
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("eval", help="score the labelled examples and our authored cases (no tickets are touched)")
    e.add_argument("--out", help="write the full result JSON here")
    e.set_defaults(fn=cmd_eval)
    x = sub.add_parser("explain", help="show every probability and the truth-table row for one ticket (read-only)")
    x.add_argument("ticket")
    x.set_defaults(fn=cmd_explain)
    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.fn(args, Settings.from_env())


if __name__ == "__main__":
    sys.exit(main())
