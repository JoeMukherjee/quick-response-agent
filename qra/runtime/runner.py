"""Runs the agent over all open tickets.

    lock -> snapshot ALL open ticket ids (closing tickets while paginating would skip pages)
         -> queue -> N worker threads -> per ticket: plan -> draft -> verify -> persist -> deliver -> log

A model or network failure on one ticket leaves THAT ticket open (never an automatic handoff) and makes the
run exit non-zero, so a re-run picks it up. Only the helpdesk client, the ledger and the limiters are shared
between threads, and all of them are thread-safe.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from qra.runtime.delivery import Outcome, deliver
from qra.core.errors import FatalAuthError
from qra.core.faq import Faq
from qra.infra.helpdesk import HelpdeskClient, HelpdeskError
from qra.decision.judge import Judge, JudgeError, PermanentJudgeError
from qra.infra.ledger import TERMINAL, Ledger
from qra.core.models import Action, Ticket
from qra.runtime.pipeline import plan_ticket
from qra.decision.policy import Plan, explain
from qra.safety.redact import redact
from qra.runtime.runlog import RunLog
from qra.telemetry import NullTelemetry
from qra.safety.verify import verify_reply
from qra.infra.workqueue import LocalQueue, WorkQueue

log = logging.getLogger("qra.runner")


class RunnerError(RuntimeError):
    pass


class DrafterLike(Protocol):
    def reply(self, ticket, language, entries): ...
    def handoff_note(self, ticket, why, priority, category, rejected_draft=""): ...


class RunLock:
    """Single-runner lock (O_CREAT|O_EXCL). A stale lock must be removed by hand: killing a process by PID
    is unsafe to automate."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RunnerError(f"another run holds {self.path} (delete it if that run is gone)") from exc
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


@dataclass
class RunSummary:
    total: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


def _judgments(planned) -> dict | None:
    """All the judgments the rule table saw for this ticket: every yes/no probability, the top option of each choice, and
    the coverage score. With the thresholds in docs/RULES.md this is enough to replay the decision by hand."""
    answers = getattr(planned, "triage", None) if planned else None
    if answers is None:
        return None
    out: dict = {k: round(v, 3) for k, v in answers.noul.items()}
    for k, probs in answers.choice.items():
        top = max(probs, key=probs.get)
        out[k] = {top: round(probs[top], 3)}
    if planned.coverage is not None:
        out["covers_everything"] = round(planned.coverage, 3)
    return out


def _scrub(payload: dict) -> dict:
    return {k: (redact(v).text if isinstance(v, str) else v) for k, v in payload.items()}


class Runner:
    def __init__(self, client: HelpdeskClient, judge: Judge, drafter: DrafterLike, ledger: Ledger, faq: Faq,
                 runlog: RunLog, workers: int = 4, dry_run: bool = False,
                 queue_factory=lambda: LocalQueue(maxsize=100), telemetry=None, opinion=None, shortlister=None):
        self.client, self.judge, self.drafter, self.ledger, self.faq = client, judge, drafter, ledger, faq
        self.opinion = opinion  # second-opinion agent for borderline reply candidates
        self.shortlister = shortlister  # cascade retrieval, or None for the exhaustive method
        self.runlog, self.workers, self.dry_run = runlog, max(1, workers), dry_run
        self._queue_factory = queue_factory
        self.tel = telemetry or NullTelemetry()
        self._lock = threading.Lock()
        self._fatal: FatalAuthError | None = None
        self.summary = RunSummary()

    # ---- orchestration ----------------------------------------------------------------------
    def run(self) -> RunSummary:
        started = time.monotonic()
        self.tel.emit("run_start", detail={
            "workers": self.workers, "dry_run": self.dry_run,
            "judge": {"provider": getattr(self.judge, "provider", "unknown"), "model": getattr(self.judge, "model", "unknown")},
            "retrieval": "cascade" if self.shortlister is not None else "exhaustive", "second_opinion": self.opinion is not None})
        try:
            tickets = [Ticket.from_api(t) for t in self.client.open_tickets()]  # snapshot BEFORE any POST
        except FatalAuthError:
            raise
        except HelpdeskError as exc:  # the helpdesk is unreachable: report cleanly, do not crash
            self.summary.errors.append(f"could not list open tickets: {exc}")
            self.tel.emit("fault", stage="list", error_class="list_failed", detail={"exc": type(exc).__name__})
            self.tel.emit("run_end", outcome="error")
            self.runlog.write(ticket_id="-", outcome="error", error=f"could not list open tickets: {exc}")
            self.summary.seconds = round(time.monotonic() - started, 1)
            return self.summary
        self._check_epoch(tickets)
        self.summary.total = len(tickets)
        self.tel.emit("run_start", stage="snapshot", detail={"tickets": len(tickets), "workers": self.workers})
        q: WorkQueue = self._queue_factory()
        threads = [threading.Thread(target=self._worker, args=(q,), name=f"qra-worker-{i}", daemon=True)
                   for i in range(self.workers)]
        for t in threads:
            t.start()
        for ticket in tickets:
            q.put(ticket)
        q.close(self.workers)
        for t in threads:
            t.join()
        self.summary.seconds = round(time.monotonic() - started, 1)
        self.tel.emit("run_end", outcome="fatal" if self._fatal else "error" if self.summary.errors else "ok",
                      latency_ms=int(self.summary.seconds * 1000), detail={"outcomes": self.summary.outcomes})
        if self._fatal:
            raise self._fatal
        return self.summary

    def _check_epoch(self, tickets: list[Ticket]) -> None:
        stale = [t.id for t in tickets if self.ledger.state(t.id) in TERMINAL]
        if stale and not self.dry_run:
            self.tel.emit("fault", stage="epoch", error_class="stale_epoch", detail={"tickets": len(stale)})
            raise RunnerError(
                f"{len(stale)} tickets are open on the server but marked done in the ledger ({', '.join(stale[:3])}...). "
                "The helpdesk was reset outside this tool. Run `python -m qra reset` to start a new epoch.")

    def _worker(self, q: WorkQueue) -> None:
        while (ticket := q.get()) is not None:
            if self._fatal:
                continue  # invalid credentials: drain the queue without touching any ticket
            try:
                self._process(ticket)
            except FatalAuthError as exc:  # the only failure that stops a run
                self._fatal = exc
                self._record(ticket.id, error=f"fatal: {exc}")
            except Exception as exc:  # one bad ticket must never stop the others
                log.exception("ticket %s failed", ticket.id)
                self._record(ticket.id, error=f"{type(exc).__name__}: {str(exc)[:160]}")

    def _elapsed_ms(self, t0: float) -> int:
        """Time spent on a ticket, minus any time spent waiting for the free-tier token window: that is pacing, not latency."""
        waited = self.judge.waited_here() if hasattr(self.judge, "waited_here") else 0.0
        return max(0, int((time.monotonic() - t0 - waited) * 1000))

    # ---- one ticket ------------------------------------------------------------------------------
    def _process(self, ticket: Ticket) -> None:
        t0 = time.monotonic()
        if hasattr(self.judge, "reset_waited"):
            self.judge.reset_waited()
        state = None if self.dry_run else self.ledger.state(ticket.id)  # a dry run never reads or writes the ledger
        if state in TERMINAL:
            self._record(ticket.id, outcome="skipped", detail=f"already {state}")
            return
        if state == "decided":  # a previous run died after deciding: resend the STORED decision, do not regenerate
            action, payload = self.ledger.decision(ticket.id)
            out = deliver(self.client, self.ledger, ticket.id, action, payload)
            self._record(ticket.id, action=action, outcome=out.result, detail=out.detail, resumed=True,
                         ms=self._elapsed_ms(t0))
            return
        try:
            if redact(f"{ticket.subject} {ticket.body}").found:
                self.tel.emit("fault", stage="redact", ticket=ticket.id, error_class="payment_data_redacted", detail={"severity": "info"})
            with self.tel.span("plan", ticket.id):
                planned = plan_ticket(ticket, self.judge, self.faq, opinion=self.opinion, shortlister=self.shortlister)
        except PermanentJudgeError as exc:
            planned = None
            plan = Plan(Action.HANDOFF, "other", "P2", "nl", "The assistant could not process this message safely.",
                        0, 0.0, triggers={"judge_rejected": 1.0})
            log.warning("ticket %s: permanent judge error (%s), handing off", ticket.id, exc)
        except JudgeError as exc:
            self._record(ticket.id, outcome="error", error=str(exc))  # leave open; a re-run retries
            return
        else:
            plan = planned.plan

        model_calls = {"judge_in": planned.tokens if planned else 0}
        if planned and planned.opinion:
            model_calls["opinion_in"], model_calls["opinion_out"] = planned.opinion_tokens
        failures: list[str] = []
        gloss = ""
        try:
            action, payload, gloss, failures = self._build(ticket, plan, model_calls)
        except JudgeError as exc:
            self._record(ticket.id, outcome="error", error=str(exc))
            return

        if self.dry_run:
            self._record(ticket.id, action=action.value, outcome="dry-run", plan=plan, failures=failures,
                         gloss=gloss, model_calls=model_calls, ms=self._elapsed_ms(t0),
                         resampled=bool(planned and planned.resampled), borderline=planned.borderline if planned else None,
                         opinion=planned.opinion if planned else None,
                         shortlist=planned.shortlist if planned else None,
                         judgments=_judgments(planned))
            return
        with self.tel.span("deliver", ticket.id):
            out: Outcome = deliver(self.client, self.ledger, ticket.id, action.value, payload)
        self._record(ticket.id, action=action.value, outcome=out.result, detail=out.detail, plan=plan,
                     failures=failures, gloss=gloss, model_calls=model_calls, ms=self._elapsed_ms(t0),
                     resampled=bool(planned and planned.resampled), borderline=planned.borderline if planned else None,
                         opinion=planned.opinion if planned else None,
                         shortlist=planned.shortlist if planned else None,
                         judgments=_judgments(planned))

    def _build(self, ticket: Ticket, plan: Plan, calls: dict) -> tuple[Action, dict, str, list[str]]:
        base = {"language": plan.language, "category": plan.category, "priority": plan.priority,
                "reason": explain(plan), "confidence": plan.confidence}
        if plan.action is Action.CLOSE:
            return Action.CLOSE, _scrub(base), "", []
        failures: list[str] = []
        gloss = ""
        rejected = ""
        if plan.action is Action.REPLY:
            entries = [self.faq[i] for i in plan.faq_ids]
            with self.tel.span("draft", ticket.id):
                draft = self.drafter.reply(ticket, plan.language, entries)
            calls.update(draft_in=draft.in_tokens, draft_out=draft.out_tokens, draft_thought=draft.thought_tokens)
            with self.tel.span("verify", ticket.id):
                verdict = verify_reply(ticket, entries, draft.text, plan.language, self.judge)
            if not verdict.ok:
                self.tel.emit("fault", stage="verify", ticket=ticket.id, error_class="verify_reject", detail={"failures": len(verdict.failures)})
            calls["verify_in"] = verdict.tokens
            gloss = draft.gloss
            if verdict.ok:
                return Action.REPLY, _scrub({**base, "draft_reply": draft.text}), gloss, []
            failures, rejected = verdict.failures, draft.text
            plan = Plan(Action.HANDOFF, plan.category, plan.priority, plan.language,
                        "A drafted reply failed verification, so a person should answer.", 9,
                        plan.confidence, plan.faq_ids, plan.triggers)
            base["reason"], base["confidence"] = explain(plan), plan.confidence
        with self.tel.span("note", ticket.id):
            note = self.drafter.handoff_note(ticket, plan.reason, plan.priority, plan.category, rejected)
        calls["note_in"], calls["note_out"] = note.in_tokens, note.out_tokens
        return Action.HANDOFF, _scrub({**base, "handoff_note": note.text}), gloss, failures

    # ---- bookkeeping -----------------------------------------------------------------------------
    def _record(self, ticket_id: str, *, outcome: str = "", action: str = "", plan: Plan | None = None,
                failures: list[str] | None = None, gloss: str = "", model_calls: dict | None = None,
                error: str = "", detail: str = "", ms: int = 0, resumed: bool = False,
                resampled: bool = False, borderline: list[str] | None = None, opinion: dict | None = None,
                shortlist: list[str] | None = None, judgments: dict | None = None) -> None:
        with self._lock:
            key = outcome or "error"
            self.summary.outcomes[key] = self.summary.outcomes.get(key, 0) + 1
            if action:
                self.summary.actions[action] = self.summary.actions.get(action, 0) + 1
            if error:
                self.summary.errors.append(f"{ticket_id}: {error}")
        fields = {"ticket_id": ticket_id, "outcome": key, "action": action, "detail": detail, "error": error,
                  "ms": ms, "resumed": resumed, "dry_run": self.dry_run, "resampled": resampled}
        if borderline:
            fields["borderline"] = borderline
        if opinion:
            fields["second_opinion"] = opinion
        if shortlist:
            fields["shortlist"] = shortlist
        if judgments:
            fields["judgments"] = judgments
        if plan:
            fields.update(language=plan.language, category=plan.category, priority=plan.priority,
                          confidence=plan.confidence, row=plan.row, faq_ids=plan.faq_ids, triggers=plan.triggers,
                          reason=plan.reason)
        if failures:
            fields["verify_failures"] = failures
        if gloss:
            fields["en_gloss"] = gloss
        if model_calls:
            fields["model_calls"] = model_calls
        self.runlog.write(**fields)
        if error:
            self.tel.emit("fault", stage="runner", ticket=ticket_id, error_class="ticket_error", detail={"error": error[:160]})
        self.tel.emit("ticket", ticket=ticket_id, outcome=key, latency_ms=ms or None,
                      error_class="ticket_error" if error else None,
                      detail={"action": action, "priority": plan.priority if plan else None,
                              "language": plan.language if plan else None, "row": plan.row if plan else None,
                              "confidence": plan.confidence if plan else None, "resumed": resumed, "resampled": resampled,
                              "error": error[:160] if error else None})
