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


def _with_opinion(args, s: Settings) -> Settings:
    from dataclasses import replace
    if getattr(args, "judge", None):
        s = replace(s, judge_backend=args.judge)
    return replace(s, second_opinion=False) if getattr(args, "no_second_opinion", False) else s


def cmd_run(args, s: Settings) -> int:
    s = _with_opinion(args, s)
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
    s = _with_opinion(args, s)
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


def cmd_experiment(args, s: Settings) -> int:
    from qra.decision.judge import Judge
    from qra.evaluation.experiments import run_experiment
    faq, nd, au = load_faq(), nordavo_cases(make_client(s).examples()), authored_cases()
    runs = []
    for k in range(args.repeat):
        # --fresh disables the cache so every repetition is a live, independent measurement
        judge = Judge.from_settings(s, cache=None) if args.fresh else make_judge(s)
        result = run_experiment(judge, faq, nd, au, tuple(args.modes))
        runs.append(result)
        for mode, b in result.items():
            n, a = b["metrics"]["nordavo_examples"], b["metrics"]["authored_cases"]
            print(f"run {k + 1} {mode:18} tokens/ticket={b['tokens_per_ticket']:5} unsafe={n['unsafe_replies'] + a['unsafe_replies']} "
                  f"action nordavo={n['action']:>5} authored={a['action']:>6} over-escalation={n['over_escalation']}+{a['over_escalation']} errors={b['errors']}", flush=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        slim = [{m: {k2: v for k2, v in b.items() if k2 != "rows"} for m, b in r.items()} for r in runs]
        Path(args.out).write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


def cmd_noise(args, s: Settings) -> int:
    from qra.decision.judge import Judge
    from qra.evaluation.variance import measure
    cases = nordavo_cases(make_client(s).examples()) + authored_cases()
    r = measure(Judge.from_settings(s, cache=None), load_faq(), cases, draws=args.draws)
    print(f"{r['cases']} cases x {r['draws']} draws, {r['tokens']:,} tokens")
    print(f"threshold crossings: {r['threshold_crossings']}; the resample margin ({r['margin']}) flags {r['caught_by_margin']} of them from the first draw alone"
          + (f" (catch rate {r['catch_rate']:.0%})" if r['catch_rate'] is not None else ""))
    print(f"{'question':26} {'mean std':>9} {'max std':>8} {'threshold':>10} {'crossings':>10} {'caught':>7}")
    for q in r["questions"][: args.top]:
        thr = f"{q['threshold']:.2f}" if q["threshold"] is not None else "n/a"
        print(f"{q['question']:26} {q['mean_std']:9.3f} {q['max_std']:8.3f} {thr:>10} {q['crossings']:10} {q['caught_by_margin']:7}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


def cmd_trace(args, s: Settings) -> int:
    from qra.runtime.service import telemetry_path
    from qra.telemetry.analytics import TelemetryReader
    reader = TelemetryReader(telemetry_path(s))
    rows = reader.faults_of(args.target) if args.faults else reader.trace(args.target, args.run)
    if not rows:
        print("nothing recorded for", args.target)
        return 0
    for e in rows:
        when = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        extra = f" {e['error_class']}" if e["error_class"] else ""
        print(f"{when} {e['run_id'][:14]:14} {e['kind']:10} {e['stage'] or '':8} {e['ticket'] or '':6} {e['outcome'] or '':10}"
              f" {e['latency_ms'] if e['latency_ms'] is not None else '':>6}ms{extra}")
    return 0


def cmd_cache(args, s: Settings) -> int:
    from qra.infra.cache import clear_store, store_stats
    path = s.state_dir / "cache.db"
    if args.action == "clear":
        removed = clear_store(path, None if args.namespace == "all" else args.namespace)
        print("removed:", json.dumps(removed) if removed else "nothing")
    print(f"{'namespace':12} {'entries':>8} {'size':>10} {'hits':>8}")
    for ns, v in sorted(store_stats(path).items()):
        print(f"{ns:12} {v['entries']:8} {v['bytes'] / 1024:8.1f} KB {v['hits']:8}")
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
    r.add_argument("--no-second-opinion", action="store_true", help="skip the agent that reviews borderline reply candidates")
    r.add_argument("--judge", choices=["gemini", "typesafe"], help="decision model: gemini-decision-1 (default) or Jev on TypeSafe")
    r.set_defaults(fn=cmd_run)
    rs = sub.add_parser("reset", help="re-open all practice tickets and start a new ledger epoch")
    rs.add_argument("--yes", action="store_true")
    rs.set_defaults(fn=cmd_reset)
    e = sub.add_parser("eval", help="score the labelled examples and our authored cases (no tickets are touched)")
    e.add_argument("--out", help="write the full result JSON here")
    e.add_argument("--no-second-opinion", action="store_true", help="evaluate without the second-opinion agent (the baseline)")
    e.add_argument("--judge", choices=["gemini", "typesafe"], help="decision model: gemini-decision-1 (default) or Jev on TypeSafe")
    e.set_defaults(fn=cmd_eval)
    x = sub.add_parser("explain", help="show every probability and the truth-table row for one ticket (read-only)")
    x.add_argument("ticket")
    x.set_defaults(fn=cmd_explain)
    ex = sub.add_parser("experiment", help="compare ways of asking the help-centre question: tokens vs accuracy and safety")
    ex.add_argument("--modes", nargs="+", default=["per_entry", "shared_definition", "choice"])
    ex.add_argument("--repeat", type=int, default=1, help="repeat every variant this many times")
    ex.add_argument("--fresh", action="store_true", help="bypass the cache so each repetition is a live measurement")
    ex.add_argument("--out")
    ex.set_defaults(fn=cmd_experiment)
    nz = sub.add_parser("noise", help="measure which questions are noisy: probability spread and threshold crossings")
    nz.add_argument("--draws", type=int, default=3)
    nz.add_argument("--top", type=int, default=15)
    nz.add_argument("--out")
    nz.set_defaults(fn=cmd_noise)
    tr = sub.add_parser("trace", help="everything that happened to one ticket (or one failure class with --faults)")
    tr.add_argument("target")
    tr.add_argument("--faults", action="store_true")
    tr.add_argument("--run")
    tr.set_defaults(fn=cmd_trace)
    ca = sub.add_parser("cache", help="show or clear the response caches")
    ca.add_argument("action", choices=["stats", "clear"])
    ca.add_argument("--namespace", choices=["all", "decisions", "drafts"], default="all")
    ca.set_defaults(fn=cmd_cache)
    sv = sub.add_parser("serve", help="open the live dashboard and bench controls in your browser (local only)")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-browser", action="store_true")
    sv.set_defaults(fn=cmd_serve)
    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.fn(args, Settings.from_env(require_gemini=args.cmd in ("run", "eval", "explain", "experiment", "noise")))


if __name__ == "__main__":
    sys.exit(main())
