"""Operations shared by the CLI and the web server: run, reset, eval.

Every function here RETURNS a result and never raises: a failure becomes a message, an exit code and a telemetry
event. The only condition that stops a run is an invalid key, and even that ends with a clean message.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from qra.core.config import ConfigError, Settings
from qra.core.errors import FatalAuthError
from qra.core.faq import load_faq
from qra.decision.judge import Judge
from qra.evaluation.evaluate import authored_cases, heldout_cases, nordavo_cases, run_cases, score
from qra.infra.cache import KVCache
from qra.infra.helpdesk import HelpdeskClient
from qra.infra.ledger import Ledger
from qra.infra.ratelimit import RequestLimiter
from qra.runtime.runlog import RunLog
from qra.runtime.runner import Runner, RunnerError, RunLock
from qra.telemetry import Telemetry

log = logging.getLogger("qra.service")


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def telemetry_path(s: Settings) -> Path:
    return s.state_dir / "telemetry.db"


def make_client(s: Settings, telemetry=None) -> HelpdeskClient:
    return HelpdeskClient(s.helpdesk_url, s.helpdesk_key, s.candidate_email, s.candidate_name,
                          limiter=RequestLimiter(s.helpdesk_rps), telemetry=telemetry)


def make_judge(s: Settings, telemetry=None) -> Judge:
    """gemini-decision-1 by default; Jev on TypeSafe when judge_backend is "typesafe". Everything downstream is identical."""
    cache = KVCache(s.state_dir / "cache.db", "decisions")
    if s.judge_backend == "typesafe":
        if not s.typesafe_api_key:
            raise ConfigError("TYPESAFE_API_KEY is not set (see .env.example)")
        return Judge.typesafe(s, cache=cache, telemetry=telemetry)
    return Judge.from_settings(s, cache=cache, telemetry=telemetry)


def make_opinion(s: Settings, faq, cache=None, telemetry=None):
    """The second-opinion agent for borderline reply candidates: on by default, None when SECOND_OPINION=0."""
    if not s.second_opinion:
        return None
    from qra.agents.opinion import OpinionAgent
    return OpinionAgent(s.model_draft, s.gemini_api_key, faq, cache=cache, telemetry=telemetry)


def make_shortlister(s: Settings, faq):
    """Cascade retrieval (sharded lexical + dense index in front of the judge), or None for the exhaustive method."""
    if s.retrieval != "cascade":
        return None
    from qra.retrieval.shortlist import Shortlister
    return Shortlister.from_faq(faq, s.state_dir / "retrieval", max_entries=s.shortlist_size)


def make_ledger(s: Settings) -> Ledger:
    return Ledger(s.state_dir / "ledger.db")


@dataclass
class RunResult:
    run_id: str
    exit_code: int
    message: str = ""
    summary: dict = field(default_factory=dict)


def execute_run(s: Settings, *, workers: int = 4, dry_run: bool = False, run_id: str | None = None) -> RunResult:
    run_id = run_id or new_run_id()
    tel = Telemetry(telemetry_path(s), run_id)
    try:
        from qra.agents.drafting import Drafter  # loaded here so eval/status work without the agent framework
        judge = make_judge(s, tel)
        drafts = KVCache(s.state_dir / "cache.db", "drafts")
        faq = load_faq()
        opinion = make_opinion(s, faq, drafts, tel)
        shortlister = make_shortlister(s, faq)
        runner = Runner(make_client(s, tel), judge, Drafter(s.model_draft, s.gemini_api_key, cache=drafts, telemetry=tel),
                        make_ledger(s), faq, RunLog(s.state_dir / "dry_logs" if dry_run else s.log_dir, run_id),
                        workers=workers, dry_run=dry_run, telemetry=tel, opinion=opinion,
                        shortlister=shortlister)
        with RunLock(s.state_dir / "run.lock"):
            summary = runner.run()
    except RunnerError as exc:
        text = str(exc)
        tel.emit("fault", stage="runner", error_class="lock_held" if "another run" in text else "stale_epoch",
                 detail={"message": text[:200]})
        return RunResult(run_id, 2, f"refusing to run: {text}")
    except FatalAuthError as exc:
        return RunResult(run_id, 3, f"stopped: {exc}. Fix the key in .env and run again; no ticket was lost or double-answered.")
    except Exception as exc:  # last line of defence: nothing escapes as a crash
        log.exception("unexpected failure in run %s", run_id)
        tel.emit("fault", stage="runner", error_class="service_error", detail={"exc": type(exc).__name__, "message": str(exc)[:160]})
        tel.emit("run_end", outcome="error")
        return RunResult(run_id, 1, f"unexpected error ({type(exc).__name__}); it was logged and nothing was lost. Re-run to continue.")
    return RunResult(run_id, summary.exit_code, "", {
        "run_id": run_id, "tickets": summary.total, "outcomes": summary.outcomes, "actions": summary.actions,
        "seconds": summary.seconds, "decision_calls": judge.calls, "decision_cache_hits": judge.cache_hits,
        "decision_input_tokens": judge.tokens, "cache": {"decisions": judge.cache_stats(), "drafts": drafts.stats()},
        "errors": summary.errors, "telemetry": tel.stats()})


def execute_reset(s: Settings) -> RunResult:
    run_id = new_run_id()
    try:
        make_client(s).reset()
        epoch = make_ledger(s).new_epoch()
        return RunResult(run_id, 0, f"helpdesk reset; ledger epoch is now {epoch}")
    except FatalAuthError as exc:
        return RunResult(run_id, 3, f"stopped: {exc}")
    except Exception as exc:
        return RunResult(run_id, 1, f"reset failed ({type(exc).__name__}: {str(exc)[:120]}); nothing was changed locally")


def execute_eval(s: Settings, out: Path | None = None) -> RunResult:
    run_id = new_run_id()
    try:
        faq, judge = load_faq(), make_judge(s)
        opinion = make_opinion(s, faq, KVCache(s.state_dir / "cache.db", "drafts"))
        shortlister = make_shortlister(s, faq)
        result = {}
        for name, cases in (("nordavo_examples", nordavo_cases(make_client(s).examples())), ("authored_cases", authored_cases()),
                             ("heldout_cases", heldout_cases())):
            rows = run_cases(cases, judge, faq, opinion=opinion, shortlister=shortlister)
            result[name] = {"metrics": score(rows), "rows": rows}
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        return RunResult(run_id, 0, "", {"results": result, "live_calls": judge.calls, "cache_hits": judge.cache_hits})
    except FatalAuthError as exc:
        return RunResult(run_id, 3, f"stopped: {exc}")
    except Exception as exc:
        return RunResult(run_id, 1, f"eval failed ({type(exc).__name__}: {str(exc)[:120]})")
