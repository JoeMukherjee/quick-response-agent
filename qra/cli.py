"""Command line: python -m qra <run|eval|explain|status|reset> ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from qra.infra.cache import KVCache
from qra.core.config import Settings
from qra.core.errors import FatalAuthError
from qra.evaluation.evaluate import authored_cases, format_report, nordavo_cases, run_cases, score
from qra.core.faq import load_faq
from qra.infra.helpdesk import HelpdeskClient
from qra.decision.judge import Judge
from qra.infra.ledger import Ledger
from qra.core.models import Ticket
from qra.runtime.pipeline import plan_ticket
from qra.infra.ratelimit import RequestLimiter
from qra.runtime.service import execute_eval, execute_reset, execute_run, make_client, make_judge, make_ledger


def _client(s: Settings):
    return make_client(s)


def _judge(s: Settings):
    return make_judge(s)


def cmd_run(args, s: Settings) -> int:
    r = execute_run(s, workers=args.workers, dry_run=args.dry_run)
    if r.summary:
        print(json.dumps(r.summary, indent=2))
    if r.message:
        print(r.message, file=sys.stderr)
    return r.exit_code


def cmd_reset(args, s: Settings) -> int:
    if not args.yes:
        print("This re-opens ALL practice tickets on the helpdesk. Re-run with --yes to confirm.", file=sys.stderr)
        return 2
    r = execute_reset(s)
    print(r.message, file=sys.stderr if r.exit_code else sys.stdout)
    return r.exit_code


def cmd_eval(args, s: Settings) -> int:
    r = execute_eval(s, Path(args.out) if args.out else None)
    if r.exit_code:
        print(r.message, file=sys.stderr)
        return r.exit_code
    for name, block in r.summary["results"].items():
        print()
        print(f"== {name} ==")
        print(format_report(block["rows"]))
        print(f"metrics: {json.dumps(block['metrics'])}")
    print()
    print(f"live Decision calls: {r.summary['live_calls']}, cache hits: {r.summary['cache_hits']}")
    if args.out:
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


def cmd_serve(args, s: Settings) -> int:
    from qra.server.app import serve
    return serve(s, port=args.port, open_browser=not args.no_browser)


def cmd_status(args, s: Settings) -> int:
    print(json.dumps({"helpdesk": _client(s).status(), "ledger": make_ledger(s).counts()}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qra")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="handle every open ticket (safe to re-run; resumes after a crash)")
    r.add_argument("--workers", type=int, default=4, help="parallel workers (default 4; the helpdesk allows 5 req/s)")
    r.add_argument("--dry-run", action="store_true", help="decide and draft but post nothing")
    r.set_defaults(fn=cmd_run)
    rs = sub.add_parser("reset", help="re-open all practice tickets and start a new ledger epoch")
    rs.add_argument("--yes", action="store_true")
    rs.set_defaults(fn=cmd_reset)
    e = sub.add_parser("eval", help="score the labelled examples and our authored cases (no tickets are touched)")
    e.add_argument("--out", help="write the full result JSON here")
    e.set_defaults(fn=cmd_eval)
    x = sub.add_parser("explain", help="show every probability and the truth-table row for one ticket (read-only)")
    x.add_argument("ticket")
    x.set_defaults(fn=cmd_explain)
    sv = sub.add_parser("serve", help="open the live dashboard and bench controls in your browser (local only)")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-browser", action="store_true")
    sv.set_defaults(fn=cmd_serve)
    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.fn(args, Settings.from_env(require_gemini=args.cmd in ("run", "eval", "explain")))


if __name__ == "__main__":
    sys.exit(main())
