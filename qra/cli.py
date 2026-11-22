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
from qra.runtime.runlog import RunLog
from qra.runtime.runner import Runner, RunnerError, RunLock


def _client(s: Settings) -> HelpdeskClient:
    return HelpdeskClient(s.helpdesk_url, s.helpdesk_key, s.candidate_email, s.candidate_name,
                          limiter=RequestLimiter(s.helpdesk_rps))


def _judge(s: Settings) -> Judge:
    return Judge.from_settings(s, cache=KVCache(s.state_dir / "cache.db", "decisions"))


def _ledger(s: Settings) -> Ledger:
    return Ledger(s.state_dir / "ledger.db")


def cmd_run(args, s: Settings) -> int:
    from qra.agents.drafting import Drafter  # imported here so eval/status work without loading the agent framework
    run_id = time.strftime("%Y%m%d-%H%M%S")
    judge, ledger = _judge(s), _ledger(s)
    drafts = KVCache(s.state_dir / "cache.db", "drafts")
    runner = Runner(_client(s), judge, Drafter(s.model_draft, s.gemini_api_key, cache=drafts), ledger, load_faq(),
                    RunLog(s.state_dir / "dry_logs" if args.dry_run else s.log_dir, run_id), workers=args.workers, dry_run=args.dry_run)
    try:
        with RunLock(s.state_dir / "run.lock"):
            summary = runner.run()
    except RunnerError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    except FatalAuthError as exc:
        print(f"stopped: {exc}. Fix the key in .env and run again; no ticket was lost or double-answered.", file=sys.stderr)
        return 3
    print(json.dumps({"run_id": run_id, "tickets": summary.total, "outcomes": summary.outcomes,
                      "actions": summary.actions, "seconds": summary.seconds,
                      "decision_calls": judge.calls, "decision_cache_hits": judge.cache_hits,
                      "decision_input_tokens": judge.tokens,
                      "cache": {"decisions": judge.cache_stats(), "drafts": drafts.stats()}, "errors": summary.errors}, indent=2))
    return summary.exit_code


def cmd_reset(args, s: Settings) -> int:
    if not args.yes:
        print("This re-opens ALL practice tickets on the helpdesk. Re-run with --yes to confirm.", file=sys.stderr)
        return 2
    _client(s).reset()
    epoch = _ledger(s).new_epoch()
    print(f"helpdesk reset; ledger epoch is now {epoch}")
    return 0


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
    print(json.dumps({"helpdesk": _client(s).status(), "ledger": _ledger(s).counts()}, indent=2))
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
    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.fn(args, Settings.from_env())


if __name__ == "__main__":
    sys.exit(main())
